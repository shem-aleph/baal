"""Request/response schemas for usage endpoints."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class UsageSummary(BaseModel):
    daily_messages_used: int
    daily_messages_limit: int
    agent_count: int
    agent_limit: int
    tier: str


class UsageDay(BaseModel):
    date: date
    message_count: int


class UsageHistory(BaseModel):
    days: list[UsageDay]


class AgentUsage(BaseModel):
    agent_id: str | None
    agent_name: str
    message_count: int
    total_tokens: int


class AgentUsageResponse(BaseModel):
    agents: list[AgentUsage]
    period_days: int
