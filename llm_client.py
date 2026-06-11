"""
llm_client.py
=============
Client LLM leger partage par les agents A2, A3 et A4.

Il reutilise les memes credentials et variables .env que l'Agent 1,
sans dependre de la couche metier lourde de a1_llm_extractor.py.

Providers supportes: OpenAI (primaire) -> Gemini (fallback).
"""

import json
import os
import time
from typing import Any

from dotenv import load_dotenv

try:
    from openai import OpenAI, RateLimitError as OpenAIRateLimit
except ImportError:
    OpenAI = None
    OpenAIRateLimit = Exception

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

load_dotenv()

PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _clear_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _is_local_proxy_value(value: str) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return False
    normalized = raw
    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    if "@" in normalized:
        normalized = normalized.rsplit("@", 1)[1]
    normalized = normalized.split("/", 1)[0]
    host = normalized.split(":", 1)[0].strip("[]")
    return host in {"127.0.0.1", "localhost", "::1"}


def _sanitize_proxy_env_for_llm() -> None:
    """
    Assainit la config proxy avant init des clients LLM.
    - si LLM_DISABLE_SYSTEM_PROXY=1: supprime tous les proxy env vars
    - sinon: supprime uniquement les proxies locaux (localhost/127.0.0.1/::1)
    """
    if _parse_bool_env("QALITAS_KEEP_SYSTEM_PROXY", False):
        return

    if _parse_bool_env("LLM_DISABLE_SYSTEM_PROXY", False):
        _clear_proxy_env()
        os.environ["QALITAS_PROXY_SANITIZED"] = "1"
        os.environ["QALITAS_PROXY_SANITIZE_REASON"] = "llm_disable_system_proxy"
        return

    removed: list[str] = []
    for key in PROXY_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if value and _is_local_proxy_value(value):
            os.environ.pop(key, None)
            removed.append(key)
    if removed:
        os.environ["QALITAS_PROXY_SANITIZED"] = "1"
        os.environ["QALITAS_PROXY_SANITIZE_REASON"] = "local_proxy_removed"
        os.environ["QALITAS_PROXY_SANITIZED_KEYS"] = ",".join(sorted(removed))


_sanitize_proxy_env_for_llm()


class AgentLLMClient:
    """
    Client LLM simple avec fallback OpenAI -> Gemini.
    Retourne directement le texte (str) ou leve une exception.
    """

    def __init__(self) -> None:
        self.primary_model = os.getenv("PRIMARY_MODEL", "gpt-4o-mini").strip()
        self.fallback_model = os.getenv("FALLBACK_MODEL", "gemini-2.5-flash").strip()
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
        self.max_tokens = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096"))
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
        self.retry_base = float(os.getenv("LLM_RETRY_BASE_SECONDS", "2"))

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

        self._openai = OpenAI(api_key=openai_key) if (OpenAI and openai_key) else None
        self._gemini = (
            google_genai.Client(api_key=gemini_key)
            if (google_genai and gemini_key)
            else None
        )

        self.last_model_used: str | None = None
        self.last_provider_used: str | None = None

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
    ) -> str:
        """
        Appelle le LLM et retourne le texte de la reponse.
        Essaie OpenAI en premier, puis Gemini en fallback.
        """
        errors: list[str] = []
        _max_tokens = max_tokens or self.max_tokens

        # Primary provider: OpenAI
        if self._openai:
            for attempt in range(self.max_retries + 1):
                try:
                    kwargs: dict[str, Any] = {
                        "model": self.primary_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": self.temperature,
                        "max_tokens": _max_tokens,
                    }
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}

                    resp = self._openai.chat.completions.create(**kwargs)
                    self.last_provider_used = "openai"
                    self.last_model_used = self.primary_model
                    return resp.choices[0].message.content or ""

                except Exception as e:
                    err_str = str(e)
                    errors.append(f"openai[{attempt}]: {err_str[:120]}")
                    if attempt < self.max_retries:
                        time.sleep(self.retry_base * (2 ** attempt))

        # Fallback provider: Gemini
        if self._gemini:
            for attempt in range(self.max_retries + 1):
                try:
                    full_prompt = f"{system_prompt}\n\n{user_prompt}"
                    config: dict[str, Any] = {
                        "temperature": self.temperature,
                        "max_output_tokens": _max_tokens,
                    }
                    if json_mode:
                        config["response_mime_type"] = "application/json"

                    resp = self._gemini.models.generate_content(
                        model=self.fallback_model,
                        contents=full_prompt,
                        config=config,
                    )
                    self.last_provider_used = "gemini"
                    self.last_model_used = self.fallback_model
                    return resp.text or ""

                except Exception as e:
                    errors.append(f"gemini[{attempt}]: {str(e)[:120]}")
                    if attempt < self.max_retries:
                        time.sleep(self.retry_base * (2 ** attempt))

        raise RuntimeError(
            f"Tous les providers LLM ont echoue:\n" + "\n".join(errors)
        )

    def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Appelle le LLM et parse la reponse JSON."""
        raw = self.call(system_prompt, user_prompt, json_mode=True, max_tokens=max_tokens)
        # Nettoyer les eventuels blocs markdown.
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw)


# Instance partagee (singleton leger).
_client: AgentLLMClient | None = None


def get_llm_client() -> AgentLLMClient:
    global _client
    if _client is None:
        _client = AgentLLMClient()
    return _client
