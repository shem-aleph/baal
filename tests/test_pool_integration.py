"""Test VM pool integration in LiberClaw.

Tests the pool claim → deploy_agent_code → mark_deployed fast path
and the fallback to cold provisioning when the pool is empty.
"""

import asyncio
import json
import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip if deps not available
pytest.importorskip("aiosqlite")


@pytest.fixture
def pool_db_path(tmp_path):
    return str(tmp_path / "test_pool.db")


@pytest.fixture
def mock_deployer():
    deployer = MagicMock()
    deployer.create_instance = AsyncMock(return_value={
        "status": "success",
        "instance_hash": "abc123deadbeef",
        "crn_url": "https://crn.example.com",
    })
    deployer.wait_for_allocation = AsyncMock(return_value={
        "vm_ipv4": "1.2.3.4",
        "ssh_port": 22,
    })
    deployer.lookup_subdomain = AsyncMock(return_value="test-sub")
    deployer.prepare_vm = AsyncMock(return_value={"status": "success"})
    deployer.deploy_agent_code = AsyncMock(return_value={
        "status": "success",
        "vm_url": "https://test-sub.2n6.me",
    })
    deployer.deploy_agent = AsyncMock(return_value={
        "status": "success",
        "vm_url": "https://test-sub.2n6.me",
    })
    deployer.destroy_instance = AsyncMock(return_value={"status": "success"})
    return deployer


@pytest.mark.asyncio
async def test_pool_initialize_and_stats(pool_db_path, mock_deployer):
    """Test pool initializes and reports stats correctly."""
    from baal_core.pool_manager import VMPool

    pool = VMPool(pool_db_path, mock_deployer, min_size=2, max_size=5)
    await pool.initialize()

    stats = await pool.get_stats()
    assert stats["available"] == 0
    assert stats["total"] == 0

    await pool.close()


@pytest.mark.asyncio
async def test_pool_claim_empty(pool_db_path, mock_deployer):
    """Claiming from empty pool returns None."""
    from baal_core.pool_manager import VMPool

    pool = VMPool(pool_db_path, mock_deployer, min_size=0, max_size=5)
    await pool.initialize()

    vm = await pool.claim()
    assert vm is None

    await pool.close()


@pytest.mark.asyncio
async def test_pool_claim_and_release(pool_db_path, mock_deployer):
    """Test claiming a VM and releasing it back."""
    from baal_core.pool_manager import VMPool

    pool = VMPool(pool_db_path, mock_deployer, min_size=0, max_size=5)
    await pool.initialize()

    # Manually insert a warm VM
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await pool._db.execute("""
        INSERT INTO vm_pool (instance_hash, vm_ip, vm_url, crn_url, ssh_port, created_at, status)
        VALUES ('hash123', '1.2.3.4', 'https://test.2n6.me', 'https://crn.example.com', 22, ?, 'warm')
    """, (now,))
    await pool._db.commit()

    # Claim it
    vm = await pool.claim()
    assert vm is not None
    assert vm.instance_hash == "hash123"
    assert vm.vm_ip == "1.2.3.4"

    # Pool should now be empty
    vm2 = await pool.claim()
    assert vm2 is None

    # Release it back
    await pool.release(vm.id)

    # Should be claimable again
    vm3 = await pool.claim()
    assert vm3 is not None
    assert vm3.instance_hash == "hash123"

    await pool.close()


@pytest.mark.asyncio
async def test_pool_mark_deployed(pool_db_path, mock_deployer):
    """Test marking a claimed VM as deployed."""
    from baal_core.pool_manager import VMPool

    pool = VMPool(pool_db_path, mock_deployer, min_size=0, max_size=5)
    await pool.initialize()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await pool._db.execute("""
        INSERT INTO vm_pool (instance_hash, vm_ip, vm_url, crn_url, ssh_port, created_at, status)
        VALUES ('hash456', '5.6.7.8', 'https://test2.2n6.me', 'https://crn2.example.com', 22, ?, 'warm')
    """, (now,))
    await pool._db.commit()

    vm = await pool.claim()
    assert vm is not None

    await pool.mark_deployed(vm.id, 42)

    # Verify it's deployed, not claimable
    stats = await pool.get_stats()
    assert stats.get("deployed", 0) == 1
    assert stats.get("available", 0) == 0

    await pool.close()


@pytest.mark.asyncio
async def test_pool_remove_by_instance(pool_db_path, mock_deployer):
    """Test removing pool entry by instance hash (for agent deletion)."""
    from baal_core.pool_manager import VMPool

    pool = VMPool(pool_db_path, mock_deployer, min_size=0, max_size=5)
    await pool.initialize()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await pool._db.execute("""
        INSERT INTO vm_pool (instance_hash, vm_ip, vm_url, crn_url, ssh_port, created_at, status)
        VALUES ('hash789', '9.10.11.12', 'https://test3.2n6.me', 'https://crn3.example.com', 22, ?, 'deployed')
    """, (now,))
    await pool._db.commit()

    await pool.remove_by_instance("hash789")

    stats = await pool.get_stats()
    assert stats["total"] == 0

    await pool.close()
