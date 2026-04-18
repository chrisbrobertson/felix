"""
Watchlist checker utility.

Provides `check_watchlists(memory_path, memories_dir, notify_fn)` which:
1. Loads all active watchlist-*.md files
2. Checks if a newly written memory matches any watchlist
3. Marks matching watchlists as triggered and calls notify_fn

Called by email_scanner, slack_scanner, zoom_scanner after writing memories.
"""
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

log = logging.getLogger("watchlist-checker")


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown file."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _extract_body(text: str) -> str:
    """Extract body content from markdown file (everything after frontmatter)."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].strip()


def _keywords_match(keywords: str, text: str) -> bool:
    """Check if all space-separated keywords appear in text (case-insensitive)."""
    if not keywords or not text:
        return False
    text_lower = text.lower()
    keyword_list = keywords.lower().split()
    return all(kw in text_lower for kw in keyword_list)


def _person_match(person: Optional[str], memory_fm: dict, memory_body: str) -> bool:
    """Check if person appears in participants or body (case-insensitive substring)."""
    if not person:
        return True  # No person filter = always match

    person_lower = person.lower()

    # Check participants field (can be list of strings or list of dicts)
    participants = memory_fm.get("participants", [])
    for p in participants:
        if isinstance(p, dict):
            name = (p.get("name") or "").lower()
            email = (p.get("email") or "").lower()
            if person_lower in name or person_lower in email:
                return True
        elif isinstance(p, str):
            if person_lower in p.lower():
                return True

    # Check body text
    if person_lower in memory_body.lower():
        return True

    return False


def _type_match(watch_type: str, memory_type: Optional[str]) -> bool:
    """Check if memory type matches watchlist watch_type filter."""
    if watch_type == "any":
        return True

    # Map watch_type to expected memory type values
    type_map = {
        "email": "email_thread",
        "slack": "slack_thread",
        "meeting": "meeting_transcript",
    }

    expected = type_map.get(watch_type)
    if expected is None:
        return True  # Unknown watch_type = no filter

    return memory_type == expected


def check_watchlists(
    memory_path: Path,
    memories_dir: Path,
    notify_fn: Optional[Callable]
) -> int:
    """
    Check if a newly written memory matches any active watchlist.

    Args:
        memory_path: Path to the newly written memory file
        memories_dir: Directory containing all memory files
        notify_fn: Optional async callback(message: str) for notifications

    Returns:
        Count of watchlists triggered
    """
    if not memory_path.exists():
        return 0

    # Load the memory that was just written
    try:
        memory_text = memory_path.read_text(encoding="utf-8")
        memory_fm = _parse_frontmatter(memory_text)
        memory_body = _extract_body(memory_text)
        memory_type = memory_fm.get("type")
        memory_title = memory_fm.get("source_title", "")
    except Exception as e:
        log.warning("Failed to read memory %s: %s", memory_path.name, e)
        return 0

    # Find all active watchlist files
    watchlist_files = list(memories_dir.glob("watchlist-*.md"))
    if not watchlist_files:
        return 0

    triggered_count = 0
    now = datetime.now(timezone.utc)

    for watchlist_path in watchlist_files:
        try:
            wl_text = watchlist_path.read_text(encoding="utf-8")
            wl_fm = _parse_frontmatter(wl_text)

            # Skip if not active
            status = wl_fm.get("status", "")
            if status != "active":
                continue

            # Check expiry
            expires = wl_fm.get("expires")
            if expires:
                try:
                    expires_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if now >= expires_dt:
                        # Mark as expired
                        _mark_watchlist_status(watchlist_path, wl_text, wl_fm, "expired")
                        log.debug("Watchlist %s expired", watchlist_path.name)
                        continue
                except (ValueError, TypeError):
                    pass

            # Check type filter
            watch_type = wl_fm.get("watch_type", "any")
            if not _type_match(watch_type, memory_type):
                continue

            # Check person filter
            person = wl_fm.get("person")
            if not _person_match(person, memory_fm, memory_body):
                continue

            # Check topic keywords
            topic = wl_fm.get("topic", "")
            if not topic:
                continue

            # Topic must match either title or body
            combined_text = f"{memory_title} {memory_body}"
            if not _keywords_match(topic, combined_text):
                continue

            # Match found — trigger watchlist
            _mark_watchlist_status(watchlist_path, wl_text, wl_fm, "triggered")
            triggered_count += 1

            # Send notification
            if notify_fn:
                person_part = f" from {person}" if person else ""
                msg = (
                    f"🔔 Watchlist triggered: {topic}{person_part}\n"
                    f"Matched: {memory_title[:60]}"
                )
                try:
                    import asyncio
                    # notify_fn is async — schedule it
                    asyncio.create_task(notify_fn(msg))
                except Exception as e:
                    log.warning("Failed to send watchlist notification: %s", e)

            log.info("Watchlist triggered: %s → %s", watchlist_path.name, memory_path.name)

        except Exception:
            log.exception("Error checking watchlist %s", watchlist_path.name)

    return triggered_count


def _mark_watchlist_status(watchlist_path: Path, old_text: str, old_fm: dict, new_status: str):
    """Update watchlist status in frontmatter and write atomically."""
    try:
        # Update frontmatter
        old_fm["status"] = new_status
        old_fm["triggered_at"] = datetime.now(timezone.utc).isoformat()

        # Rebuild file
        body = _extract_body(old_text)
        frontmatter = yaml.dump(old_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{frontmatter}---\n\n{body}\n"

        # Atomic write
        tmp_path = watchlist_path.with_suffix(".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        os.rename(str(tmp_path), str(watchlist_path))

    except Exception as e:
        log.warning("Failed to update watchlist %s status: %s", watchlist_path.name, e)
