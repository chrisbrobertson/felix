---
title: "Slack Interface (Bidirectional)"
version: 0.1.0
status: draft
maturity: 0
created: 2026-04-12
updated: 2026-04-12
---

# Slack Interface (Bidirectional)

**Status: Draft — not scheduled. Captures future intent only.**

## Problem

The second brain's interactive interface is currently Telegram-only. Users who spend their day in Slack must context-switch to Telegram to run `/commitments`, `/search`, `/briefing`, or any other command. For users where Slack is the primary communication surface, this friction means the second brain is less useful day-to-day.

## Target Capabilities

Mirror the full Telegram command surface in a Slack DM:

- `/search <query>` — keyword search across memories
- `/briefing` — daily morning digest (calendar, commitments, memory highlights)
- `/commitments [N]` — open commitments list
- `/contacts [N]` — recent contacts
- `/events [N]` — upcoming calendar events
- `/meetings [N]` — recent Zoom meeting transcripts
- `/projects [N]` — active git projects
- `/memories [N]` — recent memory files

Proactive notifications (daily briefing, pre-meeting context, deadline alerts) posted to a designated private Slack DM in addition to (or instead of) Telegram.

## Design Questions (to resolve before implementation)

1. **Event delivery:** Slack Events API (requires a public webhook endpoint) vs. Socket Mode (no public URL, long-lived WebSocket connection). Socket Mode is simpler for a personal install with no public server.
2. **Dual-transport or toggle:** Should Telegram and Slack interfaces run simultaneously (both active), or should the user pick one via config? A config toggle (`notification_manager.transport: telegram|slack|both`) is the likely answer.
3. **Message-length handling:** Slack has different character limits and block formatting. Does the chat handler need transport-aware chunking, or is a simpler truncate-and-link approach sufficient?
4. **Code structure:** New `slack_chat_handler.py` module (mirrors `chat_handler.py`), or refactor `chat_handler.py` to be transport-agnostic with pluggable Telegram/Slack frontends?
5. **Token reuse:** The `SLACK_USER_TOKEN` introduced in v1.2.0 of the Slack scanner can be extended with `chat:write` and `im:write` scopes for outbound messaging — no new app needed.

## Dependencies

- Builds on the user-token model introduced in `feat-slack-scanner.md` v1.2.0 (same `xoxp-` token, extended scopes).
- Does not block on or require the Slack scanner to be active.

## Out of Scope for This Spec

- Implementation details — this is a design placeholder only.
- Any timeline or sprint assignment.
