from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from a1_prompts import SYSTEM_PROMPT_A1
from a1_schemas import RequirementLLMResponse


PROMPT_CONTRACT_VERSION = "B2.2-1.0.1"
OUTPUT_SCHEMA_VERSION = "B2.2-schema-1.0.1"
PROMPT_SYMBOL = "SYSTEM_PROMPT_A1"
OUTPUT_SCHEMA_SYMBOL = "RequirementLLMResponse"
OUTPUT_SCHEMA_NAME = "requirement_llm_response"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

        
def _sha256_json(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strictify_json_schema(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            props = node.get("properties")
            if isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props.keys())

        for key, value in node.items():
            if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}:
                if isinstance(value, dict):
                    for child in value.values():
                        _strictify_json_schema(child)
                continue

            if key in {"items", "contains", "if", "then", "else", "not"}:
                _strictify_json_schema(value)
                continue

            if key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(value, list):
                for child in value:
                    _strictify_json_schema(child)
                continue

        return

    if isinstance(node, list):
        for child in node:
            _strictify_json_schema(child)


@dataclass(frozen=True)
class PromptContract:
    contract_version: str
    prompt_symbol: str
    prompt_text: str
    prompt_sha256: str
    schema_symbol: str
    output_schema_name: str
    output_schema_version: str
    output_schema: dict[str, Any]
    output_schema_json: str
    output_schema_sha256: str
    strict_json_mode: bool = True

    def build_user_prompt(
        self,
        *,
        article_label: str,
        chunk_text: str,
        include_system_prompt: bool,
        fewshot_suffix: str = "",
    ) -> str:
        # Phase 6: fewshot_suffix injecté après le prompt système, avant l'instruction extraction
        if include_system_prompt:
            system_block = self.prompt_text + (fewshot_suffix or "") + "\n\n"
        else:
            system_block = ""
        return f"""
{system_block}
Contrat extraction : {self.contract_version}
Schema output version : {self.output_schema_version}
Schema output sha256 : {self.output_schema_sha256}

Reference juridique : {article_label}

Texte a analyser :
\"\"\"
{chunk_text}
\"\"\"

Reponds UNIQUEMENT avec un JSON valide conforme a ce schema :
{self.output_schema_json}
""".strip()


@lru_cache(maxsize=1)
def get_prompt_contract() -> PromptContract:
    prompt_text = SYSTEM_PROMPT_A1 or ""
    output_schema = deepcopy(RequirementLLMResponse.model_json_schema())
    _strictify_json_schema(output_schema)

    output_schema_json = json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))

    return PromptContract(
        contract_version=PROMPT_CONTRACT_VERSION,
        prompt_symbol=PROMPT_SYMBOL,
        prompt_text=prompt_text,
        prompt_sha256=_sha256_text(prompt_text),
        schema_symbol=OUTPUT_SCHEMA_SYMBOL,
        output_schema_name=OUTPUT_SCHEMA_NAME,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        output_schema=output_schema,
        output_schema_json=output_schema_json,
        output_schema_sha256=_sha256_json(output_schema),
        strict_json_mode=True,
    )
