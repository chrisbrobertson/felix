---
specmas: 3.0
kind: feature
id: feat-llm-chat-import
version: 1.0.0
created: 2026-04-17
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-document-upload
---

# LLM Chat History Import

## Overview

### Problem Statement

Users have significant conversation history in Claude and ChatGPT that represents a valuable record of thinking, decisions, and research. This history is not accessible to Felix. Filed as feature `7e1220`.

Neither Claude nor ChatGPT offers a conversations API — history is only available via data export. The solution is a batch import triggered by the user uploading their export file to the Telegram bot.

### Scope

**In scope:**
- ChatGPT export: `conversations.json` from Settings → Data Controls → Export Data
- Claude export: `conversations.json` from Settings → Privacy → Export Data
- Each conversation becomes one `llm-chat-{platform}-{date}-{slug}-{id}.md` memory
- `type: llm_chat`, `platform: claude|chatgpt` frontmatter
- LLM summarization of each conversation (using `summarize` route)
- `/import_chats` command: user uploads JSON or ZIP file, bot processes in background
- Progress report via Telegram ("Imported 47 of 120 conversations…")
- Deduplication: conversation ID in `llm-chat-import-state.json` prevents re-import

**Out of scope:**
- Real-time sync (not possible without API access)
- Gemini, Copilot, or other LLM platform exports
- Individual message-level memories (conversation granularity only)
- ZIP uploads containing multiple JSON files (single JSON only — user extracts if needed)

### Success Metrics

- User uploads ChatGPT `conversations.json` → memories created for each conversation
- `/comms` or free-form chat can reference imported conversations
- Re-uploading the same export doesn't create duplicates

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `/import_chats <platform>` command (platform: `claude` or `chatgpt`) — prompts user to upload the JSON file |
| FR-2 | Bot waits for next document message in the same chat (30s timeout) |
| FR-3 | ChatGPT format: `conversations.json` array of conversation objects with `id`, `title`, `create_time`, `mapping` (message tree) |
| FR-4 | Claude format: `conversations.json` array with `uuid`, `name`, `created_at`, `chat_messages` |
| FR-5 | Each conversation flattened to chronological text: "User: ... Assistant: ..." |
| FR-6 | Conversations shorter than 100 words skipped (too brief to be useful) |
| FR-7 | LLM summarization of each conversation text via `summarize` route |
| FR-8 | Memory written as `llm-chat-{platform}-{date}-{slug}-{conv-id}.md` |
| FR-9 | `platform: claude\|chatgpt`, `type: llm_chat` in frontmatter |
| FR-10 | Conversation ID stored in `llm-chat-import-state.json` dedup set; existing IDs skipped |
| FR-11 | Progress sent as Telegram message every 10 conversations processed |
| FR-12 | `/comms` command `llm` filter shows llm_chat memories |

---

## Design

### New `llm_chat_importer.py`

```python
class LLMChatImporter:
    def __init__(self, memories_dir, deploy_dir, executor): ...

    async def import_from_bytes(self, data: bytes, platform: str,
                                 progress_cb=None) -> tuple[int, int]:
        """Returns (imported_count, skipped_count)."""
        convs = self._parse(data, platform)
        state = self._load_state()
        seen = set(state.get("imported_ids", []))
        imported = skipped = 0
        for i, conv in enumerate(convs):
            if conv["id"] in seen:
                skipped += 1
                continue
            text = self._flatten(conv, platform)
            if len(text.split()) < 100:
                skipped += 1
                continue
            summary = await self._summarize(text, conv, platform)
            if summary:
                self._write_memory(conv, summary, platform)
                seen.add(conv["id"])
                imported += 1
            if progress_cb and i % 10 == 0:
                await progress_cb(imported, i + 1, len(convs))
        state["imported_ids"] = list(seen)
        self._save_state(state)
        return imported, skipped

    def _parse(self, data: bytes, platform: str) -> list[dict]: ...
    def _flatten(self, conv: dict, platform: str) -> str: ...
    async def _summarize(self, text, conv, platform) -> Optional[str]: ...
    def _write_memory(self, conv, summary, platform): ...
```

### `/import_chats` command in `chat_handler.py`

```python
async def cmd_import_chats(self, update, context):
    if not context.args or context.args[0] not in ("claude", "chatgpt"):
        await update.message.reply_text("Usage: /import_chats claude|chatgpt\nThen send the JSON file.")
        return
    platform = context.args[0]
    self._pending_import[chat_id] = platform
    await update.message.reply_text(f"Ready. Send your {platform} conversations.json now (30s timeout).")
```

Next document upload for that chat_id triggers `_handle_import_upload()`.

### Memory format

```markdown
---
type: llm_chat
platform: chatgpt
source_title: "Discussing RAG architecture options"
created: 2026-03-15T14:22:00
conversation_id: abc-def-123
tags: [rag, architecture, llm]
---

## Summary

Chris and the assistant discussed three RAG architecture patterns...

## Key Topics

- Dense vs sparse retrieval trade-offs
- Context window management strategies
- Evaluation with RAGAS
```

---

## Test Plan

**Unit tests in `tests/unit/test_llm_chat_importer.py`:**

1. `test_parse_chatgpt_format` — ChatGPT JSON → list of conversation dicts
2. `test_parse_claude_format` — Claude JSON → list of conversation dicts
3. `test_flatten_chatgpt_conversation` — message tree → chronological text
4. `test_flatten_claude_conversation` — chat_messages → chronological text
5. `test_short_conversation_skipped` — <100 words → skipped_count +1
6. `test_deduplication_skips_seen_ids` — re-importing same conversation → no new memory
7. `test_memory_written_with_correct_frontmatter` — platform, type, conversation_id in FM
8. `test_progress_callback_fires_every_10` — mock 25 convs → callback called at 10 and 20

**Unit tests in `tests/unit/test_chat_handler.py`:**

9. `test_cmd_import_chats_unknown_platform` — bad platform arg → usage message
10. `test_cmd_import_chats_sets_pending_state` — valid platform → pending_import set
