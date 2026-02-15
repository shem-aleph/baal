"""Tests for Round 1 API improvements: pagination, filtering, validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from liberclaw.schemas.agents import AgentCreate, AgentUpdate


class TestInputValidation:
    """Test Pydantic field validators for agent schemas."""

    def test_agent_name_validation_valid(self):
        """Test that valid agent names pass validation."""
        valid_names = [
            "My Agent",
            "agent-1",
            "test_agent",
            "Agent123",
            "A",
            "a" * 100,  # Max length
        ]
        
        for name in valid_names:
            agent = AgentCreate(name=name, system_prompt="test prompt")
            assert agent.name == name

    def test_agent_name_validation_invalid(self):
        """Test that invalid agent names fail validation."""
        invalid_names = [
            "agent@name",  # @ symbol not allowed
            "agent!",      # ! symbol not allowed
            "agent.name",  # . not allowed
            "agent/name",  # / not allowed
            "",            # Empty string (min_length=1)
            "a" * 101,     # Too long
        ]
        
        for name in invalid_names:
            with pytest.raises(ValidationError):
                AgentCreate(name=name, system_prompt="test prompt")

    def test_system_prompt_length_validation(self):
        """Test system prompt length validation."""
        # Valid lengths
        AgentCreate(name="test", system_prompt="a")  # Min length 1
        AgentCreate(name="test", system_prompt="a" * 10000)  # Max length
        
        # Invalid lengths
        with pytest.raises(ValidationError):
            AgentCreate(name="test", system_prompt="")  # Empty
        
        with pytest.raises(ValidationError):
            AgentCreate(name="test", system_prompt="a" * 10001)  # Too long

    def test_model_validation(self):
        """Test model validation against AVAILABLE_MODELS."""
        # Valid model
        agent = AgentCreate(name="test", system_prompt="test", model="qwen3-coder-next")
        assert agent.model == "qwen3-coder-next"
        
        # Invalid model
        with pytest.raises(ValidationError) as exc_info:
            AgentCreate(name="test", system_prompt="test", model="invalid-model")
        
        assert "Model must be one of:" in str(exc_info.value)

    def test_agent_update_validation(self):
        """Test AgentUpdate validation works similarly."""
        # Valid update
        update = AgentUpdate(name="valid_name")
        assert update.name == "valid_name"
        
        # Invalid name
        with pytest.raises(ValidationError):
            AgentUpdate(name="invalid@name")
        
        # Invalid model
        with pytest.raises(ValidationError):
            AgentUpdate(model="invalid-model")


class TestPaginationSchema:
    """Test pagination schema changes."""

    def test_agent_list_response_includes_pagination(self):
        """Test that AgentListResponse includes pagination fields."""
        from liberclaw.schemas.agents import AgentListResponse
        
        response = AgentListResponse(
            agents=[],
            total=100,
            limit=20,
            offset=40,
        )
        
        assert response.agents == []
        assert response.total == 100
        assert response.limit == 20
        assert response.offset == 40


@pytest.mark.asyncio
class TestAgentManagerPagination:
    """Test agent manager pagination and filtering."""
    
    async def test_list_agents_returns_tuple(self):
        """Test that list_agents returns (agents, total) tuple."""
        from unittest.mock import AsyncMock, MagicMock
        import uuid
        from liberclaw.services.agent_manager import list_agents
        
        # Mock database session
        db = AsyncMock()
        
        # Mock the query results
        mock_scalar = MagicMock()
        mock_scalar.return_value = 50  # Total count
        
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []  # Empty agent list
        
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_result.scalar.return_value = 50
        
        db.execute.return_value = mock_result
        
        # Test the function
        owner_id = uuid.uuid4()
        agents, total = await list_agents(db, owner_id, limit=10, offset=0)
        
        assert isinstance(agents, list)
        assert isinstance(total, int)
        assert total == 50
        assert len(agents) == 0


class TestUniqueConstraintHandling:
    """Test unique constraint error handling."""
    
    def test_create_agent_duplicate_name_handling(self):
        """Test that duplicate agent names raise proper error."""
        from sqlalchemy.exc import IntegrityError
        from liberclaw.services.agent_manager import create_agent
        import asyncio
        
        async def test_duplicate_error():
            from unittest.mock import AsyncMock
            import uuid
            
            db = AsyncMock()
            
            # Mock IntegrityError with our constraint name in the message
            integrity_error = IntegrityError(
                "statement", "params", "uq_agents_owner_id_name violation", connection_invalidated=False
            )
            
            db.flush.side_effect = integrity_error
            
            # Use a proper Fernet key for testing
            from cryptography.fernet import Fernet
            test_key = Fernet.generate_key()
            
            with pytest.raises(ValueError, match="already exists"):
                await create_agent(
                    db, uuid.uuid4(), "duplicate", "prompt", "qwen3-coder-next", test_key
                )
        
        asyncio.run(test_duplicate_error())