import hashlib
import json
import os
import re
import yaml
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

MEMORIES_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"


def import_file(file_bytes: bytes, filename: str, memories_dir: Path) -> list[str]:
    """Parse file_bytes (ZIP or JSON) and write memory files. Returns list of written filenames."""
    # Try to detect ZIP format
    is_zip = filename.lower().endswith('.zip')
    if not is_zip:
        # Try to detect ZIP by magic bytes
        try:
            with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
                is_zip = True
        except zipfile.BadZipFile:
            is_zip = False

    if is_zip:
        # Extract JSON from ZIP — validate paths to prevent ZIP slip
        MAX_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB
        with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
            # Filter out unsafe paths (ZIP slip prevention)
            json_files = [
                f for f in zf.namelist()
                if f.endswith('.json')
                and '..' not in f
                and not f.startswith('/')
                and not f.startswith('\\')
            ]
            if not json_files:
                raise ValueError("No JSON files found in ZIP")

            # Zip bomb check: total uncompressed size
            total_size = sum(zf.getinfo(f).file_size for f in json_files)
            if total_size > MAX_UNCOMPRESSED:
                raise ValueError(f"ZIP content exceeds {MAX_UNCOMPRESSED // (1024*1024)} MB limit")

            # Prefer conversations.json if it exists (ChatGPT format)
            json_file = 'conversations.json' if 'conversations.json' in json_files else json_files[0]
            file_bytes = zf.read(json_file)

    # Parse JSON
    try:
        data = json.loads(file_bytes)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    if not isinstance(data, list):
        raise ValueError("JSON must be a list of conversations")

    # Detect format
    if not data:
        return []

    # ChatGPT format detection: has "mapping" key
    # Claude format detection: has "messages" key
    sample = data[0]
    if "mapping" in sample:
        conversations = _parse_chatgpt(data)
    elif "messages" in sample:
        conversations = _parse_claude(data)
    else:
        raise ValueError("Unknown conversation format (expected ChatGPT or Claude export)")

    # Write memory files
    written = []
    for conv in conversations:
        filename = _write_conversation(conv, memories_dir)
        written.append(filename)

    return written


def _parse_chatgpt(data: list) -> list[dict]:
    """Parse ChatGPT conversations.json data. Returns list of {title, platform, date, messages, id}."""
    conversations = []

    for item in data:
        title = item.get("title", "Untitled conversation")
        create_time = item.get("create_time", 0)
        date = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat() if create_time else datetime.now().isoformat()

        # Extract messages from mapping
        mapping = item.get("mapping", {})
        messages = []

        # Build list of (timestamp, role, content) tuples
        for node_id, node in mapping.items():
            msg = node.get("message")
            if not msg:
                continue

            author = msg.get("author", {})
            role = author.get("role")
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", {})
            parts = content.get("parts", [])
            text = " ".join(str(p) for p in parts if p)

            if not text:
                continue

            msg_time = msg.get("create_time", 0)
            messages.append((msg_time, role, text))

        # Sort by timestamp
        messages.sort(key=lambda x: x[0])

        # Skip empty conversations
        if not messages:
            continue

        # Generate stable ID from title + create_time
        id_input = f"{title}-{create_time}"
        conv_id = hashlib.sha1(id_input.encode()).hexdigest()[:12]

        conversations.append({
            "title": title,
            "platform": "chatgpt",
            "date": date,
            "messages": [(role, text) for _, role, text in messages],
            "id": conv_id,
        })

    return conversations


def _parse_claude(data: list) -> list[dict]:
    """Parse Claude conversations JSON data. Returns same shape."""
    conversations = []

    for item in data:
        title = item.get("name", "Untitled conversation")
        created_at = item.get("created_at", "")

        # Parse ISO timestamp
        try:
            date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).isoformat()
        except (ValueError, AttributeError):
            date = datetime.now().isoformat()

        # Extract messages
        messages = []
        for msg in item.get("messages", []):
            sender = msg.get("sender")
            text = msg.get("text", "")

            if sender not in ("human", "assistant"):
                continue

            if not text:
                continue

            # Map Claude sender to standard role
            role = "user" if sender == "human" else "assistant"
            messages.append((role, text))

        # Skip empty conversations
        if not messages:
            continue

        # Use UUID from Claude export or generate stable ID
        conv_id = item.get("uuid", hashlib.sha1(title.encode()).hexdigest()[:12])

        conversations.append({
            "title": title,
            "platform": "claude",
            "date": date,
            "messages": messages,
            "id": conv_id,
        })

    return conversations


def _write_conversation(conv: dict, memories_dir: Path) -> str:
    """Write one conversation as a memory file. Returns filename."""
    memories_dir.mkdir(parents=True, exist_ok=True)

    # Extract data
    title = conv["title"]
    platform = conv["platform"]
    date = conv["date"]
    messages = conv["messages"]
    conv_id = conv["id"]

    # Create filename
    date_part = date[:10]  # YYYY-MM-DD
    title_slug = re.sub(r'[^a-z0-9]+', '-', title[:50].lower()).strip('-')
    filename = f"llm-chat-{platform}-{date_part}-{title_slug}-{conv_id}.md"

    # Generate summary from first assistant message
    summary = ""
    for role, text in messages:
        if role == "assistant":
            # First 2 sentences
            sentences = re.split(r'(?<=[.!?])\s+', text.strip())
            summary = " ".join(sentences[:2])
            break

    if not summary and messages:
        # Fallback: first user message
        sentences = re.split(r'(?<=[.!?])\s+', messages[0][1].strip())
        summary = " ".join(sentences[:2])

    # Truncate summary to reasonable length
    if len(summary) > 300:
        summary = summary[:297] + "..."

    # Build frontmatter
    frontmatter = {
        "type": "llm_chat",
        "platform": platform,
        "source_title": title,
        "created": date,
        "summary": summary,
        "message_count": len(messages),
        "tags": [],
        "id": conv_id,
    }

    # Build body - first 3 exchanges, truncated
    body_lines = ["## Summary", summary, "", "## Conversation"]

    exchange_count = 0
    for role, text in messages:
        if exchange_count >= 6:  # 3 exchanges (user + assistant each)
            break

        # Truncate long messages
        truncated = text[:500]
        if len(text) > 500:
            truncated += "..."

        # Format role
        role_label = "User" if role == "user" else "Assistant"
        body_lines.append(f"**{role_label}:** {truncated}")
        body_lines.append("")
        exchange_count += 1

    if len(messages) > 6:
        body_lines.append(f"... ({len(messages) - 6} more messages)")

    body = "\n".join(body_lines)

    # Write file
    content = f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n\n{body}\n"

    # Atomic write
    target = memories_dir / filename
    tmp_path = target.with_suffix(".tmp")
    tmp_path.write_text(content)
    os.rename(tmp_path, target)

    return filename
