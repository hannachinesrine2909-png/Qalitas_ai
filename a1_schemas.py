from typing import List, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_REQ_TYPES = {
    "OBLIGATION",
    "INTERDICTION",
    "RESPONSABILITE",
    "EXCEPTION",
    "CONDITION",
    "DECLARATION",
    "CONTROLE",
    "REGISTRE",
    "AUTRE",
}


REQ_TYPE_ALIASES = {
    "EXEMPTION": "EXCEPTION",
    "RESPONSIBILITY": "RESPONSABILITE",
    "RESPONSABILITÉ": "RESPONSABILITE",
    "CONTROL": "CONTROLE",
    "REGISTER": "REGISTRE",
    "OTHER": "AUTRE",
    "DECLARATION ": "DECLARATION",
    "DECLARATIVE": "DECLARATION",
    "PROHIBITION": "INTERDICTION",
    "BAN": "INTERDICTION",
}


ALLOWED_NORMATIVE_STRENGTHS = {"IMPERATIF", "CONDITIONNEL", "FACULTATIF"}
ALLOWED_SOURCE_MODES = {
    "NON_PRECISE",
    "VERBATIM",
    "REFORMULE_LEGERE",
    "RECONSTRUCTION_CONTROLEE",
}

# req_types that are incompatible with FACULTATIF normative_strength
_IMPERATIF_ONLY_TYPES = {"OBLIGATION", "INTERDICTION", "RESPONSABILITE"}


class RequirementLLM(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_text: str = Field(min_length=1)
    req_type: Literal[
        "OBLIGATION",
        "INTERDICTION",
        "RESPONSABILITE",
        "EXCEPTION",
        "CONDITION",
        "DECLARATION",
        "CONTROLE",
        "REGISTRE",
        "AUTRE",
    ]
    normative_strength: Literal["IMPERATIF", "CONDITIONNEL", "FACULTATIF"] = Field(
        default="IMPERATIF",
        description=(
            "Force normative de la r\u00e8gle : "
            "IMPERATIF=doit/est tenu/interdit, "
            "CONDITIONNEL=si.../sous conditions, "
            "FACULTATIF=peut/devrait/recommand\u00e9"
        ),
    )
    legal_subject: str = Field(
        default="",
        description="Sujet juridique principal identifie dans la source.",
    )
    normative_verb: str = Field(
        default="",
        description="Verbe ou locution normative principale identifiee dans la source.",
    )
    action_object: str = Field(
        default="",
        description="Action ou objet juridique principal vise par la regle.",
    )
    condition_text: str = Field(
        default="",
        description="Condition juridique explicite associee a la regle, si presente.",
    )
    exception_text: str = Field(
        default="",
        description="Exception ou reserve explicite associee a la regle, si presente.",
    )
    source_mode: Literal[
        "NON_PRECISE",
        "VERBATIM",
        "REFORMULE_LEGERE",
        "RECONSTRUCTION_CONTROLEE",
    ] = Field(
        default="NON_PRECISE",
        description=(
            "Mode de formulation du requirement_text : "
            "VERBATIM, REFORMULE_LEGERE, RECONSTRUCTION_CONTROLEE ou NON_PRECISE."
        ),
    )

    @field_validator("requirement_text", mode="before")
    @classmethod
    def normalize_requirement_text(cls, v):
        if v is None:
            return ""
        value = str(v).replace("\u00a0", " ").strip()
        value = " ".join(value.split())
        return value

    @field_validator(
        "legal_subject",
        "normative_verb",
        "action_object",
        "condition_text",
        "exception_text",
        mode="before",
    )
    @classmethod
    def normalize_optional_text_fields(cls, v):
        if v is None:
            return ""
        value = str(v).replace("\u00a0", " ").strip()
        return " ".join(value.split())

    @field_validator("req_type", mode="before")
    @classmethod
    def normalize_req_type_value(cls, v):
        if v is None:
            return "AUTRE"

        value = str(v).strip().upper()
        value = REQ_TYPE_ALIASES.get(value, value)

        if value not in ALLOWED_REQ_TYPES:
            return "AUTRE"

        return value

    @field_validator("normative_strength", mode="before")
    @classmethod
    def normalize_normative_strength(cls, v):
        if v is None:
            return "IMPERATIF"
        value = str(v).strip().upper()
        if value not in ALLOWED_NORMATIVE_STRENGTHS:
            return "IMPERATIF"
        return value

    @field_validator("source_mode", mode="before")
    @classmethod
    def normalize_source_mode(cls, v):
        if v is None:
            return "NON_PRECISE"
        value = str(v).strip().upper()
        if value not in ALLOWED_SOURCE_MODES:
            return "NON_PRECISE"
        return value


class RequirementLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: List[RequirementLLM] = Field(default_factory=list)
