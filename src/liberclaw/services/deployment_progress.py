"""In-memory deployment progress store for real-time step tracking.

Background deployment tasks update this store as they progress.
The status endpoint reads from it to return structured step/log data.
Data is ephemeral — cleared when deployment finishes.

Includes an SSE broadcaster so clients can stream updates instead of polling.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field


STEP_KEYS = [
    "provisioning",
    "allocation",
    "ssh",
    "environment",
    "service",
    "health",
]


@dataclass
class StepInfo:
    key: str
    status: str = "pending"  # pending | active | done | failed
    detail: str | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "status": self.status, "detail": self.detail}


@dataclass
class LogEntry:
    timestamp: float
    level: str  # info | success | error | warning
    message: str

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "level": self.level, "message": self.message}


@dataclass
class DeploymentProgress:
    steps: list[StepInfo] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


class DeploymentBroadcaster:
    """Per-agent SSE broadcaster for deployment progress updates.

    Clients subscribe to a specific agent_id and receive events as they happen.
    """

    def __init__(self):
        self._queues: dict[uuid.UUID, list[asyncio.Queue]] = {}

    def _agent_queues(self, agent_id: uuid.UUID) -> list[asyncio.Queue]:
        if agent_id not in self._queues:
            self._queues[agent_id] = []
        return self._queues[agent_id]

    async def push(self, agent_id: uuid.UUID, event: dict) -> None:
        """Push an event to all subscribers for this agent."""
        for q in self._agent_queues(agent_id):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if consumer is slow

    async def subscribe(self, agent_id: uuid.UUID):
        """Async generator yielding events for a specific agent."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._agent_queues(agent_id).append(q)
        try:
            while True:
                event = await q.get()
                yield event
                if event.get("type") == "complete":
                    return
        finally:
            queues = self._queues.get(agent_id, [])
            if q in queues:
                queues.remove(q)
            if not queues:
                self._queues.pop(agent_id, None)


# Singletons
_store: dict[uuid.UUID, DeploymentProgress] = {}
deployment_broadcaster = DeploymentBroadcaster()


def init_progress(agent_id: uuid.UUID) -> DeploymentProgress:
    """Create a fresh progress entry with all steps pending."""
    progress = DeploymentProgress(
        steps=[StepInfo(key=k) for k in STEP_KEYS],
    )
    _store[agent_id] = progress
    return progress


def set_step(
    agent_id: uuid.UUID,
    key: str,
    status: str,
    detail: str | None = None,
) -> None:
    """Update a step's status and optional detail text."""
    progress = _store.get(agent_id)
    if not progress:
        return
    for step in progress.steps:
        if step.key == key:
            step.status = status
            if detail is not None:
                step.detail = detail
            break

    # Broadcast the step update
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            deployment_broadcaster.push(agent_id, {
                "type": "step",
                "step": {"key": key, "status": status, "detail": detail},
            })
        )
    except RuntimeError:
        pass  # No running loop (e.g. called from sync context)


def add_log(
    agent_id: uuid.UUID,
    level: str,
    message: str,
) -> None:
    """Append a timestamped log entry."""
    progress = _store.get(agent_id)
    if not progress:
        return
    entry = LogEntry(timestamp=time.time(), level=level, message=message)
    progress.logs.append(entry)

    # Broadcast the log entry
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            deployment_broadcaster.push(agent_id, {
                "type": "log",
                **entry.to_dict(),
            })
        )
    except RuntimeError:
        pass


def mark_complete(agent_id: uuid.UUID, final_status: str) -> None:
    """Signal that deployment is finished (success or failure)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            deployment_broadcaster.push(agent_id, {
                "type": "complete",
                "deployment_status": final_status,
            })
        )
    except RuntimeError:
        pass


def get_progress(agent_id: uuid.UUID) -> DeploymentProgress | None:
    """Get current progress for an agent, or None if not tracking."""
    return _store.get(agent_id)


def clear_progress(agent_id: uuid.UUID) -> None:
    """Remove progress data after deployment completes."""
    _store.pop(agent_id, None)
