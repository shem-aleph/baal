"""Request/response schemas for agent endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    system_prompt: str | None = Field(None, min_length=1, max_length=10000)
    model: str | None = None
    template_id: str | None = None
    skills: list[str] | None = None
    
    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9\s\-_]+$", v):
            raise ValueError("Name must contain only alphanumeric characters, spaces, hyphens, and underscores")
        return v
    
    @field_validator("model")
    @classmethod 
    def validate_model(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from baal_core.models import AVAILABLE_MODELS
        if v not in AVAILABLE_MODELS:
            valid_models = ", ".join(AVAILABLE_MODELS.keys())
            raise ValueError(f"Model must be one of: {valid_models}")
        return v


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    system_prompt: str | None = Field(None, min_length=1, max_length=10000)
    model: str | None = None
    skills: list[str] | None = None
    
    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.match(r"^[a-zA-Z0-9\s\-_]+$", v):
            raise ValueError("Name must contain only alphanumeric characters, spaces, hyphens, and underscores")
        return v
    
    @field_validator("model")
    @classmethod 
    def validate_model(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from baal_core.models import AVAILABLE_MODELS
        if v not in AVAILABLE_MODELS:
            valid_models = ", ".join(AVAILABLE_MODELS.keys())
            raise ValueError(f"Model must be one of: {valid_models}")
        return v


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    system_prompt: str
    model: str
    deployment_status: str
    vm_url: str | None
    source: str
    skills: list[str] | None = None
    created_at: datetime
    updated_at: datetime

    # Skills is now stored as native JSON, no parsing needed

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    agents: list[AgentResponse]
    total: int
    limit: int
    offset: int


class DeploymentStepResponse(BaseModel):
    key: str
    status: str  # pending | active | done | failed
    detail: str | None = None


class DeploymentLogEntry(BaseModel):
    timestamp: float
    level: str  # info | success | error | warning
    message: str


class DeploymentStatusResponse(BaseModel):
    agent_id: uuid.UUID
    deployment_status: str
    vm_url: str | None
    steps: list[DeploymentStepResponse] = []
    logs: list[DeploymentLogEntry] = []


class AgentHealthResponse(BaseModel):
    agent_id: uuid.UUID
    healthy: bool
    vm_url: str | None
    agent_version: int | None = None
    current_version: int | None = None


class AgentExport(BaseModel):
    """Portable agent configuration for import/export."""
    name: str
    system_prompt: str
    model: str
    skills: list[str] | None = None
    export_version: int = 1
