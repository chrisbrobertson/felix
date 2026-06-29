"""Tool-use schemas and dispatcher for the chat handler LLM.

Read-only retrieval commands exposed as function-calling tools so the
LLM can fetch data itself. State-mutating commands are usually excluded,
but add_goal and add_project are deliberate exceptions — natural-language
creation is the whole point of those operations.
"""
import logging
from pathlib import Path

log = logging.getLogger("chat-tools")

# iCloud memories directory — distinct from SECOND_BRAIN_DIR (deploy dir)
MEMORIES_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"

# Tools that write or mutate persistent state.  The chat handler uses this to
# detect the case where a timeout fires after a mutation has already landed, so
# it can warn the user rather than silently suggest they retry.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {"add_goal", "add_project", "add_bug", "add_feature", "close_issue", "close_commitment",
     "close_goal", "close_project", "deliver_pending_replies", "add_todo", "update_feature",
     "update_issue_priority", "run_action", "drop_action", "defer_action",
     "update_goal_note", "update_goal_due", "update_project_note", "update_project_due",
     "add_milestone", "toggle_milestone", "link_goal", "unlink_goal"}
)

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": (
                "List active projects from memory. Use category='code' to list "
                "code repositories grouped by hostname/laptop. Omit category or "
                "use a GoalManager category (work, personal, family, learning, other) "
                "to list work/life projects. Call this when the user asks about "
                "'projects', 'repos', 'what am I working on'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional filter: code (repos), work, personal, family, learning, other",
                    },
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
                    "type": {"type": "string", "description": "Optional type filter: email, slack, meeting, project, commitment, event, contact, web, llm_chat"},
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
            "name": "list_goals",
            "description": (
                "List the user's goals. Returns active goals by default. "
                "Call this when the user asks about their goals, objectives, "
                "aspirations, or targets — e.g. 'what are my goals?', "
                "'show my work goals', 'which goals are overdue?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional filter: personal, work, family, learning, other",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status filter (default: active). Options: active, completed, abandoned",
                        "enum": ["active", "completed", "abandoned"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_features",
            "description": (
                "List open feature requests and bug reports. Call this when the user "
                "asks about the backlog, feature requests, open bugs, or 'what's in the queue'. "
                "Use kind='bug' to show only bugs, kind='feature' for features only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Optional filter: 'feature' or 'bug'",
                        "enum": ["feature", "bug"],
                    },
                    "show_all": {
                        "type": "boolean",
                        "description": "If true, include done/wont-do items (default: false)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": (
                "List active todos and personal commitments as a checklist. "
                "Call this when the user asks about their tasks, to-dos, or what they need to do."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_actions",
            "description": (
                "List pending agent-proposed actions from the goal/project agent. "
                "Call this when the user asks about pending actions, agent suggestions, "
                "or 'what actions are proposed?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_status": {
                        "type": "string",
                        "description": "Optional filter: 'pending', 'approved', or 'all' (default: pending)",
                    },
                },
            },
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
            "name": "get_recent_commands",
            "description": (
                "Return the output of recent slash commands the user ran in this session "
                "(e.g. /events, /commitments, /goals, /projects, /contacts, /actions, /todos). "
                "Call this when the user asks a follow-up question that references something "
                "they just listed — e.g. 'which of those is most urgent?', 'tell me more about "
                "item 3', 'which one should I focus on first?'. Returns the raw text the user saw."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of recent commands to return (default 5, max 10)",
                    }
                },
            },
        },
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
            "name": "add_feature",
            "description": (
                "File a new feature request. Use when the user asks to log a feature idea, "
                "request, or improvement. Do not use for bug reports — use add_bug instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Clear description of the feature request",
                    }
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_bug",
            "description": (
                "File a new bug report. Use when the user describes unexpected behavior, "
                "an error, or something broken. Do not use for feature requests — use add_feature instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Clear description of the bug — what happened vs what was expected",
                    }
                },
                "required": ["description"],
            },
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
    {
        "type": "function",
        "function": {
            "name": "close_issue",
            "description": (
                "Close, resolve, or update the status of a bug or feature request. "
                "Use when the user says something like 'mark that bug as done', "
                "'close feature 6d364b', or 'that issue is fixed'. "
                "Provide either short_id (the 6-char hash) or a title substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "short_id": {
                        "type": "string",
                        "description": "6-character hash ID shown in /features or /bugs listings",
                    },
                    "title": {
                        "type": "string",
                        "description": "Partial title to search for when short_id is unknown",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status to set (default: done)",
                        "enum": ["done", "wont_do", "in_progress"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_issue_priority",
            "description": (
                "Update the priority of a bug or feature request. "
                "Use when the user says something like 'set bug 2b2b14 to high priority', "
                "'mark that feature as critical', or 'lower the priority of the dark mode request'. "
                "Provide either short_id (the 6-char hash) or a title substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "short_id": {
                        "type": "string",
                        "description": "6-character hash ID shown in /features or /bugs listings",
                    },
                    "title": {
                        "type": "string",
                        "description": "Partial title to search for when short_id is unknown",
                    },
                    "priority": {
                        "type": "string",
                        "description": "New priority level",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                },
                "required": ["priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_commitment",
            "description": (
                "Mark a commitment or waiting-on item as completed or dismissed. "
                "Use when the user says something like 'I sent that report', "
                "'mark commitment 3 done', 'dismiss the dentist commitment', "
                "or 'I finished that task'. "
                "Call list_commitments first if you don't have an index or title yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_commitments result",
                    },
                    "title": {
                        "type": "string",
                        "description": "Partial title of the commitment when index is unknown",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (default: completed)",
                        "enum": ["completed", "dismissed"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_goal",
            "description": (
                "Mark a goal as completed or abandoned. Use when the user says something like "
                "'I achieved my goal of X', 'mark my fitness goal done', 'abandon the learning goal', "
                "or 'I've completed my goal to Y'. Call list_goals first if you need to confirm "
                "which goal the user means."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Partial title of the goal to find and update",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (default: completed)",
                        "enum": ["completed", "abandoned"],
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_project",
            "description": (
                "Mark a project as completed, abandoned, or on hold. Use when the user says "
                "'I finished the X project', 'mark the Y project done', 'put the Z project on hold', "
                "or 'abandon the W project'. Call list_projects first if you need to confirm "
                "which project the user means."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Partial title of the project to find and update",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (default: completed)",
                        "enum": ["completed", "abandoned", "on_hold"],
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_todo",
            "description": (
                "Create a personal todo item. Use when the user says things like "
                "'remind me to call John', 'add a todo to send the report', "
                "'I need to follow up with Jane', or 'create a task to review the doc'. "
                "For waiting-on items ('waiting for John to reply') set type to waiting_on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What needs to be done",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Optional due date in YYYY-MM-DD format",
                    },
                    "type": {
                        "type": "string",
                        "description": "Todo type (default: personal)",
                        "enum": ["personal", "waiting_on", "outbound"],
                    },
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_goal",
            "description": (
                "Get full detail for a specific goal by its list index. "
                "Call list_goals first to get the numbered list, then use "
                "get_goal with the index number to see due date, priority, "
                "linked projects, and notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_goals result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project",
            "description": (
                "Get full detail for a specific project by its list index. "
                "Call list_projects first to get the numbered list, then use "
                "get_project with the index number to see due date, priority, "
                "linked goal, and milestones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal_note",
            "description": (
                "Append a timestamped note to a goal. Use when the user says things like "
                "'add a note to my fitness goal', 'update goal 2 with this note', "
                "'log a progress update on goal 1'. "
                "Call list_goals first if you need to find the goal index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_goals result",
                    },
                    "note": {
                        "type": "string",
                        "description": "Text to append as a timestamped note",
                    },
                },
                "required": ["index", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal_due",
            "description": (
                "Set or clear the due date on a goal. Use when the user says "
                "'set the due date on goal 2 to next month', 'update deadline for my learning goal', "
                "or 'clear the due date on goal 1'. "
                "Call list_goals first if you need to find the goal index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_goals result",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in YYYY-MM-DD format, or 'none' to clear",
                    },
                },
                "required": ["index", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_note",
            "description": (
                "Append a timestamped note to a project. Use when the user says "
                "'add a note to the X project', 'log an update on project 2', "
                "'record a status note on my website project'. "
                "Call list_projects first if you need to find the project index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                    "note": {
                        "type": "string",
                        "description": "Text to append as a timestamped note",
                    },
                },
                "required": ["index", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_due",
            "description": (
                "Set or clear the due date on a project. Use when the user says "
                "'set the deadline for project 3 to June', 'update due date on X project', "
                "or 'clear the due date on project 1'. "
                "Call list_projects first if you need to find the project index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in YYYY-MM-DD format, or 'none' to clear",
                    },
                },
                "required": ["index", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_milestone",
            "description": (
                "Add a new milestone to a project. Use when the user says "
                "'add a milestone to project 2', 'create a checkpoint for the X project', "
                "'add a deliverable to my website project'. "
                "Call list_projects first if you need to find the project index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                    "text": {
                        "type": "string",
                        "description": "Description of the milestone",
                    },
                },
                "required": ["project_index", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_milestone",
            "description": (
                "Toggle a milestone on a project as done or undone. Use when the user says "
                "'mark milestone 2 on project 1 done', 'I completed milestone 3 for the X project', "
                "or 'undo milestone 1 on project 2'. "
                "Call get_project first to see the milestone list and indices."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                    "milestone_index": {
                        "type": "integer",
                        "description": "1-based position of the milestone within the project",
                    },
                },
                "required": ["project_index", "milestone_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_goal",
            "description": (
                "Link a project to a goal. Use when the user says "
                "'link project 2 to goal 1', 'connect the X project to my fitness goal', "
                "or 'associate project 3 with goal 2'. "
                "Call list_projects and list_goals first to find the indices."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                    "goal_index": {
                        "type": "integer",
                        "description": "1-based position from the last list_goals result",
                    },
                },
                "required": ["project_index", "goal_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unlink_goal",
            "description": (
                "Unlink a project from its associated goal. Use when the user says "
                "'unlink project 2 from its goal', 'remove the goal link from the X project', "
                "or 'detach project 1 from goal'. "
                "Call list_projects first if you need to find the project index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_index": {
                        "type": "integer",
                        "description": "1-based position from the last list_projects result",
                    },
                },
                "required": ["project_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_feature",
            "description": (
                "Get full detail for a specific feature request or bug report. "
                "Accepts a 1-based list index (from list_features), a GitHub issue "
                "number (e.g. '#42' or '42'), or a 6-char short_id hash. "
                "Returns title, status, priority, description, and notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index_or_id": {
                        "type": "string",
                        "description": "List index (e.g. '2'), GitHub issue number (e.g. '42' or '#42'), or 6-char short_id",
                    },
                },
                "required": ["index_or_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_feature",
            "description": (
                "Update a feature request or bug report's status, priority, or add a note. "
                "Actions: 'plan' (mark as planned), 'start' (mark in-progress), "
                "'done' (close as completed), 'wont_do' (close as won't do), "
                "'priority' (update priority), 'note' (append a note). "
                "Accepts a 1-based index, GitHub issue number, or 6-char short_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index_or_id": {
                        "type": "string",
                        "description": "List index, GitHub issue number (e.g. '42' or '#42'), or 6-char short_id",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["plan", "start", "done", "wont_do", "priority", "note"],
                        "description": "Action to perform on the feature/bug",
                    },
                    "note_or_priority": {
                        "type": "string",
                        "description": "For 'note': the note text. For 'priority': low/medium/high/critical. For 'done'/'wont_do': optional closing note.",
                    },
                },
                "required": ["index_or_id", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event",
            "description": (
                "Get full detail for a specific calendar event by its list index. "
                "Call list_events first to get the numbered list, then use get_event "
                "with the index number to see location, participants, notes, and summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_events result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting",
            "description": (
                "Get full detail for a specific meeting transcript by its list index. "
                "Call list_meetings first to get the numbered list, then use get_meeting "
                "with the index number to see participants, summary, and transcript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_meetings result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact",
            "description": (
                "Get full detail for a contact by 1-based index or by name substring. "
                "Returns name, email, relationship score, open commitments, and recent interaction summary. "
                "Auto-loads the contact list if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name_or_index": {
                        "type": "string",
                        "description": "Contact name (or substring) or 1-based index from list_contacts",
                    },
                },
                "required": ["name_or_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_comm",
            "description": (
                "Get full detail for a specific email or Slack thread by its list index. "
                "Call list_comms first to get the numbered list, then use get_comm "
                "with the index number to see participants, messages, and full thread content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_comms result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reading",
            "description": (
                "Get full detail for a specific captured web page by its list index. "
                "Call list_readings first to get the numbered list, then use get_reading "
                "with the index number to see URL, full summary, key points, and tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_readings result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_action",
            "description": (
                "Get full detail for a specific agent-proposed action by its list index. "
                "Call list_actions first to get the numbered list, then use get_action "
                "with the index number to see source goal/project, rationale, and proposed steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_actions result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_action",
            "description": (
                "Approve and execute a pending agent-proposed action by its list index. "
                "Call list_actions first to see the numbered list. Use when the user says "
                "'run action 2', 'approve that action', 'execute action N', or similar. "
                "Marks the action as executed and runs the proposed steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_actions result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_action",
            "description": (
                "Reject and dismiss a pending agent-proposed action by its list index. "
                "Call list_actions first to see the numbered list. Use when the user says "
                "'drop action 2', 'reject that action', 'dismiss action N', or similar. "
                "Marks the action as rejected so it won't appear in future lists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_actions result",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "defer_action",
            "description": (
                "Snooze a pending agent-proposed action for a number of hours. "
                "Call list_actions first to see the numbered list. Use when the user says "
                "'defer action 2', 'snooze that for a day', 'remind me about action N later'. "
                "The action won't appear in list_actions results until the defer window expires."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "1-based position from the last list_actions result",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to snooze (default 24)",
                    },
                },
                "required": ["index"],
            },
        },
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
    if name == "list_goals":
        return handler._list_goals_text(
            category=arguments.get("category"),
            status=arguments.get("status", "active"),
        )
    if name == "list_features":
        return await handler._list_features_text(
            kind=arguments.get("kind"),
            show_all=bool(arguments.get("show_all", False)),
        )
    if name == "list_todos":
        return await handler._list_todos_text()
    if name == "list_actions":
        return await handler._list_actions_text(
            filter_status=arguments.get("filter_status"),
        )
    if name == "list_projects":
        return await handler._list_projects_text(
            category=arguments.get("category"),
            limit=min(int(arguments.get("limit", 50)), 100),
        )
    if name == "list_commitments":
        return await handler._list_commitments_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_events":
        return await handler._list_events_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_meetings":
        return await handler._list_meetings_text(
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_contacts":
        return await handler._list_contacts_text(
            limit=min(int(arguments.get("limit", 30)), 200),
        )
    if name == "list_comms":
        return await handler._list_comms_text(
            kind=arguments.get("kind"),
            limit=min(int(arguments.get("limit", 20)), 100),
        )
    if name == "list_readings":
        return await handler._list_readings_text(
            limit=min(int(arguments.get("limit", 20)), 50),
        )
    if name == "search_memories":
        return await handler._search_memories_text(
            query=arguments["query"],
            type_filter=arguments.get("type"),
        )
    if name == "get_memory":
        return await handler._get_memory_text(arguments["name"])
    if name == "list_commands":
        return handler._list_commands_text()
    if name == "deliver_pending_replies":
        return await _deliver_pending(handler)
    if name == "discard_pending_replies":
        return await _discard_pending(handler)
    if name == "add_goal":
        from goals_tracker import GoalManager
        gm = GoalManager(MEMORIES_DIR, handler._config if hasattr(handler, "_config") else {})
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
        gm = GoalManager(MEMORIES_DIR, handler._config if hasattr(handler, "_config") else {})
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
    if name == "close_issue":
        return await handler._close_issue_text(
            short_id=arguments.get("short_id"),
            title=arguments.get("title"),
            status=arguments.get("status", "done"),
        )
    if name == "update_issue_priority":
        return await handler._update_issue_priority_text(
            short_id=arguments.get("short_id"),
            title=arguments.get("title"),
            priority=arguments["priority"],
        )
    if name == "close_commitment":
        return await handler._close_commitment_text(
            index=arguments.get("index"),
            title=arguments.get("title"),
            status=arguments.get("status", "completed"),
        )
    if name == "close_goal":
        return await handler._close_goal_text(
            title=arguments["title"],
            status=arguments.get("status", "completed"),
        )
    if name == "close_project":
        return await handler._close_project_text(
            title=arguments["title"],
            status=arguments.get("status", "completed"),
        )
    if name == "add_todo":
        return handler._add_todo_text(
            description=arguments["description"],
            due_date=arguments.get("due_date"),
            todo_type=arguments.get("type"),
        )
    if name == "get_goal":
        return handler._get_goal_text(int(arguments["index"]))
    if name == "get_project":
        return handler._get_project_text(int(arguments["index"]))
    if name == "update_goal_note":
        return await handler._update_goal_note_text(
            index=int(arguments["index"]),
            note=arguments["note"],
        )
    if name == "update_goal_due":
        return await handler._update_goal_due_text(
            index=int(arguments["index"]),
            due_date=arguments["due_date"],
        )
    if name == "update_project_note":
        return await handler._update_project_note_text(
            index=int(arguments["index"]),
            note=arguments["note"],
        )
    if name == "update_project_due":
        return await handler._update_project_due_text(
            index=int(arguments["index"]),
            due_date=arguments["due_date"],
        )
    if name == "add_milestone":
        return await handler._add_milestone_text(
            project_index=int(arguments["project_index"]),
            text=arguments["text"],
        )
    if name == "toggle_milestone":
        return await handler._toggle_milestone_text(
            project_index=int(arguments["project_index"]),
            milestone_index=int(arguments["milestone_index"]),
        )
    if name == "link_goal":
        return await handler._link_goal_text(
            project_index=int(arguments["project_index"]),
            goal_index=int(arguments["goal_index"]),
        )
    if name == "unlink_goal":
        return await handler._unlink_goal_text(
            project_index=int(arguments["project_index"]),
        )
    if name == "get_feature":
        return await handler._get_feature_text(str(arguments["index_or_id"]))
    if name == "update_feature":
        return await handler._update_feature_text(
            index_or_id=str(arguments["index_or_id"]),
            action=str(arguments["action"]),
            note_or_priority=arguments.get("note_or_priority"),
        )
    if name == "get_event":
        return handler._get_event_text(int(arguments["index"]))
    if name == "get_meeting":
        return handler._get_meeting_text(int(arguments["index"]))
    if name == "get_contact":
        return await handler._get_contact_text(str(arguments["name_or_index"]))
    if name == "get_comm":
        return handler._get_comm_text(int(arguments["index"]))
    if name == "get_reading":
        return handler._get_reading_text(int(arguments["index"]))
    if name == "get_action":
        return handler._get_action_text(int(arguments["index"]))
    if name == "run_action":
        return await handler._run_action_text(int(arguments["index"]))
    if name == "drop_action":
        return await handler._drop_action_text(int(arguments["index"]))
    if name == "defer_action":
        return await handler._defer_action_text(
            int(arguments["index"]),
            hours=int(arguments.get("hours", 24)),
        )
    if name in ("add_feature", "add_bug"):
        import hashlib, os, re, yaml
        from datetime import datetime
        from pathlib import Path
        description = arguments.get("description", "").strip()
        if not description:
            return "Error: description is required."
        kind = "bug" if name == "add_bug" else "feature"
        tags = [t[1:].lower() for t in re.findall(r'#\w+', description)]
        clean_desc = re.sub(r'#\w+', '', description).strip()
        title = " ".join(clean_desc.split()[:8])
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40].strip('-')
        id_hash = hashlib.sha1(f"{description}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
        filename = f"feature-request-{slug}-{id_hash}.md"
        fm = {
            "title": clean_desc[:100],
            "type": "feature_request",
            "kind": kind,
            "status": "new",
            "priority": "medium",
            "created": datetime.now().isoformat(),
            "tags": tags,
            "short_id": id_hash,
        }
        if kind == "bug":
            body = (
                f"## Bug\n\n{clean_desc}\n\n"
                f"## Expected\n\n\n\n"
                f"## Actual\n\n\n\n"
                f"## Steps to reproduce\n\n\n\n"
                f"## Notes\n\nCaptured via add_bug tool at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
            )
        else:
            body = (
                f"## Request\n\n{clean_desc}\n\n"
                f"## Context\n\nCaptured via add_feature tool at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
                f"## Notes\n\n"
            )
        MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
        target = MEMORIES_DIR / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.rename(tmp, target)
        # NOTE: This write bypasses memory_writer, so cache won't auto-invalidate.
        # The 60s sweep loop will catch it.
        label = "Bug" if kind == "bug" else "Feature"
        return f"{label} captured: '{clean_desc[:60]}' (ID: {id_hash})"
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
