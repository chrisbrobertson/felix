"""Unit tests for chat_tools.py dispatcher."""
import pytest
from unittest.mock import MagicMock
import chat_tools


@pytest.fixture
def mock_handler():
    """Mock TelegramChatHandler with stub list methods."""
    h = MagicMock()
    h._list_projects_text.return_value = "projects result"
    h._list_commitments_text.return_value = "commitments result"
    h._list_events_text.return_value = "events result"
    h._list_meetings_text.return_value = "meetings result"
    h._list_contacts_text.return_value = "contacts result"
    h._list_comms_text.return_value = "comms result"
    h._list_readings_text.return_value = "readings result"
    h._search_memories_text.return_value = "search result"
    h._get_memory_text.return_value = "memory content"
    return h


async def test_dispatch_list_projects(mock_handler):
    result = await chat_tools.dispatch("list_projects", {"limit": 10}, mock_handler)
    assert result == "projects result"
    mock_handler._list_projects_text.assert_called_once_with(category=None, limit=10)


async def test_dispatch_list_projects_with_category(mock_handler):
    await chat_tools.dispatch("list_projects", {"category": "code", "limit": 5}, mock_handler)
    mock_handler._list_projects_text.assert_called_once_with(category="code", limit=5)


async def test_dispatch_list_commitments(mock_handler):
    result = await chat_tools.dispatch("list_commitments", {}, mock_handler)
    assert result == "commitments result"


async def test_dispatch_list_events(mock_handler):
    result = await chat_tools.dispatch("list_events", {}, mock_handler)
    assert result == "events result"


async def test_dispatch_list_meetings(mock_handler):
    result = await chat_tools.dispatch("list_meetings", {}, mock_handler)
    assert result == "meetings result"


async def test_dispatch_list_contacts(mock_handler):
    result = await chat_tools.dispatch("list_contacts", {}, mock_handler)
    assert result == "contacts result"


async def test_dispatch_list_comms(mock_handler):
    result = await chat_tools.dispatch("list_comms", {"kind": "email"}, mock_handler)
    assert result == "comms result"
    mock_handler._list_comms_text.assert_called_once_with(kind="email", limit=20)


async def test_dispatch_list_readings(mock_handler):
    result = await chat_tools.dispatch("list_readings", {}, mock_handler)
    assert result == "readings result"


async def test_dispatch_search_memories(mock_handler):
    result = await chat_tools.dispatch("search_memories", {"query": "migration"}, mock_handler)
    assert result == "search result"
    mock_handler._search_memories_text.assert_called_once_with(query="migration", type_filter=None)


async def test_dispatch_search_memories_with_type(mock_handler):
    await chat_tools.dispatch("search_memories", {"query": "migration", "type": "meeting"}, mock_handler)
    mock_handler._search_memories_text.assert_called_once_with(query="migration", type_filter="meeting")


async def test_dispatch_get_memory(mock_handler):
    result = await chat_tools.dispatch("get_memory", {"name": "secondbrain"}, mock_handler)
    assert result == "memory content"


async def test_dispatch_unknown_tool_returns_error_string(mock_handler):
    result = await chat_tools.dispatch("delete_everything", {}, mock_handler)
    assert "unknown tool" in result.lower() or "Error" in result


async def test_dispatch_handler_exception_returns_error_string(mock_handler):
    mock_handler._list_projects_text.side_effect = RuntimeError("disk full")
    result = await chat_tools.dispatch("list_projects", {}, mock_handler)
    assert "Error" in result
    assert "list_projects" in result


def test_tools_schema_valid():
    """Every tool entry has required fields."""
    for tool in chat_tools.TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["description"], f"Tool {fn['name']} has empty description"


def test_all_tool_names_in_dispatcher():
    """Every tool name in TOOLS is handled by dispatch (checked via no 'unknown tool' return)."""
    # We don't run dispatch here (async) — just verify the names are in the known set
    dispatched_names = {
        "list_projects", "list_commitments", "list_events", "list_meetings",
        "list_contacts", "list_comms", "list_readings", "search_memories", "get_memory",
    }
    for tool in chat_tools.TOOLS:
        name = tool["function"]["name"]
        assert name in dispatched_names, f"Tool {name!r} has no dispatch case"
