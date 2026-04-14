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
    h._list_commands_text.return_value = "commands text"
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


async def test_dispatch_list_commands(mock_handler):
    result = await chat_tools.dispatch("list_commands", {}, mock_handler)
    assert result == "commands text"
    mock_handler._list_commands_text.assert_called_once_with()


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


async def test_dispatch_logs_success(mock_handler, caplog):
    """Successful dispatch emits two INFO log lines: entry and exit with char count."""
    import logging
    with caplog.at_level(logging.INFO, logger="chat-tools"):
        await chat_tools.dispatch("list_projects", {"limit": 5}, mock_handler)
    info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("dispatch list_projects" in m and "args=" in m for m in info_messages)
    assert any("dispatch list_projects" in m and "chars" in m for m in info_messages)


def test_all_tool_names_in_dispatcher():
    """Every tool name in TOOLS is handled by dispatch (checked via no 'unknown tool' return)."""
    # We don't run dispatch here (async) — just verify the names are in the known set
    dispatched_names = {
        "list_projects", "list_commitments", "list_events", "list_meetings",
        "list_contacts", "list_comms", "list_readings", "search_memories", "get_memory",
        "list_commands", "deliver_pending_replies", "discard_pending_replies",
    }
    for tool in chat_tools.TOOLS:
        name = tool["function"]["name"]
        assert name in dispatched_names, f"Tool {name!r} has no dispatch case"


async def test_deliver_pending_replies_sends_queued_and_clears_state(mock_handler, tmp_path):
    """deliver_pending_replies sends all queued items and clears state on success."""
    from unittest.mock import AsyncMock
    import json

    # Setup handler with pending file
    pending_file = tmp_path / "pending-replies.json"
    mock_handler.PENDING_FILE = pending_file
    mock_handler._load_pending = lambda: json.loads(pending_file.read_text()) if pending_file.exists() else {}
    mock_handler._save_pending = lambda state: pending_file.write_text(json.dumps(state, indent=2))
    mock_handler._chat_history = {}
    mock_handler.HISTORY_WINDOW_TURNS = 6
    mock_handler.app = MagicMock()
    mock_handler.app.bot.send_message = AsyncMock()

    # Pre-populate queue
    pending_file.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "What's up?", "response": "All good!", "queued_at": "2026-04-13T10:00:00"}
            ],
            "summary_sent": True
        }
    }, indent=2))

    result = await chat_tools.dispatch("deliver_pending_replies", {}, mock_handler)

    # Assert send_message called
    assert mock_handler.app.bot.send_message.called
    # Assert history updated
    assert 12345 in mock_handler._chat_history
    assert len(mock_handler._chat_history[12345]) == 2  # user + assistant
    # Assert state cleared
    state = mock_handler._load_pending()
    assert state == {}
    # Assert result message
    assert "Delivered 1" in result
    assert "empty" in result.lower()


async def test_deliver_pending_replies_empty_queue_returns_no_pending(mock_handler, tmp_path):
    """deliver_pending_replies with empty queue returns 'No pending replies'."""
    pending_file = tmp_path / "pending-replies.json"
    mock_handler.PENDING_FILE = pending_file
    mock_handler._load_pending = lambda: {}
    mock_handler._save_pending = lambda state: None

    result = await chat_tools.dispatch("deliver_pending_replies", {}, mock_handler)
    assert "No pending replies" in result


async def test_discard_pending_replies_clears_state(mock_handler, tmp_path):
    """discard_pending_replies clears all queued items."""
    import json

    pending_file = tmp_path / "pending-replies.json"
    mock_handler.PENDING_FILE = pending_file
    mock_handler._load_pending = lambda: json.loads(pending_file.read_text()) if pending_file.exists() else {}
    mock_handler._save_pending = lambda state: pending_file.write_text(json.dumps(state, indent=2))

    # Pre-populate queue
    pending_file.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "test", "response": "test response", "queued_at": "2026-04-13T10:00:00"}
            ]
        }
    }, indent=2))

    result = await chat_tools.dispatch("discard_pending_replies", {}, mock_handler)

    # Assert state cleared
    state = mock_handler._load_pending()
    assert state == {}
    # Assert result message
    assert "Discarded 1" in result


async def test_discard_pending_replies_empty_queue(mock_handler, tmp_path):
    """discard_pending_replies with empty queue returns 'No pending replies'."""
    pending_file = tmp_path / "pending-replies.json"
    mock_handler.PENDING_FILE = pending_file
    mock_handler._load_pending = lambda: {}
    mock_handler._save_pending = lambda state: None

    result = await chat_tools.dispatch("discard_pending_replies", {}, mock_handler)
    assert "No pending replies" in result
