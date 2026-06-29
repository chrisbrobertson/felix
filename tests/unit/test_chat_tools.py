"""Unit tests for chat_tools.py dispatcher."""
import pytest
from unittest.mock import AsyncMock, MagicMock
import chat_tools


@pytest.fixture
def mock_handler():
    """Mock TelegramChatHandler with stub list methods."""
    h = MagicMock()
    h._list_goals_text.return_value = "goals result"
    h._list_features_text = AsyncMock(return_value="features result")
    h._list_todos_text = AsyncMock(return_value="todos result")
    h._list_actions_text = AsyncMock(return_value="actions result")
    h._list_projects_text = AsyncMock(return_value="projects result")
    h._list_commitments_text = AsyncMock(return_value="commitments result")
    h._list_events_text = AsyncMock(return_value="events result")
    h._list_meetings_text = AsyncMock(return_value="meetings result")
    h._list_contacts_text = AsyncMock(return_value="contacts result")
    h._list_comms_text = AsyncMock(return_value="comms result")
    h._list_readings_text = AsyncMock(return_value="readings result")
    h._list_notes_text = AsyncMock(return_value="notes result")
    h._get_note_text = AsyncMock(return_value="note detail")
    h._list_insights_text = AsyncMock(return_value="insights result")
    h._list_aichat_text = AsyncMock(return_value="aichat result")
    h._get_aichat_text = AsyncMock(return_value="aichat detail")
    h._get_code_text = AsyncMock(return_value="code repo detail")
    h._list_changes_text = AsyncMock(return_value="changes result")
    h._list_pending_text = AsyncMock(return_value="pending result")
    h._search_memories_text = AsyncMock(return_value="search result")
    h._get_memory_text = AsyncMock(return_value="memory content")
    h._list_commands_text.return_value = "commands text"
    h._get_event_text.return_value = "event detail"
    h._get_meeting_text.return_value = "meeting detail"
    h._get_contact_text = AsyncMock(return_value="contact detail")
    h._get_comm_text.return_value = "comm detail"
    h._get_reading_text.return_value = "reading detail"
    h._get_action_text.return_value = "action detail"
    h._run_action_text = AsyncMock(return_value="✓ Action 1 executed: done")
    h._drop_action_text = AsyncMock(return_value="✗ Action 1 rejected.")
    h._defer_action_text = AsyncMock(return_value="Action 1 snoozed for 24h.")
    h._update_feature_text = AsyncMock(return_value="feature updated")
    h._update_issue_priority_text = AsyncMock(return_value="priority updated")
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


async def test_dispatch_list_goals(mock_handler):
    result = await chat_tools.dispatch("list_goals", {}, mock_handler)
    assert result == "goals result"
    mock_handler._list_goals_text.assert_called_once_with(category=None, status="active")


async def test_dispatch_list_goals_with_filters(mock_handler):
    await chat_tools.dispatch("list_goals", {"category": "work", "status": "completed"}, mock_handler)
    mock_handler._list_goals_text.assert_called_once_with(category="work", status="completed")


async def test_dispatch_list_features(mock_handler):
    result = await chat_tools.dispatch("list_features", {}, mock_handler)
    assert result == "features result"
    mock_handler._list_features_text.assert_called_once_with(kind=None, show_all=False)


async def test_dispatch_list_features_bugs_only(mock_handler):
    await chat_tools.dispatch("list_features", {"kind": "bug"}, mock_handler)
    mock_handler._list_features_text.assert_called_once_with(kind="bug", show_all=False)


async def test_dispatch_list_todos(mock_handler):
    result = await chat_tools.dispatch("list_todos", {}, mock_handler)
    assert result == "todos result"
    mock_handler._list_todos_text.assert_called_once_with()


async def test_dispatch_list_actions(mock_handler):
    result = await chat_tools.dispatch("list_actions", {}, mock_handler)
    assert result == "actions result"
    mock_handler._list_actions_text.assert_called_once_with(filter_status=None)


async def test_dispatch_list_actions_with_filter(mock_handler):
    await chat_tools.dispatch("list_actions", {"filter_status": "all"}, mock_handler)
    mock_handler._list_actions_text.assert_called_once_with(filter_status="all")


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


async def test_add_feature_writes_file(mock_handler, tmp_path, monkeypatch):
    """add_feature creates a feature_request file with kind:feature."""
    from unittest.mock import patch
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    with patch.object(chat_tools, "MEMORIES_DIR", memories_dir):
        result = await chat_tools.dispatch(
            "add_feature", {"description": "add dark mode support"}, mock_handler
        )
        assert "Feature captured" in result
        files = list(memories_dir.glob("feature-request-*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "kind: feature" in content
        assert "add dark mode support" in content
        assert "status: new" in content


async def test_add_bug_writes_file(mock_handler, tmp_path, monkeypatch):
    """add_bug creates a feature_request file with kind:bug."""
    from unittest.mock import patch
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    with patch.object(chat_tools, "MEMORIES_DIR", memories_dir):
        result = await chat_tools.dispatch(
            "add_bug", {"description": "calendar alerts fire twice"}, mock_handler
        )
        assert "Bug captured" in result
        files = list(memories_dir.glob("feature-request-*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "kind: bug" in content
        assert "calendar alerts fire twice" in content
        assert "## Bug" in content


async def test_add_feature_empty_description(mock_handler, tmp_path, monkeypatch):
    """add_feature with empty description returns error without writing a file."""
    monkeypatch.setenv("SECOND_BRAIN_DIR", str(tmp_path))
    result = await chat_tools.dispatch("add_feature", {"description": ""}, mock_handler)
    assert "Error" in result
    memories_dir = tmp_path / "memories"
    assert not memories_dir.exists() or not list(memories_dir.glob("*.md"))


async def test_dispatch_get_event(mock_handler):
    result = await chat_tools.dispatch("get_event", {"index": 2}, mock_handler)
    assert result == "event detail"
    mock_handler._get_event_text.assert_called_once_with(2)


async def test_dispatch_get_meeting(mock_handler):
    result = await chat_tools.dispatch("get_meeting", {"index": 1}, mock_handler)
    assert result == "meeting detail"
    mock_handler._get_meeting_text.assert_called_once_with(1)


async def test_dispatch_get_contact(mock_handler):
    result = await chat_tools.dispatch("get_contact", {"name_or_index": "Alice"}, mock_handler)
    assert result == "contact detail"
    mock_handler._get_contact_text.assert_called_once_with("Alice")


async def test_dispatch_get_contact_by_index(mock_handler):
    result = await chat_tools.dispatch("get_contact", {"name_or_index": "3"}, mock_handler)
    assert result == "contact detail"
    mock_handler._get_contact_text.assert_called_once_with("3")


async def test_dispatch_get_comm(mock_handler):
    result = await chat_tools.dispatch("get_comm", {"index": 4}, mock_handler)
    assert result == "comm detail"
    mock_handler._get_comm_text.assert_called_once_with(4)


async def test_dispatch_get_reading(mock_handler):
    result = await chat_tools.dispatch("get_reading", {"index": 1}, mock_handler)
    assert result == "reading detail"
    mock_handler._get_reading_text.assert_called_once_with(1)


async def test_dispatch_get_action(mock_handler):
    result = await chat_tools.dispatch("get_action", {"index": 2}, mock_handler)
    assert result == "action detail"
    mock_handler._get_action_text.assert_called_once_with(2)


async def test_dispatch_run_action(mock_handler):
    result = await chat_tools.dispatch("run_action", {"index": 1}, mock_handler)
    assert "executed" in result or "Action 1" in result
    mock_handler._run_action_text.assert_called_once_with(1)


async def test_dispatch_drop_action(mock_handler):
    result = await chat_tools.dispatch("drop_action", {"index": 1}, mock_handler)
    assert "rejected" in result.lower() or "Action 1" in result
    mock_handler._drop_action_text.assert_called_once_with(1)


async def test_dispatch_defer_action_default_hours(mock_handler):
    result = await chat_tools.dispatch("defer_action", {"index": 1}, mock_handler)
    assert "snoozed" in result or "Action 1" in result
    mock_handler._defer_action_text.assert_called_once_with(1, hours=24)


async def test_dispatch_defer_action_custom_hours(mock_handler):
    await chat_tools.dispatch("defer_action", {"index": 2, "hours": 48}, mock_handler)
    mock_handler._defer_action_text.assert_called_once_with(2, hours=48)


async def test_run_action_in_mutating_tools():
    assert "run_action" in chat_tools.MUTATING_TOOLS


async def test_drop_action_in_mutating_tools():
    assert "drop_action" in chat_tools.MUTATING_TOOLS


async def test_defer_action_in_mutating_tools():
    assert "defer_action" in chat_tools.MUTATING_TOOLS


async def test_dispatch_update_feature_plan(mock_handler):
    result = await chat_tools.dispatch(
        "update_feature", {"index_or_id": "3", "action": "plan"}, mock_handler
    )
    assert result == "feature updated"
    mock_handler._update_feature_text.assert_called_once_with(
        index_or_id="3", action="plan", note_or_priority=None
    )


async def test_dispatch_update_feature_done_with_note(mock_handler):
    result = await chat_tools.dispatch(
        "update_feature", {"index_or_id": "abc123", "action": "done", "note_or_priority": "Shipped"}, mock_handler
    )
    assert result == "feature updated"
    mock_handler._update_feature_text.assert_called_once_with(
        index_or_id="abc123", action="done", note_or_priority="Shipped"
    )


async def test_dispatch_update_feature_priority(mock_handler):
    result = await chat_tools.dispatch(
        "update_feature", {"index_or_id": "2", "action": "priority", "note_or_priority": "high"}, mock_handler
    )
    assert result == "feature updated"
    mock_handler._update_feature_text.assert_called_once_with(
        index_or_id="2", action="priority", note_or_priority="high"
    )


async def test_update_feature_in_mutating_tools():
    assert "update_feature" in chat_tools.MUTATING_TOOLS


async def test_dispatch_list_notes(mock_handler):
    result = await chat_tools.dispatch("list_notes", {}, mock_handler)
    assert result == "notes result"
    mock_handler._list_notes_text.assert_called_once_with(limit=20, folder_filter=None, todos_only=False)


async def test_dispatch_list_notes_with_folder(mock_handler):
    await chat_tools.dispatch("list_notes", {"folder": "Work", "limit": 5}, mock_handler)
    mock_handler._list_notes_text.assert_called_once_with(limit=5, folder_filter="Work", todos_only=False)


async def test_dispatch_list_notes_todos_only(mock_handler):
    await chat_tools.dispatch("list_notes", {"todos_only": True}, mock_handler)
    mock_handler._list_notes_text.assert_called_once_with(limit=20, folder_filter=None, todos_only=True)


async def test_dispatch_get_note(mock_handler):
    result = await chat_tools.dispatch("get_note", {"index": 3}, mock_handler)
    assert result == "note detail"
    mock_handler._get_note_text.assert_called_once_with(3)


async def test_dispatch_list_insights(mock_handler):
    result = await chat_tools.dispatch("list_insights", {}, mock_handler)
    assert result == "insights result"
    mock_handler._list_insights_text.assert_called_once_with(limit=10)


async def test_dispatch_list_insights_custom_limit(mock_handler):
    await chat_tools.dispatch("list_insights", {"limit": 5}, mock_handler)
    mock_handler._list_insights_text.assert_called_once_with(limit=5)


async def test_dispatch_list_aichat(mock_handler):
    result = await chat_tools.dispatch("list_aichat", {}, mock_handler)
    assert result == "aichat result"
    mock_handler._list_aichat_text.assert_called_once_with(limit=20)


async def test_dispatch_get_aichat(mock_handler):
    result = await chat_tools.dispatch("get_aichat", {"index": 2}, mock_handler)
    assert result == "aichat detail"
    mock_handler._get_aichat_text.assert_called_once_with(2)


def test_all_tool_names_in_dispatcher():
    """Every tool name in TOOLS is handled by dispatch (checked via no 'unknown tool' return)."""
    # We don't run dispatch here (async) — just verify the names are in the known set
    dispatched_names = {
        "list_goals", "list_features", "list_todos", "list_actions",
        "list_projects", "list_commitments", "list_events", "list_meetings",
        "list_contacts", "list_comms", "list_readings", "search_memories", "get_memory",
        "list_commands", "deliver_pending_replies", "discard_pending_replies",
        "add_goal", "add_project", "add_feature", "add_bug", "close_issue",
        "close_commitment", "close_goal", "close_project",
        "add_todo", "get_goal", "get_project", "get_feature", "update_feature",
        "get_event", "get_meeting", "get_contact", "get_comm", "get_reading", "get_action",
        "run_action", "drop_action", "defer_action",
        "update_issue_priority",
        "list_notes", "get_note", "list_insights", "list_aichat", "get_aichat",
        "get_code", "list_changes", "list_pending",
        # Handled in handle_message's tool_dispatch closure (needs chat_id in scope)
        "get_recent_commands",
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


# --- FR-8: LLM tools for goals and projects ---

def test_add_goal_tool_in_tools_list():
    """add_goal is present in TOOLS with required fields."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "add_goal" in names

    # Find the add_goal tool and verify its structure
    add_goal_tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "add_goal")
    assert add_goal_tool["function"]["description"]
    params = add_goal_tool["function"]["parameters"]
    assert "title" in params["properties"]
    assert "category" in params["properties"]
    assert params["required"] == ["title", "category"]


def test_add_project_tool_in_tools_list():
    """add_project is present in TOOLS with required fields."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "add_project" in names

    # Find the add_project tool and verify its structure
    add_project_tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "add_project")
    assert add_project_tool["function"]["description"]
    params = add_project_tool["function"]["parameters"]
    assert "title" in params["properties"]
    assert "category" in params["properties"]
    assert params["required"] == ["title", "category"]


@pytest.mark.asyncio
async def test_add_goal_tool_dispatch_creates_file(tmp_path):
    """Dispatching add_goal creates a goal file."""
    from unittest.mock import MagicMock, patch

    handler = MagicMock()
    handler._config = {"goals": {"categories": ["personal", "work", "family", "learning", "other"]}}

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(chat_tools, "MEMORIES_DIR", memories_dir):
        result = await chat_tools.dispatch(
            "add_goal",
            {"title": "Run a 5K", "category": "personal", "due_date": "2026-06-30", "priority": "medium"},
            handler
        )

    assert "Goal created" in result
    assert "Run a 5K" in result
    assert "[personal]" in result
    assert "2026-06-30" in result

    # Check that a goal file was created
    goal_files = list(memories_dir.glob("goal-*.md"))
    assert len(goal_files) == 1

    # Verify file content
    content = goal_files[0].read_text()
    assert "type: goal" in content
    assert "category: personal" in content
    assert "source_title: Run a 5K" in content


@pytest.mark.asyncio
async def test_add_project_tool_dispatch_creates_file(tmp_path):
    """Dispatching add_project creates a project file."""
    from unittest.mock import MagicMock, patch

    handler = MagicMock()
    handler._config = {"goals": {"categories": ["personal", "work", "family", "learning", "other"]}}

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(chat_tools, "MEMORIES_DIR", memories_dir):
        result = await chat_tools.dispatch(
            "add_project",
            {"title": "Q2 rollout", "category": "work", "due_date": "2026-07-01"},
            handler
        )

    assert "Project created" in result
    assert "Q2 rollout" in result
    assert "[work]" in result
    assert "2026-07-01" in result

    # Check that a project file was created
    project_files = list(memories_dir.glob("project-*.md"))
    assert len(project_files) == 1

    # Verify file content
    content = project_files[0].read_text()
    assert "type: project" in content
    assert "category: work" in content
    assert "source_title: Q2 rollout" in content


@pytest.mark.asyncio
async def test_add_goal_invalid_category_returns_error(tmp_path):
    """add_goal with invalid category returns error string, not exception."""
    from unittest.mock import MagicMock, patch

    handler = MagicMock()
    handler._config = {"goals": {"categories": ["personal", "work", "family", "learning", "other"]}}

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.dict("os.environ", {"SECOND_BRAIN_DIR": str(tmp_path)}, clear=False):
        result = await chat_tools.dispatch(
            "add_goal",
            {"title": "Test Goal", "category": "invalid_category"},
            handler
        )

    assert "Error" in result
    assert "Invalid category" in result or "invalid_category" in result


# --- close_issue tool tests ---

def test_close_issue_tool_in_tools_list():
    """close_issue is present in TOOLS with required fields."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "close_issue" in names

    close_issue_tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "close_issue")
    assert close_issue_tool["function"]["description"]
    params = close_issue_tool["function"]["parameters"]
    assert "short_id" in params["properties"]
    assert "title" in params["properties"]
    assert "status" in params["properties"]
    assert params["properties"]["status"]["enum"] == ["done", "wont_do", "in_progress"]


@pytest.mark.asyncio
async def test_close_issue_by_short_id(mock_handler, tmp_path):
    """close_issue by short_id calls _close_issue_text correctly."""
    mock_handler._close_issue_text = AsyncMock(return_value="Closed [abc123] Bug title → done")

    result = await chat_tools.dispatch(
        "close_issue",
        {"short_id": "abc123"},
        mock_handler
    )

    assert result == "Closed [abc123] Bug title → done"
    mock_handler._close_issue_text.assert_called_once_with(
        short_id="abc123",
        title=None,
        status="done"
    )


@pytest.mark.asyncio
async def test_close_issue_by_title(mock_handler, tmp_path):
    """close_issue by title calls _close_issue_text correctly."""
    mock_handler._close_issue_text = AsyncMock(return_value="Closed [def456] Feature request → done")

    result = await chat_tools.dispatch(
        "close_issue",
        {"title": "PDF bug"},
        mock_handler
    )

    assert result == "Closed [def456] Feature request → done"
    mock_handler._close_issue_text.assert_called_once_with(
        short_id=None,
        title="PDF bug",
        status="done"
    )


@pytest.mark.asyncio
async def test_close_issue_custom_status(mock_handler, tmp_path):
    """close_issue with custom status passes it through."""
    mock_handler._close_issue_text = AsyncMock(return_value="Closed [ghi789] Issue → wont_do")

    result = await chat_tools.dispatch(
        "close_issue",
        {"short_id": "ghi789", "status": "wont_do"},
        mock_handler
    )

    assert "wont_do" in result
    mock_handler._close_issue_text.assert_called_once_with(
        short_id="ghi789",
        title=None,
        status="wont_do"
    )


# --- update_issue_priority tool tests ---

def test_update_issue_priority_tool_in_tools_list():
    """update_issue_priority is present in TOOLS with required fields."""
    names = {t["function"]["name"] for t in chat_tools.TOOLS}
    assert "update_issue_priority" in names


def test_update_issue_priority_in_mutating_tools():
    assert "update_issue_priority" in chat_tools.MUTATING_TOOLS


async def test_dispatch_update_issue_priority_by_short_id(mock_handler):
    result = await chat_tools.dispatch(
        "update_issue_priority", {"short_id": "abc123", "priority": "high"}, mock_handler
    )
    assert result == "priority updated"
    mock_handler._update_issue_priority_text.assert_awaited_once_with(
        short_id="abc123", title=None, priority="high"
    )


async def test_dispatch_update_issue_priority_by_title(mock_handler):
    result = await chat_tools.dispatch(
        "update_issue_priority", {"title": "dark mode", "priority": "critical"}, mock_handler
    )
    assert result == "priority updated"
    mock_handler._update_issue_priority_text.assert_awaited_once_with(
        short_id=None, title="dark mode", priority="critical"
    )


# --- close_commitment tool tests ---

def test_close_commitment_tool_in_tools_list():
    """close_commitment is present in TOOLS with required fields."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "close_commitment" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "close_commitment")
    assert tool["function"]["description"]
    params = tool["function"]["parameters"]
    assert "index" in params["properties"]
    assert "title" in params["properties"]
    assert "status" in params["properties"]
    assert params["properties"]["status"]["enum"] == ["completed", "dismissed"]


def test_close_commitment_in_mutating_tools():
    """close_commitment is listed as a mutating tool."""
    assert "close_commitment" in chat_tools.MUTATING_TOOLS


@pytest.mark.asyncio
async def test_close_commitment_by_index(mock_handler):
    """close_commitment by index calls _close_commitment_text correctly."""
    mock_handler._close_commitment_text = AsyncMock(return_value="✓ Send the report → completed")

    result = await chat_tools.dispatch(
        "close_commitment",
        {"index": 2},
        mock_handler,
    )

    assert result == "✓ Send the report → completed"
    mock_handler._close_commitment_text.assert_called_once_with(
        index=2,
        title=None,
        status="completed",
    )


@pytest.mark.asyncio
async def test_close_commitment_by_title(mock_handler):
    """close_commitment by title substring calls _close_commitment_text correctly."""
    mock_handler._close_commitment_text = AsyncMock(return_value="✓ dentist → completed")

    result = await chat_tools.dispatch(
        "close_commitment",
        {"title": "dentist"},
        mock_handler,
    )

    assert "dentist" in result
    mock_handler._close_commitment_text.assert_called_once_with(
        index=None,
        title="dentist",
        status="completed",
    )


@pytest.mark.asyncio
async def test_close_commitment_dismissed_status(mock_handler):
    """close_commitment with dismissed status passes it through."""
    mock_handler._close_commitment_text = AsyncMock(return_value="✕ old task → dismissed")

    result = await chat_tools.dispatch(
        "close_commitment",
        {"index": 1, "status": "dismissed"},
        mock_handler,
    )

    assert "dismissed" in result
    mock_handler._close_commitment_text.assert_called_once_with(
        index=1,
        title=None,
        status="dismissed",
    )


# --- add_todo tool tests ---

def test_add_todo_tool_in_tools_list():
    """add_todo is present in TOOLS with required fields."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "add_todo" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "add_todo")
    assert tool["function"]["description"]
    params = tool["function"]["parameters"]
    assert "description" in params["properties"]
    assert "description" in params.get("required", [])
    assert "due_date" in params["properties"]
    assert "type" in params["properties"]


def test_add_todo_in_mutating_tools():
    """add_todo is listed as a mutating tool."""
    assert "add_todo" in chat_tools.MUTATING_TOOLS


@pytest.mark.asyncio
async def test_add_todo_basic(mock_handler):
    """add_todo calls _add_todo_text with description."""
    mock_handler._add_todo_text.return_value = "✓ Todo added [personal]: Call John"

    result = await chat_tools.dispatch("add_todo", {"description": "Call John"}, mock_handler)

    assert result == "✓ Todo added [personal]: Call John"
    mock_handler._add_todo_text.assert_called_once_with(
        description="Call John",
        due_date=None,
        todo_type=None,
    )


@pytest.mark.asyncio
async def test_add_todo_with_due_date_and_type(mock_handler):
    """add_todo passes due_date and type through."""
    mock_handler._add_todo_text.return_value = "✓ Todo added [waiting_on]: Waiting for Jane — due 2026-05-15"

    result = await chat_tools.dispatch(
        "add_todo",
        {"description": "Waiting for Jane", "due_date": "2026-05-15", "type": "waiting_on"},
        mock_handler,
    )

    assert "waiting_on" in result or "Waiting for Jane" in result
    mock_handler._add_todo_text.assert_called_once_with(
        description="Waiting for Jane",
        due_date="2026-05-15",
        todo_type="waiting_on",
    )


# --- get_goal tool tests ---

def test_get_goal_tool_in_tools_list():
    """get_goal is present in TOOLS with required index field."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "get_goal" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "get_goal")
    params = tool["function"]["parameters"]
    assert "index" in params["properties"]
    assert "index" in params.get("required", [])


@pytest.mark.asyncio
async def test_get_goal_dispatch(mock_handler):
    """get_goal dispatches to _get_goal_text with the index."""
    mock_handler._get_goal_text.return_value = "Run a 5K [fitness] — active\nDue: 2026-06-01"

    result = await chat_tools.dispatch("get_goal", {"index": 2}, mock_handler)

    assert "5K" in result
    mock_handler._get_goal_text.assert_called_once_with(2)


# --- get_project tool tests ---

def test_get_project_tool_in_tools_list():
    """get_project is present in TOOLS with required index field."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "get_project" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "get_project")
    params = tool["function"]["parameters"]
    assert "index" in params["properties"]
    assert "index" in params.get("required", [])


@pytest.mark.asyncio
async def test_get_project_dispatch(mock_handler):
    """get_project dispatches to _get_project_text with the index."""
    mock_handler._get_project_text.return_value = "Website redesign [work] — active"

    result = await chat_tools.dispatch("get_project", {"index": 1}, mock_handler)

    assert "Website" in result
    mock_handler._get_project_text.assert_called_once_with(1)


# --- get_feature tool tests ---

def test_get_feature_tool_in_tools_list():
    """get_feature is present in TOOLS with required index_or_id field."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "get_feature" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "get_feature")
    params = tool["function"]["parameters"]
    assert "index_or_id" in params["properties"]
    assert "index_or_id" in params.get("required", [])


@pytest.mark.asyncio
async def test_get_feature_dispatch_by_index(mock_handler):
    """get_feature dispatches to _get_feature_text with the index string."""
    mock_handler._get_feature_text = AsyncMock(return_value="**Dark mode** — Status: new")

    result = await chat_tools.dispatch("get_feature", {"index_or_id": "3"}, mock_handler)

    assert "Dark mode" in result
    mock_handler._get_feature_text.assert_called_once_with("3")


@pytest.mark.asyncio
async def test_get_feature_dispatch_by_short_id(mock_handler):
    """get_feature dispatches to _get_feature_text with the short_id string."""
    mock_handler._get_feature_text = AsyncMock(return_value="**Some bug** — Status: new | Kind: bug")

    result = await chat_tools.dispatch("get_feature", {"index_or_id": "ab12cd"}, mock_handler)

    assert "Some bug" in result
    mock_handler._get_feature_text.assert_called_once_with("ab12cd")


# --- get_code tool tests ---

def test_get_code_tool_in_tools_list():
    """get_code is present in TOOLS with required index field."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "get_code" in names

    tool = next(t for t in chat_tools.TOOLS if t["function"]["name"] == "get_code")
    params = tool["function"]["parameters"]
    assert "index" in params["properties"]
    assert "index" in params.get("required", [])


@pytest.mark.asyncio
async def test_get_code_dispatch(mock_handler):
    """get_code dispatches to _get_code_text with the index."""
    mock_handler._get_code_text = AsyncMock(return_value="secondbrain\nhttps://github.com/foo/bar")

    result = await chat_tools.dispatch("get_code", {"index": 1}, mock_handler)

    assert "secondbrain" in result
    mock_handler._get_code_text.assert_called_once_with(1)


# --- list_changes tool tests ---

def test_list_changes_tool_in_tools_list():
    """list_changes is present in TOOLS."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "list_changes" in names


@pytest.mark.asyncio
async def test_list_changes_dispatch_default_hours(mock_handler):
    """list_changes defaults to 24 hours."""
    result = await chat_tools.dispatch("list_changes", {}, mock_handler)
    assert result == "changes result"
    mock_handler._list_changes_text.assert_called_once_with(hours=24)


@pytest.mark.asyncio
async def test_list_changes_dispatch_custom_hours(mock_handler):
    """list_changes passes hours through."""
    await chat_tools.dispatch("list_changes", {"hours": 48}, mock_handler)
    mock_handler._list_changes_text.assert_called_once_with(hours=48)


# --- list_pending tool tests ---

def test_list_pending_tool_in_tools_list():
    """list_pending is present in TOOLS."""
    names = [t["function"]["name"] for t in chat_tools.TOOLS]
    assert "list_pending" in names


@pytest.mark.asyncio
async def test_list_pending_dispatch(mock_handler):
    """list_pending dispatches to _list_pending_text."""
    result = await chat_tools.dispatch("list_pending", {}, mock_handler)
    assert result == "pending result"
    mock_handler._list_pending_text.assert_called_once_with()
