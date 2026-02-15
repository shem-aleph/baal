"""Tests for Round 4 features: deployment SSE broadcaster, per-agent usage tracking."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from liberclaw.schemas.usage import AgentUsage, AgentUsageResponse
from liberclaw.services.deployment_progress import (
    DeploymentBroadcaster,
    StepInfo,
    LogEntry,
    init_progress,
    set_step,
    add_log,
    mark_complete,
    get_progress,
    clear_progress,
    deployment_broadcaster,
)


class TestDeploymentBroadcaster:
    """Test the event-driven deployment broadcaster."""

    @pytest.mark.asyncio
    async def test_subscriber_receives_events(self):
        """Test that a subscriber receives pushed events."""
        bc = DeploymentBroadcaster()
        agent_id = uuid.uuid4()
        received = []

        async def consumer():
            async for event in bc.subscribe(agent_id):
                received.append(event)
                if event.get("type") == "complete":
                    break

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)

        await bc.push(agent_id, {"type": "step", "step": {"key": "ssh", "status": "active"}})
        await bc.push(agent_id, {"type": "log", "message": "Connecting..."})
        await bc.push(agent_id, {"type": "complete", "deployment_status": "running"})

        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 3
        assert received[0]["type"] == "step"
        assert received[1]["type"] == "log"
        assert received[2]["type"] == "complete"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        """Test that multiple subscribers each get all events."""
        bc = DeploymentBroadcaster()
        agent_id = uuid.uuid4()
        counts = [0, 0]

        async def consumer(idx):
            async for event in bc.subscribe(agent_id):
                counts[idx] += 1
                if event.get("type") == "complete":
                    break

        t1 = asyncio.create_task(consumer(0))
        t2 = asyncio.create_task(consumer(1))
        await asyncio.sleep(0.05)

        await bc.push(agent_id, {"type": "step", "step": {"key": "ssh", "status": "done"}})
        await bc.push(agent_id, {"type": "complete", "deployment_status": "running"})

        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
        assert counts == [2, 2]

    @pytest.mark.asyncio
    async def test_different_agents_isolated(self):
        """Test that events for one agent don't leak to another."""
        bc = DeploymentBroadcaster()
        agent_a = uuid.uuid4()
        agent_b = uuid.uuid4()
        received_a = []

        async def consumer_a():
            async for event in bc.subscribe(agent_a):
                received_a.append(event)
                if event.get("type") == "complete":
                    break

        task = asyncio.create_task(consumer_a())
        await asyncio.sleep(0.05)

        # Push to agent_b — should not be seen by agent_a's subscriber
        await bc.push(agent_b, {"type": "step", "step": {"key": "ssh", "status": "active"}})

        # Push to agent_a — should be seen
        await bc.push(agent_a, {"type": "complete", "deployment_status": "running"})

        await asyncio.wait_for(task, timeout=2.0)
        assert len(received_a) == 1
        assert received_a[0]["type"] == "complete"


class TestDeploymentProgressStore:
    """Test the in-memory progress store with broadcasting."""

    def test_init_progress_creates_all_steps(self):
        agent_id = uuid.uuid4()
        progress = init_progress(agent_id)
        assert len(progress.steps) == 6
        assert all(s.status == "pending" for s in progress.steps)
        clear_progress(agent_id)

    def test_step_info_to_dict(self):
        step = StepInfo(key="ssh", status="active", detail="Connecting")
        d = step.to_dict()
        assert d == {"key": "ssh", "status": "active", "detail": "Connecting"}

    def test_log_entry_to_dict(self):
        entry = LogEntry(timestamp=1234567890.0, level="info", message="Hello")
        d = entry.to_dict()
        assert d == {"timestamp": 1234567890.0, "level": "info", "message": "Hello"}

    def test_get_and_clear_progress(self):
        agent_id = uuid.uuid4()
        assert get_progress(agent_id) is None
        init_progress(agent_id)
        assert get_progress(agent_id) is not None
        clear_progress(agent_id)
        assert get_progress(agent_id) is None


class TestPerAgentUsageSchemas:
    """Test the per-agent usage response schemas."""

    def test_agent_usage_schema(self):
        usage = AgentUsage(
            agent_id=str(uuid.uuid4()),
            agent_name="TestBot",
            message_count=42,
            total_tokens=1500,
        )
        assert usage.message_count == 42
        assert usage.agent_name == "TestBot"

    def test_agent_usage_response_schema(self):
        resp = AgentUsageResponse(
            agents=[
                AgentUsage(
                    agent_id=str(uuid.uuid4()),
                    agent_name="Bot1",
                    message_count=10,
                    total_tokens=500,
                ),
                AgentUsage(
                    agent_id=None,
                    agent_name="Unknown",
                    message_count=5,
                    total_tokens=0,
                ),
            ],
            period_days=30,
        )
        assert len(resp.agents) == 2
        assert resp.period_days == 30
        assert resp.agents[1].agent_id is None


class TestPerAgentUsageTracker:
    """Test the per-agent usage tracker query."""

    @pytest.mark.asyncio
    async def test_get_per_agent_usage_empty(self):
        """Test per-agent usage returns empty list for user with no events."""
        from liberclaw.services.usage_tracker import get_per_agent_usage

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        db.execute.return_value = mock_result

        result = await get_per_agent_usage(db, uuid.uuid4(), days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_per_agent_usage_with_data(self):
        """Test per-agent usage returns correctly formatted data."""
        from liberclaw.services.usage_tracker import get_per_agent_usage

        agent_id = uuid.uuid4()
        db = AsyncMock()

        mock_row = MagicMock()
        mock_row.agent_id = agent_id
        mock_row.agent_name = "TestBot"
        mock_row.message_count = 42
        mock_row.total_tokens = 1500

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        db.execute.return_value = mock_result

        result = await get_per_agent_usage(db, uuid.uuid4(), days=7)
        assert len(result) == 1
        assert result[0]["agent_name"] == "TestBot"
        assert result[0]["message_count"] == 42
        assert result[0]["total_tokens"] == 1500


class TestRecordEventInChat:
    """Test that chat endpoint records usage events."""

    def test_record_event_function_signature(self):
        """Verify record_event is importable and has correct signature."""
        from liberclaw.services.usage_tracker import record_event
        import inspect
        sig = inspect.signature(record_event)
        params = list(sig.parameters.keys())
        assert "db" in params
        assert "user_id" in params
        assert "event_type" in params
        assert "agent_id" in params

    def test_chat_router_imports_record_event(self):
        """Verify chat router imports record_event."""
        import liberclaw.routers.chat as chat_module
        assert hasattr(chat_module, "record_event")
