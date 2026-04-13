"""Unit tests for SkillExecutor.run_with_tools()."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import yaml


@pytest.fixture
def tmp_skills_dir(tmp_path):
    """Create a temporary skills directory with a test skill."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "test_skill.md"
    skill_content = """---
name: test_skill
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: claude-haiku-4-5-20251001
---

## Instructions

You are a helpful assistant.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""
    skill_path.write_text(skill_content)
    return skills_dir


@pytest.fixture
def mock_tool_dispatch():
    """Mock tool dispatch callable."""
    async def _dispatch(name: str, args: dict) -> str:
        return f"Tool {name} returned: {args}"
    return AsyncMock(side_effect=_dispatch)


@pytest.fixture
def mock_acompletion_no_tools():
    """Mock acompletion that returns content without tool calls."""
    mock_msg = MagicMock()
    mock_msg.content = "Final answer from LLM"
    mock_msg.tool_calls = []

    mock_choice = MagicMock()
    mock_choice.message = mock_msg

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    return AsyncMock(return_value=mock_response)


@pytest.fixture
def mock_acompletion_with_tools():
    """Mock acompletion that returns tool calls on first turn, then content."""
    # First turn: tool call
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function.name = "list_projects"
    tool_call.function.arguments = '{"limit": 10}'

    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tool_call]

    choice1 = MagicMock()
    choice1.message = msg1

    response1 = MagicMock()
    response1.choices = [choice1]

    # Second turn: final content
    msg2 = MagicMock()
    msg2.content = "Here are your projects"
    msg2.tool_calls = []

    choice2 = MagicMock()
    choice2.message = msg2

    response2 = MagicMock()
    response2.choices = [choice2]

    return AsyncMock(side_effect=[response1, response2])


@pytest.fixture
def mock_acompletion_infinite_tools():
    """Mock acompletion that always returns tool calls (never final content)."""
    tool_call = MagicMock()
    tool_call.id = "call_456"
    tool_call.function.name = "search_memories"
    tool_call.function.arguments = '{"query": "test"}'

    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tool_call]

    choice = MagicMock()
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]

    return AsyncMock(return_value=response)


async def test_run_with_tools_no_tool_calls(tmp_skills_dir, mock_tool_dispatch, mock_acompletion_no_tools):
    """Single turn, no tool calls — returns content, dispatcher never called."""
    from skill_executor import SkillExecutor

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion_no_tools):
        executor = SkillExecutor("test_skill", role="full")
        result = await executor.run_with_tools(
            inputs={"query": "test"},
            tools=[],
            tool_dispatch=mock_tool_dispatch,
        )

        assert result == "Final answer from LLM"
        mock_tool_dispatch.assert_not_called()


async def test_run_with_tools_one_tool_call(tmp_skills_dir, mock_tool_dispatch, mock_acompletion_with_tools):
    """One tool call turn + final content turn — dispatcher called once with right args."""
    from skill_executor import SkillExecutor

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion_with_tools):
        executor = SkillExecutor("test_skill", role="full")
        tools = [{"type": "function", "function": {"name": "list_projects"}}]
        result = await executor.run_with_tools(
            inputs={"query": "show projects"},
            tools=tools,
            tool_dispatch=mock_tool_dispatch,
        )

        assert result == "Here are your projects"
        mock_tool_dispatch.assert_called_once_with("list_projects", {"limit": 10})


async def test_run_with_tools_max_iterations(tmp_skills_dir, mock_tool_dispatch, mock_acompletion_infinite_tools):
    """max_iterations=2, every turn has tool_calls — returns the 'ran out of iterations' string."""
    from skill_executor import SkillExecutor

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion_infinite_tools):
        executor = SkillExecutor("test_skill", role="full")
        tools = [{"type": "function", "function": {"name": "search_memories"}}]
        result = await executor.run_with_tools(
            inputs={"query": "search"},
            tools=tools,
            tool_dispatch=mock_tool_dispatch,
            max_iterations=2,
        )

        assert "ran out of iterations" in result
        assert mock_tool_dispatch.call_count == 2


async def test_run_with_tools_fallback_on_error(tmp_skills_dir, mock_tool_dispatch):
    """Preferred model raises, fallback succeeds (single turn, no tools)."""
    from skill_executor import SkillExecutor

    # First call (preferred) raises, second call (fallback) succeeds
    msg = MagicMock()
    msg.content = "Fallback result"
    msg.tool_calls = []

    choice = MagicMock()
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]

    mock_acompletion = AsyncMock(side_effect=[RuntimeError("preferred failed"), response])

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion):
        executor = SkillExecutor("test_skill", role="full")
        result = await executor.run_with_tools(
            inputs={"query": "test"},
            tools=[],
            tool_dispatch=mock_tool_dispatch,
        )

        assert result == "Fallback result"
        assert mock_acompletion.call_count == 2


async def test_run_with_tools_all_models_fail(tmp_skills_dir, mock_tool_dispatch):
    """Both models raise — returns None, ERROR_LOG written."""
    from skill_executor import SkillExecutor

    mock_acompletion = AsyncMock(side_effect=RuntimeError("all failed"))

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion), \
         patch("skill_executor.ERROR_LOG", tmp_path := Path("/tmp/test-errors.log")):
        # Ensure log file parent exists
        tmp_path.parent.mkdir(exist_ok=True, parents=True)

        executor = SkillExecutor("test_skill", role="full")
        result = await executor.run_with_tools(
            inputs={"query": "test"},
            tools=[],
            tool_dispatch=mock_tool_dispatch,
        )

        assert result is None
        # Both preferred and fallback should be tried
        assert mock_acompletion.call_count == 2


async def test_run_with_tools_invalid_json_arguments(tmp_skills_dir, mock_tool_dispatch):
    """Tool call with malformed JSON arguments — should handle gracefully."""
    from skill_executor import SkillExecutor

    # Tool call with invalid JSON
    tool_call = MagicMock()
    tool_call.id = "call_789"
    tool_call.function.name = "list_projects"
    tool_call.function.arguments = '{invalid json'

    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tool_call]

    choice1 = MagicMock()
    choice1.message = msg1

    response1 = MagicMock()
    response1.choices = [choice1]

    # Second turn: final content
    msg2 = MagicMock()
    msg2.content = "Final answer"
    msg2.tool_calls = []

    choice2 = MagicMock()
    choice2.message = msg2

    response2 = MagicMock()
    response2.choices = [choice2]

    mock_acompletion = AsyncMock(side_effect=[response1, response2])

    with patch("skill_executor.SKILLS_DIR", tmp_skills_dir), \
         patch("skill_executor.acompletion", mock_acompletion):
        executor = SkillExecutor("test_skill", role="full")
        result = await executor.run_with_tools(
            inputs={"query": "test"},
            tools=[],
            tool_dispatch=mock_tool_dispatch,
        )

        assert result == "Final answer"
        # Should be called with empty dict when JSON parsing fails
        mock_tool_dispatch.assert_called_once_with("list_projects", {})
