"""
Integration tests for LiberClaw API.

Tests that hit the live API server at http://127.0.0.1:8000 using httpx.
Each test is independent and cleans up after itself.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
import pytest

# Test configuration
API_BASE = "http://127.0.0.1:8000/api/v1"
TIMEOUT = 30.0

class APITestClient:
    """HTTP client for API testing with auth support."""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=TIMEOUT)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()
    
    def _auth_headers(self) -> Dict[str, str]:
        """Get authorization headers if token is available."""
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}
    
    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an API request with optional auth."""
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        
        url = f"{API_BASE}{path}" if not path.startswith(API_BASE) else path
        return await self.client.request(method, url, headers=headers, **kwargs)
    
    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)
    
    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)
    
    async def patch(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("PATCH", path, **kwargs)
    
    async def delete(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)
    
    async def login_guest(self, device_id: str | None = None) -> Dict[str, Any]:
        """Login as guest and store tokens."""
        if device_id is None:
            device_id = str(uuid.uuid4())
        
        response = await self.post("/auth/guest", json={"device_id": device_id})
        assert response.status_code == 200
        
        data = response.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        return data
    
    async def refresh_tokens(self) -> Dict[str, Any]:
        """Refresh access token using stored refresh token."""
        assert self.refresh_token, "No refresh token available"
        
        response = await self.post("/auth/refresh", json={"refresh_token": self.refresh_token})
        assert response.status_code == 200
        
        data = response.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        return data


@pytest.fixture
def unique_device_id() -> str:
    """Generate a unique device ID for each test."""
    return f"test-device-{uuid.uuid4()}"


@pytest.fixture
def unique_agent_name() -> str:
    """Generate a unique agent name for each test."""
    return f"test-agent-{uuid.uuid4().hex[:8]}"


class TestAuthFlow:
    """Test authentication flow endpoints."""
    
    @pytest.mark.asyncio
    async def test_guest_login_creates_user_returns_tokens(self, unique_device_id):
        """Guest login creates user and returns valid tokens."""
        async with APITestClient() as client:
            response = await client.post("/auth/guest", json={"device_id": unique_device_id})
            
            assert response.status_code == 200
            data = response.json()
            
            # Check response structure
            assert "access_token" in data
            assert "refresh_token" in data
            assert data["token_type"] == "bearer"
            assert data["expires_in"] > 0
            
            # Verify tokens work by accessing protected endpoint
            client.access_token = data["access_token"]
            usage_response = await client.get("/usage")
            assert usage_response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_guest_login_same_device_returns_same_user(self, unique_device_id):
        """Guest login with same device_id returns same user."""
        async with APITestClient() as client1:
            # First login
            response1 = await client1.post("/auth/guest", json={"device_id": unique_device_id})
            assert response1.status_code == 200
            
            client1.access_token = response1.json()["access_token"]
            
            # Create an agent to verify identity
            agent_response = await client1.post("/agents", json={
                "name": "test-agent",
                "system_prompt": "You are a test agent.",
                "model": "qwen3-coder-next"
            })
            assert agent_response.status_code == 201
            agent_id = agent_response.json()["id"]
            
            # Second login with same device ID
            async with APITestClient() as client2:
                response2 = await client2.post("/auth/guest", json={"device_id": unique_device_id})
                assert response2.status_code == 200
                
                client2.access_token = response2.json()["access_token"]
                
                # Should see the same agent
                agents_response = await client2.get("/agents")
                assert agents_response.status_code == 200
                agents = agents_response.json()["agents"]
                assert len(agents) == 1
                assert agents[0]["id"] == agent_id
                
                # Cleanup
                await client2.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_token_refresh_works_with_valid_token(self, unique_device_id):
        """Token refresh works with valid refresh token."""
        async with APITestClient() as client:
            # Login and get tokens
            login_data = await client.login_guest(unique_device_id)
            original_access = client.access_token
            original_refresh = client.refresh_token
            
            # Wait a moment to ensure different token timestamps
            await asyncio.sleep(0.1)
            
            # Refresh tokens
            refresh_data = await client.refresh_tokens()
            
            # Should get new tokens
            assert refresh_data["access_token"] != original_access
            assert refresh_data["refresh_token"] != original_refresh
            
            # New tokens should work
            usage_response = await client.get("/usage")
            assert usage_response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_token_refresh_fails_with_invalid_token(self):
        """Token refresh fails with invalid refresh token."""
        async with APITestClient() as client:
            response = await client.post("/auth/refresh", json={
                "refresh_token": "invalid-token"
            })
            assert response.status_code == 401
    
    @pytest.mark.asyncio
    async def test_protected_endpoint_without_token_returns_401(self):
        """Accessing protected endpoint without token returns 401."""
        async with APITestClient() as client:
            response = await client.get("/agents")
            assert response.status_code == 401
    
    @pytest.mark.asyncio
    async def test_protected_endpoint_with_expired_token_returns_401(self):
        """Accessing protected endpoint with expired token returns 401."""
        async with APITestClient() as client:
            # Use a clearly invalid/expired token
            client.access_token = "expired.token.here"
            response = await client.get("/agents")
            assert response.status_code == 401


class TestAgentCRUD:
    """Test agent CRUD operations."""
    
    @pytest.mark.asyncio
    async def test_create_agent_with_valid_data_returns_201(self, unique_device_id, unique_agent_name):
        """Create agent with valid data returns 201."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            response = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "You are a helpful test agent.",
                "model": "qwen3-coder-next"
            })
            
            assert response.status_code == 201
            data = response.json()
            
            # Check response structure
            assert data["name"] == unique_agent_name
            assert data["system_prompt"] == "You are a helpful test agent."
            assert data["model"] == "qwen3-coder-next"
            assert data["deployment_status"] == "pending"
            assert "id" in data
            assert "created_at" in data
            assert "updated_at" in data
            
            # Cleanup
            await client.delete(f"/agents/{data['id']}")
    
    @pytest.mark.asyncio
    async def test_create_agent_with_invalid_name_returns_422(self, unique_device_id):
        """Create agent with invalid name returns 422."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Name with invalid characters
            response = await client.post("/agents", json={
                "name": "invalid@name#here",
                "system_prompt": "You are a test agent.",
                "model": "qwen3-coder-next"
            })
            
            assert response.status_code == 422
    
    @pytest.mark.asyncio
    async def test_create_agent_with_duplicate_name_returns_409(self, unique_device_id, unique_agent_name):
        """Create agent with duplicate name returns 409."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create first agent
            response1 = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "First agent.",
                "model": "qwen3-coder-next"
            })
            assert response1.status_code == 201
            agent_id = response1.json()["id"]
            
            # Try to create second agent with same name
            response2 = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "Second agent.",
                "model": "qwen3-coder-next"
            })
            
            assert response2.status_code == 409
            
            # Cleanup
            await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_list_agents_returns_owned_agents_only(self):
        """List agents returns owned agents only (user isolation)."""
        device_id_1 = str(uuid.uuid4())
        device_id_2 = str(uuid.uuid4())
        
        # User 1 creates an agent
        async with APITestClient() as client1:
            await client1.login_guest(device_id_1)
            
            response1 = await client1.post("/agents", json={
                "name": "user1-agent",
                "system_prompt": "User 1 agent.",
                "model": "qwen3-coder-next"
            })
            assert response1.status_code == 201
            agent1_id = response1.json()["id"]
        
        # User 2 creates an agent
        async with APITestClient() as client2:
            await client2.login_guest(device_id_2)
            
            response2 = await client2.post("/agents", json={
                "name": "user2-agent",
                "system_prompt": "User 2 agent.",
                "model": "qwen3-coder-next"
            })
            assert response2.status_code == 201
            agent2_id = response2.json()["id"]
            
            # User 2 should only see their own agent
            list_response = await client2.get("/agents")
            assert list_response.status_code == 200
            
            agents = list_response.json()["agents"]
            assert len(agents) == 1
            assert agents[0]["id"] == agent2_id
            assert agents[0]["name"] == "user2-agent"
        
        # Cleanup
        async with APITestClient() as client1:
            await client1.login_guest(device_id_1)
            await client1.delete(f"/agents/{agent1_id}")
        
        async with APITestClient() as client2:
            await client2.login_guest(device_id_2)
            await client2.delete(f"/agents/{agent2_id}")
    
    @pytest.mark.asyncio
    async def test_get_agent_by_id_works_for_owner(self, unique_device_id, unique_agent_name):
        """Get agent by ID works for owner."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agent
            create_response = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "Test agent for get.",
                "model": "qwen3-coder-next"
            })
            assert create_response.status_code == 201
            agent_id = create_response.json()["id"]
            
            # Get agent
            get_response = await client.get(f"/agents/{agent_id}")
            assert get_response.status_code == 200
            
            data = get_response.json()
            assert data["id"] == agent_id
            assert data["name"] == unique_agent_name
            assert data["system_prompt"] == "Test agent for get."
            
            # Cleanup
            await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_get_agent_by_nonexistent_id_returns_404(self, unique_device_id):
        """Get agent by non-existent ID returns 404."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            fake_id = str(uuid.uuid4())
            response = await client.get(f"/agents/{fake_id}")
            assert response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_update_agent_works(self, unique_device_id, unique_agent_name):
        """Update agent name/prompt/model works."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agent
            create_response = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "Original prompt.",
                "model": "qwen3-coder-next"
            })
            assert create_response.status_code == 201
            agent_id = create_response.json()["id"]
            
            # Update agent
            new_name = f"{unique_agent_name}-updated"
            update_response = await client.patch(f"/agents/{agent_id}", json={
                "name": new_name,
                "system_prompt": "Updated prompt.",
                "model": "glm-4.7"
            })
            assert update_response.status_code == 200
            
            data = update_response.json()
            assert data["name"] == new_name
            assert data["system_prompt"] == "Updated prompt."
            assert data["model"] == "glm-4.7"
            
            # Cleanup
            await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_delete_agent_returns_204(self, unique_device_id, unique_agent_name):
        """Delete agent returns 204."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agent
            create_response = await client.post("/agents", json={
                "name": unique_agent_name,
                "system_prompt": "Agent to delete.",
                "model": "qwen3-coder-next"
            })
            assert create_response.status_code == 201
            agent_id = create_response.json()["id"]
            
            # Delete agent
            delete_response = await client.delete(f"/agents/{agent_id}")
            assert delete_response.status_code == 204
            
            # Verify agent is gone
            get_response = await client.get(f"/agents/{agent_id}")
            assert get_response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_delete_nonexistent_agent_returns_404(self, unique_device_id):
        """Delete non-existent agent returns 404."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            fake_id = str(uuid.uuid4())
            response = await client.delete(f"/agents/{fake_id}")
            assert response.status_code == 404


class TestPaginationAndFiltering:
    """Test pagination and filtering of agent lists."""
    
    @pytest.mark.asyncio
    async def test_pagination_limit_and_total(self, unique_device_id):
        """Create 5 agents, test limit=2 returns 2 with total=5."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create 5 agents
            agent_ids = []
            for i in range(5):
                response = await client.post("/agents", json={
                    "name": f"agent-{i}",
                    "system_prompt": f"Agent number {i}.",
                    "model": "qwen3-coder-next"
                })
                assert response.status_code == 201
                agent_ids.append(response.json()["id"])
            
            # Test pagination
            response = await client.get("/agents?limit=2")
            assert response.status_code == 200
            
            data = response.json()
            assert len(data["agents"]) == 2
            assert data["total"] == 5
            assert data["limit"] == 2
            assert data["offset"] == 0
            
            # Cleanup
            for agent_id in agent_ids:
                await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_pagination_offset(self, unique_device_id):
        """Test offset=3 returns remaining agents."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create 5 agents
            agent_ids = []
            for i in range(5):
                response = await client.post("/agents", json={
                    "name": f"agent-{i}",
                    "system_prompt": f"Agent number {i}.",
                    "model": "qwen3-coder-next"
                })
                assert response.status_code == 201
                agent_ids.append(response.json()["id"])
            
            # Test offset
            response = await client.get("/agents?offset=3")
            assert response.status_code == 200
            
            data = response.json()
            assert len(data["agents"]) == 2  # 5 - 3 = 2 remaining
            assert data["total"] == 5
            assert data["offset"] == 3
            
            # Cleanup
            for agent_id in agent_ids:
                await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_status_filter(self, unique_device_id):
        """Test status filter (pending, running, etc.)."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agent (will be in pending status)
            response = await client.post("/agents", json={
                "name": "pending-agent",
                "system_prompt": "Pending agent.",
                "model": "qwen3-coder-next"
            })
            assert response.status_code == 201
            agent_id = response.json()["id"]
            
            # Filter by pending status
            response = await client.get("/agents?status=pending")
            assert response.status_code == 200
            
            data = response.json()
            assert data["total"] >= 1
            
            # All returned agents should have pending status
            for agent in data["agents"]:
                assert agent["deployment_status"] == "pending"
            
            # Filter by running status (should be empty)
            response = await client.get("/agents?status=running")
            assert response.status_code == 200
            
            data = response.json()
            # Should not find any running agents in test environment
            assert all(agent["deployment_status"] == "running" for agent in data["agents"])
            
            # Cleanup
            await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_sort_by_name_vs_created_at(self, unique_device_id):
        """Test sort by name vs created_at."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agents with specific names to test sorting
            agent_names = ["zebra", "alpha", "beta"]
            agent_ids = []
            
            for name in agent_names:
                response = await client.post("/agents", json={
                    "name": name,
                    "system_prompt": f"Agent {name}.",
                    "model": "qwen3-coder-next"
                })
                assert response.status_code == 201
                agent_ids.append(response.json()["id"])
                # Small delay to ensure different created_at timestamps
                await asyncio.sleep(0.01)
            
            # Test sort by name (ascending)
            response = await client.get("/agents?sort=name&order=asc")
            assert response.status_code == 200
            
            data = response.json()
            names = [agent["name"] for agent in data["agents"]]
            assert names == sorted(names)  # Should be alphabetically sorted
            
            # Test sort by created_at (descending - newest first)
            response = await client.get("/agents?sort=created_at&order=desc")
            assert response.status_code == 200
            
            data = response.json()
            created_times = [agent["created_at"] for agent in data["agents"]]
            # Convert to datetime objects for comparison
            dt_times = [datetime.fromisoformat(t.replace('Z', '+00:00')) for t in created_times]
            assert dt_times == sorted(dt_times, reverse=True)
            
            # Cleanup
            for agent_id in agent_ids:
                await client.delete(f"/agents/{agent_id}")
    
    @pytest.mark.asyncio
    async def test_order_asc_vs_desc(self, unique_device_id):
        """Test order asc vs desc."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Create agents
            agent_ids = []
            for i in range(3):
                response = await client.post("/agents", json={
                    "name": f"agent-{i:02d}",  # Zero-padded for proper string sorting
                    "system_prompt": f"Agent {i}.",
                    "model": "qwen3-coder-next"
                })
                assert response.status_code == 201
                agent_ids.append(response.json()["id"])
            
            # Test ascending order
            response = await client.get("/agents?sort=name&order=asc")
            assert response.status_code == 200
            asc_names = [agent["name"] for agent in response.json()["agents"]]
            
            # Test descending order
            response = await client.get("/agents?sort=name&order=desc")
            assert response.status_code == 200
            desc_names = [agent["name"] for agent in response.json()["agents"]]
            
            # Descending should be reverse of ascending
            assert desc_names == list(reversed(asc_names))
            
            # Cleanup
            for agent_id in agent_ids:
                await client.delete(f"/agents/{agent_id}")


class TestUsageAndTemplates:
    """Test usage and templates endpoints."""
    
    @pytest.mark.asyncio
    async def test_usage_endpoint_returns_correct_limits_for_guest_tier(self, unique_device_id):
        """Usage endpoint returns correct limits for guest tier."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            response = await client.get("/usage")
            assert response.status_code == 200
            
            data = response.json()
            assert "daily_messages_used" in data
            assert "daily_messages_limit" in data
            assert "agent_count" in data
            assert "agent_limit" in data
            assert data["tier"] == "guest"
            
            # Guest tier should have limited resources
            assert data["daily_messages_limit"] > 0
            assert data["agent_limit"] > 0
            assert data["daily_messages_used"] >= 0
            assert data["agent_count"] >= 0
    
    @pytest.mark.asyncio
    async def test_templates_endpoint_returns_categories(self):
        """Templates endpoint returns categories."""
        async with APITestClient() as client:
            response = await client.get("/templates")
            assert response.status_code == 200
            
            data = response.json()
            assert "categories" in data
            assert isinstance(data["categories"], list)
            
            # Each category should have expected structure
            for category in data["categories"]:
                assert "category" in category
                assert "templates" in category
                assert isinstance(category["templates"], list)
    
    @pytest.mark.asyncio
    async def test_skills_endpoint_returns_skills_list(self):
        """Skills endpoint returns skills list."""
        async with APITestClient() as client:
            response = await client.get("/skills")
            assert response.status_code == 200
            
            data = response.json()
            assert "skills" in data
            assert isinstance(data["skills"], list)
            
            # Each skill should have expected structure
            for skill in data["skills"]:
                assert "id" in skill
                assert "name" in skill
                assert "description" in skill


class TestHealthCheck:
    """Test health check endpoints."""
    
    @pytest.mark.asyncio
    async def test_basic_health_returns_ok(self):
        """Basic health returns ok."""
        async with APITestClient() as client:
            response = await client.get("/health")
            assert response.status_code == 200
            
            data = response.json()
            assert data["status"] == "ok"
    
    @pytest.mark.asyncio
    async def test_deep_health_returns_db_check_with_latency(self):
        """Deep health returns DB check with latency."""
        async with APITestClient() as client:
            response = await client.get("/health?deep=true")
            assert response.status_code == 200
            
            data = response.json()
            assert "status" in data
            assert "checks" in data
            assert "database" in data["checks"]
            
            db_check = data["checks"]["database"]
            assert "status" in db_check
            assert "latency_ms" in db_check
            assert isinstance(db_check["latency_ms"], (int, float))
            assert db_check["latency_ms"] >= 0


class TestErrorHandling:
    """Test error handling for various invalid requests."""
    
    @pytest.mark.asyncio
    async def test_malformed_json_returns_422(self, unique_device_id):
        """Malformed JSON returns 422."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Send malformed JSON
            response = await client.client.post(
                f"{API_BASE}/agents",
                headers=client._auth_headers(),
                content="{invalid json content"
            )
            assert response.status_code == 422
    
    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_422(self, unique_device_id):
        """Missing required fields returns 422."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            # Missing required 'name' field
            response = await client.post("/agents", json={
                "system_prompt": "Test agent.",
                "model": "qwen3-coder-next"
            })
            assert response.status_code == 422
    
    @pytest.mark.asyncio
    async def test_invalid_model_name_returns_422(self, unique_device_id):
        """Invalid model name returns 422."""
        async with APITestClient() as client:
            await client.login_guest(unique_device_id)
            
            response = await client.post("/agents", json={
                "name": "test-agent",
                "system_prompt": "Test agent.",
                "model": "nonexistent-model"
            })
            assert response.status_code == 422


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])