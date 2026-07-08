"""Typed spec models (frozen pydantic v2)."""
from __future__ import annotations
from typing import Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

Number = Union[int, float]

class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["boolean", "float", "int", "string"]
    min: Optional[Number] = None
    max: Optional[Number] = None

class BranchRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    if_: Optional[str] = Field(default=None, alias="if")
    then: Optional[dict] = None
    else_: Optional[dict] = Field(default=None, alias="else")

class StepModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    id: str
    type: Literal["bounded_invocation", "activity", "branch", "terminal"]
    schema_: Optional[dict[str, Union[FieldSpec, str]]] = Field(default=None, alias="schema")
    on_out_of_bounds: Optional[Literal["human_review", "reject", "clamp"]] = None
    activity: Optional[str] = None
    args: Optional[list[str]] = None
    rules: Optional[list[BranchRule]] = None

    @field_validator("schema_", mode="before")
    @classmethod
    def _coerce_schema(cls, v):
        if v is None:
            return v
        out = {}
        for k, fv in v.items():
            if isinstance(fv, str):
                out[k] = FieldSpec(type=fv)
            elif isinstance(fv, dict):
                out[k] = FieldSpec(**fv)
            else:
                out[k] = fv
        return out

class Metadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    owner: str
    compliance: list[str] = Field(default_factory=list)

class SpecModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    workflow_spec: str
    version: int
    metadata: Metadata
    inputs: dict[str, dict[str, str]]
    steps: list[StepModel]
    outputs: dict[str, dict[str, str]]

    @field_validator("workflow_spec")
    @classmethod
    def _snake(cls, v):
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError("workflow_spec must be snake_case")
        return v
