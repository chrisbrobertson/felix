"""Tool-use schemas and dispatcher for the chat handler LLM.

Read-only retrieval commands exposed as function-calling tools so the
LLM can fetch data itself. State-mutating commands are usually excluded,
but add_goal and add_project are deliberate exceptions — natural-language
creation is the whole point of those operations.
"""
import logging

log = logging.getLogger("chat-tools")

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": (
                "List known code/work/person projects from memory, grouped by "
                "project name across all laptops/hostnames. Call this when the "
                "user asks about 'projects', 'repos', 'what am I working on', "
                "or wants a list grouped by hostname/laptop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional filter: code, work, person"},
                    "limit": {"type": "integer", "description": "Max projects to return (default 50)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_commitments",
            "description": "List active commitments and waiting-on items extracted from meetings and emails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "List recent and upcoming calendar events from memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max events (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_meetings",
            "description": "List recent meeting transcripts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max meetings (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "List contacts/people tracked from emails, meetings, and calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max contacts (default 30)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_comms",
            "description": "List recent email threads and Slack threads.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Optional filter: 'email' or 'slack'"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_readings",
            "description": "List recently captured web page memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memories",
            "description": "Keyword search across all memories. Returns titles grouped by type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "type": {"type": "string", "description": "Optional type filter: email, slack, meeting, project, commitment, event, contact, web"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory",
            "description": "Retrieve the full contents of a specific memory file by title or filename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Title or partial filename to look up"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_commands",
            "description": (
                "List every Telegram slash command this assistant supports, with a "
                "one-line description of each. Call this when the user asks what "
                "they can do, what commands exist, or references a feature whose "
                "exact command they can't recall (e.g. 'how do I mute?', 'is there "
                "a way to see my meetings?')."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_goal",
            "description": "Create a new goal for the user. Use when the user expresses a desired outcome, aspiration, or target they want to achieve.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short, clear description of the goal"
                    },
                    "category": {
                        "type": "string",
                        "description": "Category from the configured list (personal, work, family, learning, other)",
                        "enum": ["personal", "work", "family", "learning", "other"]
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Target date in YYYY-MM-DD format, or omit if no deadline"
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority level",
                        "enum": ["low", "medium", "high", "critical"]
                    }
                },
                "required": ["title", "category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_project",
            "description": "Create a new project for the user. Use when the user describes work they're undertaking — building something, coordinating an effort, pursuing a task over time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short name for the project"
                    },
                    "category": {
                        "type": "string",
                        "description": "Category from the configured list (personal, work, family, learning, other)",
                        "enum": ["personal", "work", "family", "learning", "other"]
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Target completion date in YYYY-MM-DD format, or omit if no deadline"
                    }
                },
                "required": ["title", "category"]
            }
        }
    },
]


def _call(name: str, arguments: dict, handler) -> str:
    """Pure routing from tool name → handler method. Raises on unknown name or missing args."""
    if name == "list_projects":
        return handler._list_projects_text(
            category=arguments.get("category"),
            limit=min(int(arguments.get("limit", 50)), 100),
        )
    if name == "list_commitments":
        return handler._list_commitments_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_events":
        return handler._list_events_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_meetings":
        return handler._list_meetings_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_contacts":
        return handler._list_contacts_text(
            limit=min(int(arguments.get("limit", 30)), 200),
        )
    if name == "list_comms":
        return handler._list_comms_text(
            kind=arguments.get("kind"),
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_readings":
        return handler._list_readings_text(
            limit=min(int(arguments.get("limit", 20)), 50),
        )
    if name == "search_memories":
        return handler._search_memories_text(
            query=arguments["query"],
            type_filter=arguments.get("type"),
        )
    if name == "get_memory":
        return handler._get_memory_text(arguments["name"])
    if name == "list_commands":
        return handler._list_commands_text()
    if name == "add_goal":
        from goals_tracker import GoalManager
        from pathlib import Path
        import os
        memories_dir = Path(os.environ.get("SECOND_BRAIN_DIR", Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain")) / "memories"
        gm = GoalManager(memories_dir, handler._config if hasattr(handler, "_config") else {})
        try:
            path = gm.create_goal(
                title=arguments.get("title", ""),
                category=arguments.get("category", "personal"),
                due_date=arguments.get("due_date"),
                priority=arguments.get("priority", "medium"),
            )
            due_str = f" — due {arguments['due_date']}" if arguments.get("due_date") else ""
            return f"Goal created: {arguments['title']} [{arguments['category']}]{due_str}"
        except ValueError as e:
            return f"Error: {e}"
    if name == "add_project":
        from goals_tracker import GoalManager
        from pathlib import Path
        import os
        memories_dir = Path(os.environ.get("SECOND_BRAIN_DIR", Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain")) / "memories"
        gm = GoalManager(memories_dir, handler._config if hasattr(handler, "_config") else {})
        try:
            path = gm.create_project(
                title=arguments.get("title", ""),
                category=arguments.get("category", "personal"),
                due_date=arguments.get("due_date"),
            )
            due_str = f" — due {arguments['due_date']}" if arguments.get("due_date") else ""
            return f"Project created: {arguments['title']} [{arguments['category']}]{due_str}"
        except ValueError as e:
            return f"Error: {e}"
    raise ValueError(f"unknown tool {name!r}")


async def dispatch(name: str, arguments: dict, handler) -> str:
    """Route a tool call to the right TelegramChatHandler helper.
    Logs every dispatch so failures are visible in error.log without reading LiteLLM internals."""
    log.info(f"dispatch {name} args={arguments}")
    try:
        result = _call(name, arguments, handler)
        log.info(f"dispatch {name} → {len(result)} chars")
        return result
    except KeyError as e:
        log.exception(f"Tool {name} missing required arg: {e}")
        return f"Error running {name}: missing required argument {e}"
    except Exception as e:
        log.exception(f"Tool {name} failed: {e}")
        return f"Error running {name}: {e}"
