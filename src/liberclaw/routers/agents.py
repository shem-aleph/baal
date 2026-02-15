"""Agent CRUD and deployment routes."""

from __future__ import annotations

import asyncio
import uuid

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from baal_core.encryption import decrypt
from baal_core.proxy import health_check, health_check_detailed
from liberclaw.auth.dependencies import get_current_user, get_settings
from liberclaw.database.models import Agent, User
from liberclaw.database.session import get_db, get_session_factory
from liberclaw.schemas.agents import (
    AgentCreate,
    AgentExport,
    AgentHealthResponse,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
    BulkDeleteRequest,
    BulkDeleteResponse,
    DeploymentLogEntry,
    DeploymentStatusResponse,
    DeploymentStepResponse,
)
from liberclaw.services.activity import emit_activity
from liberclaw.services.agent_manager import (
    create_agent,
    delete_agent,
    deploy_agent_background,
    get_agent,
    list_agents,
    redeploy_agent_background,
    update_agent,
)
from liberclaw.services.usage_tracker import get_agent_count

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/pool/stats")
async def get_pool_stats(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Get VM pool statistics. Restricted to pro-tier users."""
    if user.tier != "pro":
        raise HTTPException(status_code=403, detail="Admin access required")
        
    pool = getattr(request.app.state, "vm_pool", None)
    if not pool:
        return {"enabled": False}
        
    stats = await pool.get_stats()
    return {"enabled": True, **stats}


@router.get("/", response_model=AgentListResponse)
async def list_user_agents(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    sort: str = "created_at", 
    order: str = "desc",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List agents owned by the current user with pagination, filtering, and sorting."""
    # Validate parameters
    limit = min(max(1, limit), 100)  # Clamp between 1 and 100
    offset = max(0, offset)
    
    valid_sorts = ["created_at", "name", "updated_at"]
    if sort not in valid_sorts:
        sort = "created_at"
    
    if order.lower() not in ["asc", "desc"]:
        order = "desc"
        
    agents, total = await list_agents(
        db, user.id, limit=limit, offset=offset, 
        status=status, sort=sort, order=order
    )
    
    return AgentListResponse(
        agents=[AgentResponse.model_validate(a) for a in agents],
        total=total,
        limit=limit,
        offset=offset,
    )


def _resolve_agent_config(body: AgentCreate):
    """Resolve agent configuration from template and request body."""
    system_prompt = body.system_prompt
    model = body.model
    skills = body.skills

    if body.template_id:
        from baal_core.templates.loader import get_template
        template = get_template(body.template_id)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown template: {body.template_id}",
            )
        
        system_prompt = system_prompt or template["system_prompt"]
        model = model or template["model"]
        skills = skills or template["skills"]

    if system_prompt is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="system_prompt is required when not using a template",
        )
    
    return system_prompt, model or "qwen3-coder-next", skills


def _validate_skills(skills):
    """Validate skills list if provided."""
    if not skills:
        return
        
    from baal_core.templates.loader import validate_skills
    invalid = validate_skills(skills)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown skills: {', '.join(invalid)}",
        )


def _create_deployer(settings):
    """Create AlephDeployer instance from settings."""
    from baal_core.deployer import AlephDeployer
    
    return AlephDeployer(
        private_key=settings.aleph_private_key,
        ssh_pubkey=settings.aleph_ssh_pubkey,
        ssh_privkey_path=settings.aleph_ssh_privkey_path,
    )


@router.post("/", response_model=AgentResponse, status_code=201)
async def create_user_agent(
    body: AgentCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new agent and start background deployment."""
    settings = get_settings()

    # Check agent limit
    agent_limit = settings.agent_limit(user.tier)
    count = await get_agent_count(db, user.id)
    if count >= agent_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent limit reached ({agent_limit})",
        )

    # Resolve configuration from template and request
    system_prompt, model, skills = _resolve_agent_config(body)
    _validate_skills(skills)

    # Create agent and emit activity
    try:
        agent = await create_agent(
            db, user.id, body.name, system_prompt, model,
            settings.encryption_key, skills=skills,
        )
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    await emit_activity(
        db, "agent_created",
        user_id=user.id, agent_id=agent.id,
        metadata={"agent_name": body.name},
    )
    await db.commit()

    # Launch background deployment
    deployer = _create_deployer(settings)
    vm_pool = getattr(request.app.state, "vm_pool", None)
    background_tasks.add_task(
        deploy_agent_background,
        agent_id=agent.id,
        deployer=deployer,
        libertai_api_key=settings.libertai_api_key,
        encryption_key=settings.encryption_key,
        db_factory=get_session_factory(),
        vm_pool=vm_pool,
    )

    return AgentResponse.model_validate(agent)


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_agents(
    body: BulkDeleteRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple agents at once. Returns which were deleted and which were not found."""
    settings = get_settings()
    deployer = _create_deployer(settings)
    vm_pool = getattr(request.app.state, "vm_pool", None)

    deleted = []
    not_found = []

    for agent_id in body.agent_ids:
        agent = await get_agent(db, agent_id, user.id)
        if not agent:
            not_found.append(agent_id)
            continue

        agent_name = agent.name
        await emit_activity(
            db, "agent_deleted",
            user_id=user.id,
            metadata={"agent_name": agent_name, "bulk": True},
            is_public=True,
        )
        await delete_agent(db, agent, deployer, vm_pool=vm_pool)
        deleted.append(agent_id)

    return BulkDeleteResponse(
        deleted=deleted,
        not_found=not_found,
        total_deleted=len(deleted),
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_user_agent(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent details."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_user_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent configuration."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    _validate_skills(body.skills)

    try:
        agent = await update_agent(
            db, agent, name=body.name,
            system_prompt=body.system_prompt, model=body.model,
            skills=body.skills,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    changes = [
        field for field in ("name", "system_prompt", "model", "skills") 
        if getattr(body, field) is not None
    ]
    
    if changes:
        await emit_activity(
            db, "agent_updated",
            user_id=user.id, agent_id=agent.id,
            metadata={"agent_name": agent.name, "changes": changes},
        )

    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=204)
async def delete_user_agent(
    agent_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an agent and destroy its VM."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    settings = get_settings()
    deployer = _create_deployer(settings)
    vm_pool = getattr(request.app.state, "vm_pool", None)
    
    agent_name = agent.name
    await emit_activity(
        db, "agent_deleted",
        user_id=user.id,
        metadata={"agent_name": agent_name},
        is_public=True,
    )
    
    await delete_agent(db, agent, deployer, vm_pool=vm_pool)


@router.get("/{agent_id}/health", response_model=AgentHealthResponse)
async def check_agent_health(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if an agent's VM is healthy."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    from baal_agent import AGENT_VERSION

    healthy = False
    agent_version = None
    if agent.vm_url:
        result = await health_check_detailed(agent.vm_url)
        healthy = result["healthy"]
        agent_version = result["agent_version"]

    return AgentHealthResponse(
        agent_id=agent.id,
        healthy=healthy,
        vm_url=agent.vm_url,
        agent_version=agent_version,
        current_version=AGENT_VERSION,
    )


@router.get("/{agent_id}/status", response_model=DeploymentStatusResponse)
async def get_deployment_status(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get deployment progress (poll during creation)."""
    from liberclaw.services.deployment_progress import get_progress

    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    progress = get_progress(agent.id)
    steps = []
    logs = []
    if progress:
        steps = [
            DeploymentStepResponse(
                key=s.key, status=s.status, detail=s.detail,
            )
            for s in progress.steps
        ]
        logs = [
            DeploymentLogEntry(
                timestamp=l.timestamp, level=l.level, message=l.message,
            )
            for l in progress.logs
        ]

    return DeploymentStatusResponse(
        agent_id=agent.id,
        deployment_status=agent.deployment_status,
        vm_url=agent.vm_url,
        steps=steps,
        logs=logs,
    )


@router.get("/{agent_id}/deploy/stream")
async def stream_deployment_status(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream of deployment progress updates.

    Uses event-driven broadcasting — no polling.  Sends the current snapshot
    immediately, then streams step/log/complete events as they happen.
    Falls back to a ``complete`` event if no deployment is in progress.
    """
    from liberclaw.services.deployment_progress import (
        deployment_broadcaster,
        get_progress,
    )

    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    async def event_generator():
        try:
            # Send current snapshot first so the client has immediate state
            progress = get_progress(agent_id)
            if progress:
                steps_data = [s.to_dict() for s in progress.steps]
                yield f"data: {json.dumps({'type': 'snapshot', 'steps': steps_data})}\n\n"
                for log in progress.logs:
                    yield f"data: {json.dumps({'type': 'log', **log.to_dict()})}\n\n"
            else:
                # No active deployment — send final status and close
                async with get_session_factory()() as fresh_db:
                    agent_fresh = await get_agent(fresh_db, agent_id, user.id)
                    status_val = agent_fresh.deployment_status if agent_fresh else "unknown"
                yield f"data: {json.dumps({'type': 'complete', 'deployment_status': status_val})}\n\n"
                return

            # Stream real-time updates via the broadcaster
            async for event in deployment_broadcaster.subscribe(agent_id):
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "complete":
                    return
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _cleanup_existing_vm(agent, deployer):
    """Clean up existing VM instance if it exists."""
    if not agent.instance_hash:
        return
        
    try:
        await deployer.destroy_instance(agent.instance_hash)
    except Exception:
        pass  # Best-effort cleanup; new deploy will proceed regardless
        
    agent.instance_hash = None
    agent.crn_url = None
    agent.vm_url = None


@router.post("/{agent_id}/rebuild", response_model=AgentResponse)
async def rebuild_agent(
    agent_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Destroy existing VM and deploy a fresh one from scratch."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.deployment_status not in ("failed", "pending", "running", "deploying"):
        raise HTTPException(
            status_code=400, 
            detail="Agent cannot be rebuilt in its current state"
        )

    settings = get_settings()
    deployer = _create_deployer(settings)

    # Destroy old VM first to avoid orphaned instances
    await _cleanup_existing_vm(agent, deployer)

    await emit_activity(
        db, "agent_rebuilt",
        user_id=user.id, agent_id=agent.id,
        metadata={"agent_name": agent.name},
        is_public=True,
    )
    
    agent.deployment_status = "pending"
    await db.commit()

    vm_pool = getattr(request.app.state, "vm_pool", None)
    background_tasks.add_task(
        deploy_agent_background,
        agent_id=agent.id,
        deployer=deployer,
        libertai_api_key=settings.libertai_api_key,
        encryption_key=settings.encryption_key,
        db_factory=get_session_factory(),
        vm_pool=vm_pool,
    )

    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/redeploy", response_model=AgentResponse)
async def redeploy_agent(
    agent_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Push latest code/config/skills to a running agent's VM (in-place update)."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.deployment_status not in ("running", "failed"):
        raise HTTPException(
            status_code=400, 
            detail="Agent is not in a redeployable state"
        )

    agent.deployment_status = "deploying"
    await db.commit()

    settings = get_settings()
    deployer = _create_deployer(settings)
    
    background_tasks.add_task(
        redeploy_agent_background,
        agent_id=agent.id,
        deployer=deployer,
        libertai_api_key=settings.libertai_api_key,
        encryption_key=settings.encryption_key,
        db_factory=get_session_factory(),
    )

    return AgentResponse.model_validate(agent)


# ── Export / Import ────────────────────────────────────────────────────


@router.get("/{agent_id}/export", response_model=AgentExport)
async def export_agent(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export agent configuration as portable JSON."""
    agent = await get_agent(db, agent_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentExport(
        name=agent.name,
        system_prompt=agent.system_prompt,
        model=agent.model,
        skills=agent.skills,
    )


@router.post("/import", response_model=AgentResponse, status_code=201)
async def import_agent(
    body: AgentExport,
    background_tasks: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import an agent from exported JSON and deploy it."""
    settings = get_settings()

    agent_limit = settings.agent_limit(user.tier)
    count = await get_agent_count(db, user.id)
    if count >= agent_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent limit reached ({agent_limit})",
        )

    agent = await create_agent(
        db, user.id, body.name, body.system_prompt, body.model,
        settings.encryption_key, skills=body.skills,
    )
    await emit_activity(
        db, "agent_imported",
        user_id=user.id, agent_id=agent.id,
        metadata={"agent_name": body.name},
    )
    await db.commit()

    deployer = _create_deployer(settings)
    vm_pool = getattr(request.app.state, "vm_pool", None)
    background_tasks.add_task(
        deploy_agent_background,
        agent_id=agent.id,
        deployer=deployer,
        libertai_api_key=settings.libertai_api_key,
        encryption_key=settings.encryption_key,
        db_factory=get_session_factory(),
        vm_pool=vm_pool,
    )

    return AgentResponse.model_validate(agent)
