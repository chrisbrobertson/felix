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
            "name": "deliver_pending_replies",
            "description": (
                "Deliver every reply queued while the network was down. ONLY call this tool when "
                "(a) your most recent assistant turn in the conversation was the '📬 Network is back. "
                "I have N response(s) I couldn't deliver earlier' notification, AND (b) the user's "
                "latest message is an affirmative response directly addressing that notification. "
                "If the user's 'yes' could be answering any other question in the conversation, "
                "do NOT call this tool — ask them to clarify instead."
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
            "name": "discard_pending_replies",
            "description": (
                "Discard all queued replies from when the network was down. ONLY call this tool when "
                "(a) your most recent assistant turn was the pending-queue notification, AND "
                "(b) the user explicitly wants to discard the queued replies (e.g. 'discard them', "
                "'drop them', 'no don't deliver'). Do not call this on an ambiguous 'no' that could "
                "be answering a different question."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
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


async def _deliver_pending(handler) -> str:
    """Deliver all queued replies via bot.send_message, updating chat history on success."""
    state = handler._load_pending()
    if not state:
        return "No pending replies to deliver."

    total_delivered = 0
    total_remaining = 0

    for chat_id_str, entry in list(state.items()):
        pending = entry.get("pending", [])
        if not pending:
            continue
        chat_id = int(chat_id_str)
        remaining = []
        turns = handler._chat_history.setdefault(chat_id, [])

        for item in pending:
            text = item["response"]
            chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)] or [text]
            try:
                for chunk in chunks:
                    await handler.app.bot.send_message(chat_id=chat_id, text=chunk)
                turns.append({"role": "user", "content": item["query"]})
                turns.append({"role": "assistant", "content": text[:4096]})
                total_delivered += 1
            except Exception as e:
                log.warning("Pending delivery failed for chat %s: %s", chat_id_str, e)
                remaining.append(item)
                total_remaining += 1

        max_msgs = handler.HISTORY_WINDOW_TURNS * 2
        if len(turns) > max_msgs:
            handler._chat_history[chat_id] = turns[-max_msgs:]

        if remaining:
            entry["pending"] = remaining
            entry["summary_sent"] = False
            state[chat_id_str] = entry
        else:
            state.pop(chat_id_str, None)

    handler._save_pending(state)

    if total_remaining:
        return (
            f"Delivered {total_delivered} reply/replies. "
            f"{total_remaining} could not be sent (network still down) and remain queued."
        )
    return f"Delivered {total_delivered} queued reply/replies. Queue is now empty."


async def _discard_pending(handler) -> str:
    """Discard all queued pending replies."""
    state = handler._load_pending()
    if not state:
        return "No pending replies to discard."
    total = sum(len(e.get("pending", [])) for e in state.values())
    handler._save_pending({})
    return f"Discarded {total} queued reply/replies."


async def _call(name: str, arguments: dict, handler):
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
    if name == "deliver_pending_replies":
        return await _deliver_pending(handler)
    if name == "discard_pending_replies":
        return await _discard_pending(handler)
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
        result = await _call(name, arguments, handler)
        log.info(f"dispatch {name} → {len(result)} chars")
        return result
    except KeyError as e:
        log.exception(f"Tool {name} missing required arg: {e}")
        return f"Error running {name}: missing required argument {e}"
    except Exception as e:
        log.exception(f"Tool {name} failed: {e}")
        return f"Error running {name}: {e}"
