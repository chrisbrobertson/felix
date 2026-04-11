#!/usr/bin/env python3
"""
migrate_memories.py — rewrite existing memory files to the new frontmatter layout.

New field order: source_title, source_url, summary, id, created, visit_count,
tags, browser, hostname.

Safe to run multiple times (idempotent: files already in the new format are
rewritten in place with the same content).
"""
import os
import re
import sys
import yaml
from pathlib import Path

from memory_writer import _extract_summary

MEMORIES_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"

FIELD_ORDER = [
    "source_title", "source_url", "summary",
    "id", "created", "visit_count", "tags", "browser", "hostname",
]


def migrate_file(path: Path) -> str:
    text = path.read_text()

    # Parse frontmatter
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not m:
        return "skip (no frontmatter)"

    try:
        fm = yaml.safe_load(m.group(1))
    except Exception as e:
        return f"skip (yaml error: {e})"

    body = m.group(2).lstrip("\n")

    # Add summary field if missing
    if not fm.get("summary"):
        fm["summary"] = _extract_summary(body)

    # Rebuild frontmatter in new field order, unknown fields appended at end
    ordered = {}
    for key in FIELD_ORDER:
        if key in fm:
            ordered[key] = fm[key]
    for key, val in fm.items():
        if key not in ordered:
            ordered[key] = val

    new_content = f"---\n{yaml.dump(ordered, sort_keys=False)}---\n\n{body}\n"

    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_content)
    os.rename(tmp, path)
    return "ok"


def main():
    if not MEMORIES_DIR.exists():
        print(f"Memories dir not found: {MEMORIES_DIR}")
        sys.exit(1)

    files = sorted(MEMORIES_DIR.glob("*.md"))
    if not files:
        print("No memory files found.")
        return

    ok = skip = errors = 0
    for f in files:
        result = migrate_file(f)
        print(f"  {f.name[:60]:<60}  {result}")
        if result == "ok":
            ok += 1
        elif result.startswith("skip"):
            skip += 1
        else:
            errors += 1

    print(f"\n{ok} migrated, {skip} skipped, {errors} errors")


if __name__ == "__main__":
    main()
