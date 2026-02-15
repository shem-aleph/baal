"""Agent CRUD and deployment orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from baal_core.deployer import AlephDeployer
from baal_core.encryption import encrypt
from baal_core.models import AVAILABLE_MODELS
from liberclaw.database.models import Agent, DeploymentHistory
from liberclaw.services.activity import emit_activity

logger = logging.getLogger(__name__)


async def _check_agent_health(health_check, vm_url, max_attempts=12, delay=10):
    """Check agent health with retries."""
    for attempt in range(max_attempts):
        await asyncio.sleep(delay)
        healthy = await health_check(vm_url)
        if healthy:
            return True
    return False


async def create_agent(
    db: AsyncSession,
    owner_id: uuid.UUID,
    name: str,
    system_prompt: str,
    model: str,
    encryption_key: str,
    skills: list[str] | None = None,
) -> Agent:
    """Create a new agent record with an encrypted auth token."""
    if model not in AVAILABLE_MODELS:
        raise ValueError(f"Unknown model: {model}")

    agent_secret = secrets.token_urlsafe(32)
    encrypted_secret = encrypt(agent_secret, encryption_key)

    agent = Agent(
        owner_id=owner_id,
        name=name,
        system_prompt=system_prompt,
        model=model,
        auth_token=encrypted_secret,
        deployment_status="pending",
        source="web",
        skills=skills,  # Now stored as native JSON, not string
    )
    db.add(agent)
    try:
        await db.flush()
    except IntegrityError as e:
        if "uq_agents_owner_id_name" in str(e):
            raise ValueError(f"Agent name '{name}' already exists") from e
        raise
    return agent


async def list_agents(
    db: AsyncSession, 
    owner_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    sort: str = "created_at",
    order: str = "desc"
) -> tuple[list[Agent], int]:
    """List agents for a user with pagination, filtering, and sorting."""
    # Build base query
    query = select(Agent).where(Agent.owner_id == owner_id)
    
    # Add status filter if provided
    if status:
        query = query.where(Agent.deployment_status == status)
    
    # Add sorting
    sort_column = getattr(Agent, sort, Agent.created_at)
    if order.lower() == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())
    
    # Get total count for pagination info
    count_query = select(sa.func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Apply pagination
    query = query.limit(limit).offset(offset)
    
    # Execute query
    result = await db.execute(query)
    agents = list(result.scalars().all())
    
    return agents, total


async def get_agent(
    db: AsyncSession, agent_id: uuid.UUID, owner_id: uuid.UUID
) -> Agent | None:
    """Get an agent by ID, verifying ownership."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.owner_id == owner_id)
    )
    return result.scalar_one_or_none()


async def update_agent(
    db: AsyncSession,
    agent: Agent,
    name: str | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    skills: list[str] | None = None,
) -> Agent:
    """Update agent configuration fields."""
    if name is not None:
        agent.name = name
    if system_prompt is not None:
        agent.system_prompt = system_prompt
    if model is not None:
        if model not in AVAILABLE_MODELS:
            raise ValueError(f"Unknown model: {model}")
        agent.model = model
    if skills is not None:
        agent.skills = skills  # Now stored as native JSON, not string
    await db.flush()
    return agent


async def delete_agent(
    db: AsyncSession, agent: Agent, deployer: AlephDeployer, vm_pool=None
) -> None:
    """Delete an agent and destroy its VM."""
    if not agent.instance_hash:
        await db.delete(agent)
        return

    # Clean up pool entry if this VM came from the pool
    if vm_pool:
        try:
            await vm_pool.remove_by_instance(agent.instance_hash)
        except Exception as e:
            logger.warning(f"Pool cleanup failed for {agent.instance_hash}: {e}")
    
    # Destroy the VM
    try:
        await deployer.destroy_instance(agent.instance_hash)
    except Exception as e:
        logger.error(f"Failed to destroy VM for agent {agent.id}: {e}")
    
    await db.delete(agent)


async def _setup_pool_vm(agent_id, agent, pooled_vm, db, set_step, add_log):
    """Set up agent record with pooled VM details."""
    add_log(agent_id, "info", "Claimed pre-provisioned VM from pool")
    set_step(agent_id, "provisioning", "done",
             f"VM claimed from pool ({pooled_vm.instance_hash[:12]}...)")
    set_step(agent_id, "allocation", "done",
             f"VM already online at {pooled_vm.vm_ip}:{pooled_vm.ssh_port}")

    agent.instance_hash = pooled_vm.instance_hash
    agent.crn_url = pooled_vm.crn_url
    await db.commit()


async def _deploy_to_pooled_vm(
    pooled_vm, agent, deployer, libertai_api_key, agent_secret, subdomain
):
    """Deploy agent code to the pooled VM."""
    agent_skills = agent.skills  # Now native JSON
    fqdn = f"{subdomain}.2n6.me"

    return await deployer.deploy_agent_code(
        vm_ip=pooled_vm.vm_ip,
        ssh_port=pooled_vm.ssh_port,
        fqdn=fqdn,
        agent_name=agent.name,
        system_prompt=agent.system_prompt,
        model=agent.model,
        libertai_api_key=libertai_api_key,
        agent_secret=agent_secret,
        owner_chat_id=str(agent.owner_id),
        skills=agent_skills,
    )


async def _finalize_pool_deployment(
    agent_id, agent, pooled_vm, vm_pool, vm_url, healthy, duration, db
):
    """Finalize pool deployment with appropriate status."""
    if healthy:
        agent.vm_url = vm_url
        agent.deployment_status = "running"
        await vm_pool.mark_deployed(pooled_vm.id, agent.id)
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="success",
            step="pool_complete", duration_seconds=duration,
        ))
        
        await emit_activity(
            db, "agent_deployed",
            user_id=agent.owner_id, agent_id=agent.id,
            metadata={
                "agent_name": agent.name, "crn_url": agent.crn_url,
                "model": agent.model, "pool": True
            },
            is_public=True,
        )
    else:
        agent.vm_url = vm_url
        agent.deployment_status = "failed"
        await vm_pool.mark_deployed(pooled_vm.id, agent.id)
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="failed",
            step="health_check",
            error_message="Agent not responding after 30s (pool)",
            duration_seconds=duration,
        ))

    await db.commit()


async def _try_pool_deploy(
    agent_id: uuid.UUID,
    agent,
    pooled_vm,
    vm_pool,
    deployer: AlephDeployer,
    libertai_api_key: str,
    agent_secret: str,
    deploy_start: float,
    db,
    set_step,
    add_log,
    health_check,
) -> bool:
    """Attempt to deploy an agent using a pooled VM.

    Returns True if deployment completed (success or health-check failure).
    Returns False if we should fall back to cold provisioning.
    """
    await _setup_pool_vm(agent_id, agent, pooled_vm, db, set_step, add_log)

    # Look up subdomain
    subdomain = await deployer.lookup_subdomain(pooled_vm.instance_hash)
    if not subdomain:
        add_log(agent_id, "error", "Could not resolve subdomain for pooled VM")
        await vm_pool.release(pooled_vm.id)
        return False

    # Deploy code to the warm VM
    set_step(agent_id, "environment", "active",
             "Deploying agent code to pre-provisioned VM...")
    add_log(agent_id, "info", "Deploying code (fast path)...")

    deploy_result = await _deploy_to_pooled_vm(
        pooled_vm, agent, deployer, libertai_api_key, agent_secret, subdomain
    )

    if deploy_result.get("status") != "success":
        error = deploy_result.get("error", "Unknown error")
        add_log(agent_id, "error", f"Pool deploy failed: {error}")
        await vm_pool.release(pooled_vm.id)
        return False

    vm_url = deploy_result["vm_url"]
    set_step(agent_id, "environment", "done", "Code deployed")
    set_step(agent_id, "service", "done", f"HTTPS active at {subdomain}.2n6.me")

    # Health check
    set_step(agent_id, "health", "active", "Verifying agent is responding...")
    add_log(agent_id, "info", f"Checking health at {vm_url}/health...")

    healthy = await _check_agent_health(health_check, vm_url, max_attempts=6, delay=5)
    duration = int(time.monotonic() - deploy_start)

    if healthy:
        set_step(agent_id, "health", "done", f"Agent responding on {vm_url}")
        add_log(agent_id, "success", f"Pool deployment complete in {duration}s.")
    else:
        set_step(agent_id, "health", "failed", "Agent not responding after pool deploy")
        add_log(agent_id, "error", "Health check failed — agent deployed but not responding.")

    await _finalize_pool_deployment(
        agent_id, agent, pooled_vm, vm_pool, vm_url, healthy, duration, db
    )
    
    return True


async def _create_vm_instance(agent_id, agent, deployer, set_step, add_log, db):
    """Create VM instance on Aleph Cloud. Returns (instance_hash, crn_url) or (None, None) on failure."""
    set_step(agent_id, "provisioning", "active")
    add_log(agent_id, "info", "Discovering compute nodes...")

    create_result = await deployer.create_instance(agent.name)

    if create_result.get("status") != "success":
        error = create_result.get("error", "Unknown error")
        set_step(agent_id, "provisioning", "failed", error)
        add_log(agent_id, "error", f"Provisioning failed: {error}")
        agent.deployment_status = "failed"
        db.add(DeploymentHistory(
            agent_id=agent_id, status="failed",
            step="create_instance", error_message=error,
        ))
        await db.commit()
        return None, None

    instance_hash = create_result["instance_hash"]
    crn_url = create_result["crn_url"]
    agent.instance_hash = instance_hash
    agent.crn_url = crn_url
    await db.commit()

    set_step(agent_id, "provisioning", "done",
             f"VM created (instance: {instance_hash[:12]}...)")
    add_log(agent_id, "success",
            f"Instance {instance_hash[:12]}... created on CRN")
    
    return instance_hash, crn_url


async def _wait_for_vm_allocation(agent_id, instance_hash, crn_url, deployer, set_step, add_log, db):
    """Wait for VM allocation. Returns (vm_ip, ssh_port) or (None, None) on failure."""
    set_step(agent_id, "allocation", "active", "Waiting for VM to come online...")
    add_log(agent_id, "info", "Polling for VM allocation...")

    alloc = await deployer.wait_for_allocation(instance_hash, crn_url)

    if not alloc:
        set_step(agent_id, "allocation", "failed", "Allocation timed out after 120s")
        add_log(agent_id, "error", "VM allocation timed out")
        db.add(DeploymentHistory(
            agent_id=agent_id, status="failed",
            step="wait_allocation", error_message="Allocation timed out",
        ))
        await db.commit()
        return None, None

    vm_ip = alloc["vm_ipv4"]
    ssh_port = alloc.get("ssh_port", 22)

    set_step(agent_id, "allocation", "done", f"VM online at {vm_ip}:{ssh_port}")
    add_log(agent_id, "success", f"VM allocated: {vm_ip}:{ssh_port}")
    
    return vm_ip, ssh_port


async def _finalize_deployment(agent_id, agent, vm_url, healthy, duration, db, set_step, add_log):
    """Finalize deployment with appropriate status and history."""
    agent.vm_url = vm_url
    
    if healthy:
        set_step(agent_id, "health", "done", f"Agent responding on {vm_url}")
        add_log(agent_id, "success", f"Health check passed. Deployment complete in {duration}s.")
        agent.deployment_status = "running"
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="success",
            step="complete", duration_seconds=duration,
        ))
        await emit_activity(
            db, "agent_deployed",
            user_id=agent.owner_id, agent_id=agent.id,
            metadata={
                "agent_name": agent.name, 
                "crn_url": agent.crn_url, 
                "model": agent.model
            },
            is_public=True,
        )
    else:
        set_step(agent_id, "health", "failed", "Agent is not responding after deployment")
        add_log(agent_id, "error", "Health check failed — agent deployed but not responding. Use Rebuild to retry.")
        agent.deployment_status = "failed"
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="failed",
            step="health_check", error_message="Agent not responding after 120s",
            duration_seconds=duration,
        ))

    await db.commit()


async def _finalize_redeploy(agent_id, agent, healthy, duration, db, set_step, add_log):
    """Finalize redeploy with appropriate status."""
    if healthy:
        set_step(agent_id, "health", "done", f"Agent responding on {agent.vm_url}")
        add_log(agent_id, "success", f"Redeploy complete in {duration}s.")
        agent.deployment_status = "running"
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="success",
            step="redeploy_complete", duration_seconds=duration,
        ))
        await emit_activity(
            db, "agent_redeployed",
            user_id=agent.owner_id, agent_id=agent.id,
            metadata={"agent_name": agent.name},
            is_public=True,
        )
    else:
        set_step(agent_id, "health", "failed", "Agent not responding after redeploy")
        add_log(agent_id, "error", "Health check failed after redeploy. Use Rebuild to retry.")
        agent.deployment_status = "failed"
        
        db.add(DeploymentHistory(
            agent_id=agent_id, status="failed",
            step="health_check", error_message="Agent not responding after redeploy",
            duration_seconds=duration,
        ))

    await db.commit()


async def deploy_agent_background(
    agent_id: uuid.UUID,
    deployer: AlephDeployer,
    libertai_api_key: str,
    encryption_key: str,
    db_factory,
    vm_pool=None,
) -> None:
    """Background task: deploy an agent to Aleph Cloud.

    Creates its own DB session since this runs outside request lifecycle.
    Updates the in-memory progress store for real-time frontend tracking.
    """
    from baal_core.encryption import decrypt
    from baal_core.proxy import health_check

    from liberclaw.services.deployment_progress import (
        add_log,
        clear_progress,
        init_progress,
        set_step,
    )

    deploy_start = time.monotonic()
    init_progress(agent_id)

    def on_deploy_progress(step_key: str, status: str, detail: str) -> None:
        """Callback from deployer.deploy_agent() for sub-step updates."""
        set_step(agent_id, step_key, status, detail)
        level = "success" if status == "done" else "error" if status == "failed" else "info"
        add_log(agent_id, level, detail)

    async with db_factory() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            logger.error(f"Agent {agent_id} not found for deployment")
            clear_progress(agent_id)
            return

        agent.deployment_status = "deploying"
        await db.commit()

        pooled_vm = None
        try:
            agent_secret = decrypt(agent.auth_token, encryption_key)

            # ── Fast path: claim a pre-provisioned VM from the pool ────
            pooled_vm = await vm_pool.claim() if vm_pool else None

            if pooled_vm:
                pool_success = await _try_pool_deploy(
                    agent_id, agent, pooled_vm, vm_pool, deployer,
                    libertai_api_key, agent_secret, deploy_start,
                    db, set_step, add_log, health_check,
                )
                if pool_success:
                    return  # Done — pool fast path complete

                # Pool deploy failed — reset state for cold path
                agent.instance_hash = None
                agent.crn_url = None
                await db.commit()
                set_step(agent_id, "provisioning", "pending")
                set_step(agent_id, "allocation", "pending")
                add_log(agent_id, "info", "Falling back to cold provisioning...")

            # ── Step 1: Infrastructure Provisioning (cold path) ────────
            instance_hash, crn_url = await _create_vm_instance(
                agent_id, agent, deployer, set_step, add_log, db
            )
            if not instance_hash:
                return

            # ── Step 2: Network Allocation ─────────────────────────────
            vm_ip, ssh_port = await _wait_for_vm_allocation(
                agent_id, instance_hash, crn_url, deployer, set_step, add_log, db
            )
            if not vm_ip:
                agent.deployment_status = "failed"
                return

            # ── Steps 3-5: Deploy agent (SSH → environment → service) ──
            # The deployer calls on_deploy_progress for sub-step updates
            add_log(agent_id, "info", "Starting agent deployment...")

            # Get skills from agent record (now native JSON)
            agent_skills = agent.skills

            deploy_result = await deployer.deploy_agent(
                vm_ip=vm_ip,
                ssh_port=ssh_port,
                agent_name=agent.name,
                system_prompt=agent.system_prompt,
                model=agent.model,
                libertai_api_key=libertai_api_key,
                agent_secret=agent_secret,
                instance_hash=instance_hash,
                owner_chat_id=str(agent.owner_id),
                on_progress=on_deploy_progress,
                skills=agent_skills,
            )

            if deploy_result.get("status") != "success":
                error = deploy_result.get("error", "Unknown error")
                add_log(agent_id, "error", f"Deployment failed: {error}")
                agent.deployment_status = "failed"
                duration = int(time.monotonic() - deploy_start)
                db.add(DeploymentHistory(
                    agent_id=agent_id, status="failed",
                    step="deploy_agent", error_message=error,
                    duration_seconds=duration,
                ))
                await db.commit()
                return

            vm_url = deploy_result["vm_url"]

            # ── Step 6: Health Check ───────────────────────────────────
            set_step(agent_id, "health", "active", "Verifying agent is responding...")
            add_log(agent_id, "info", f"Checking health at {vm_url}/health...")

            healthy = await _check_agent_health(health_check, vm_url)
            duration = int(time.monotonic() - deploy_start)

            await _finalize_deployment(
                agent_id, agent, vm_url, healthy, duration, db, set_step, add_log
            )

        except Exception as e:
            logger.error(f"Deployment failed for agent {agent_id}: {e}", exc_info=True)
            add_log(agent_id, "error", f"Unexpected error: {e}")
            agent.deployment_status = "failed"
            db.add(DeploymentHistory(
                agent_id=agent_id, status="failed",
                step="unexpected_error", error_message=str(e),
            ))
            await db.commit()
        finally:
            # Keep progress in store briefly so the final poll can see the result
            # (the frontend will stop polling once it sees "running" or "failed")
            await asyncio.sleep(10)
            clear_progress(agent_id)


async def redeploy_agent_background(
    agent_id: uuid.UUID,
    deployer: AlephDeployer,
    libertai_api_key: str,
    encryption_key: str,
    db_factory,
) -> None:
    """Background task: push updated code/env/skills to an existing VM.

    Unlike deploy_agent_background(), this does NOT create a new instance.
    It SSHes into the running VM and updates in place.
    """
    from baal_core.encryption import decrypt
    from baal_core.proxy import health_check

    from liberclaw.services.deployment_progress import (
        add_log,
        clear_progress,
        init_progress,
        set_step,
    )

    deploy_start = time.monotonic()
    init_progress(agent_id)

    async with db_factory() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            logger.error(f"Agent {agent_id} not found for redeploy")
            clear_progress(agent_id)
            return

        if not agent.vm_url or not agent.instance_hash or not agent.crn_url:
            logger.error(f"Agent {agent_id} has no existing VM to redeploy to")
            add_log(agent_id, "error", "No existing VM — use Repair instead")
            agent.deployment_status = "failed"
            await db.commit()
            clear_progress(agent_id)
            return

        agent.deployment_status = "deploying"
        await db.commit()

        try:
            agent_secret = decrypt(agent.auth_token, encryption_key)
            agent_skills = agent.skills  # Now native JSON
            fqdn = agent.vm_url.replace("https://", "").replace("http://", "")

            # ── Step 1: Find existing VM ─────────────────────────────────
            set_step(agent_id, "allocation", "active",
                     "Locating existing VM...")
            add_log(agent_id, "info", "Looking up VM allocation...")

            alloc = await deployer.wait_for_allocation(
                agent.instance_hash, agent.crn_url, retries=3, delay=5,
            )
            if not alloc:
                set_step(agent_id, "allocation", "failed",
                         "Could not reach existing VM")
                add_log(agent_id, "error",
                        "VM not reachable — it may have been destroyed. Use Rebuild instead.")
                agent.deployment_status = "failed"
                db.add(DeploymentHistory(
                    agent_id=agent_id, status="failed",
                    step="allocation",
                    error_message="Existing VM not reachable",
                ))
                await db.commit()
                return

            vm_ip = alloc["vm_ipv4"]
            ssh_port = alloc.get("ssh_port", 22)
            set_step(agent_id, "allocation", "done",
                     f"VM found at {vm_ip}:{ssh_port}")
            add_log(agent_id, "success", f"VM located: {vm_ip}:{ssh_port}")

            # ── Step 2: Push code + env + skills ─────────────────────────
            set_step(agent_id, "environment", "active",
                     "Pushing updated code and configuration...")
            add_log(agent_id, "info", "Deploying code update...")

            deploy_result = await deployer.deploy_agent_code(
                vm_ip=vm_ip,
                ssh_port=ssh_port,
                fqdn=fqdn,
                agent_name=agent.name,
                system_prompt=agent.system_prompt,
                model=agent.model,
                libertai_api_key=libertai_api_key,
                agent_secret=agent_secret,
                owner_chat_id=str(agent.owner_id),
                skills=agent_skills,
            )

            if deploy_result.get("status") != "success":
                error = deploy_result.get("error", "Unknown error")
                set_step(agent_id, "environment", "failed", error)
                add_log(agent_id, "error", f"Redeploy failed: {error}")
                agent.deployment_status = "failed"
                duration = int(time.monotonic() - deploy_start)
                db.add(DeploymentHistory(
                    agent_id=agent_id, status="failed",
                    step="redeploy_code", error_message=error,
                    duration_seconds=duration,
                ))
                await db.commit()
                return

            set_step(agent_id, "environment", "done", "Code updated")
            add_log(agent_id, "success", "Code and configuration pushed")

            # ── Step 3: Health Check ─────────────────────────────────────
            set_step(agent_id, "health", "active", "Verifying agent is responding...")
            add_log(agent_id, "info", f"Checking health at {agent.vm_url}/health...")

            healthy = await _check_agent_health(health_check, agent.vm_url, max_attempts=6, delay=5)
            duration = int(time.monotonic() - deploy_start)

            await _finalize_redeploy(agent_id, agent, healthy, duration, db, set_step, add_log)

        except Exception as e:
            logger.error(f"Redeploy failed for agent {agent_id}: {e}", exc_info=True)
            add_log(agent_id, "error", f"Unexpected error: {e}")
            agent.deployment_status = "failed"
            db.add(DeploymentHistory(
                agent_id=agent_id, status="failed",
                step="unexpected_error", error_message=str(e),
            ))
            await db.commit()
        finally:
            await asyncio.sleep(10)
            clear_progress(agent_id)
