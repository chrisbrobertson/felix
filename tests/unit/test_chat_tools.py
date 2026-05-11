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
    h._list_actions_text.return_value = "actions result"
    h._list_projects_text.return_value = "projects result"
    h._list_commitments_text = AsyncMock(return_value="commitments result")
    h._list_events_text.return_value = "events result"
    h._list_meetings_text.return_value = "meetings result"
    h._list_contacts_text.return_value = "contacts result"
    h._list_comms_text.return_value = "comms result"
    h._list_readings_text = AsyncMock(return_value="readings result")
    h._search_memories_text = AsyncMock(return_value="search result")
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


def test_all_tool_names_in_dispatcher():
    """Every tool name in TOOLS is handled by dispatch (checked via no 'unknown tool' return)."""
    # We don't run dispatch here (async) — just verify the names are in the known set
    dispatched_names = {
        "list_goals", "list_features", "list_todos", "list_actions",
        "list_projects", "list_commitments", "list_events", "list_meetings",
        "list_contacts", "list_comms", "list_readings", "search_memories", "get_memory",
        "list_commands", "deliver_pending_replies", "discard_pending_replies",
        "add_goal", "add_project", "add_feature", "add_bug", "close_issue",
        "close_commitment",
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
