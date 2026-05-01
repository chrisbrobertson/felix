#!/usr/bin/env python3
"""Promote locally-captured feature/bug requests to GitHub issues.

Reads `feature-request-*.md` files from the iCloud memories directory,
creates a GitHub issue per file via the `gh` CLI, and stamps the file's
frontmatter with `github_issue_number`. The file is kept in memories/ so
local memory context remains available — mirrors the semantics of
`cmd_feature_import` in `chat_handler.py`.

Usage:
  scripts/promote_local_features.py [--dry-run] [--repo OWNER/NAME]

Exits 0 if everything succeeds (including "nothing to promote").
Exits non-zero if any `gh` call fails — the bash wrapper aborts in that
case so we don't silently leak local reports.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"

# Mirrors _STANDARD_LABELS in github_client.py — must stay in sync.
STANDARD_LABELS = [
    {"name": "kind:feature",       "color": "0075ca", "description": "New feature or enhancement"},
    {"name": "kind:bug",           "color": "d73a4a", "description": "Something isn't working"},
    {"name": "status:planned",     "color": "cfd3d7", "description": "Planned but not started"},
    {"name": "status:in-progress", "color": "fbca04", "description": "Currently being worked on"},
    {"name": "priority:low",       "color": "e4e669", "description": "Low priority"},
    {"name": "priority:medium",    "color": "ffa500", "description": "Normal priority"},
    {"name": "priority:high",      "color": "e11d48", "description": "High priority"},
    {"name": "priority:critical",  "color": "b60205", "description": "Critical — blocking"},
]


def gh_ensure_labels(repo: str | None) -> None:
    """Bootstrap the standard label vocabulary on the GitHub repo (idempotent).

    Uses --force so the call is safe to re-run; warns on unexpected errors
    but never aborts — a missing label is logged, not fatal here.
    """
    for lb in STANDARD_LABELS:
        cmd = [
            "gh", "label", "create", lb["name"],
            "--color", lb["color"],
            "--description", lb["description"],
            "--force",
        ]
        if repo:
            cmd += ["--repo", repo]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"WARN: could not ensure label {lb['name']!r}: {result.stderr.strip()}",
                  file=sys.stderr)


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def split_body(text: str) -> str:
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) >= 3 else text


def rewrite_frontmatter(path: Path, updates: dict) -> None:
    text = path.read_text()
    if not text.startswith("---"):
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        return
    _, fm_text, body = parts
    fm = yaml.safe_load(fm_text) or {}
    fm.update(updates)
    new_text = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---{body}"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text)
    os.rename(tmp, path)


def gh_create_issue(title: str, body: str, labels: list[str], repo: str | None) -> int:
    """Run `gh issue create`, return new issue number. Raises on failure."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        body_path = f.name
    try:
        cmd = ["gh", "issue", "create", "--title", title, "--body-file", body_path]
        for label in labels:
            cmd += ["--label", label]
        if repo:
            cmd += ["--repo", repo]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    finally:
        os.unlink(body_path)
    # Last non-empty line of stdout is the issue URL: .../issues/123
    for line in reversed(result.stdout.strip().splitlines()):
        m = re.search(r"/issues/(\d+)", line)
        if m:
            return int(m.group(1))
    raise RuntimeError(f"Could not parse issue number from gh output: {result.stdout!r}")


def gh_close_issue(number: int, reason: str, repo: str | None) -> None:
    cmd = ["gh", "issue", "close", str(number), "--reason", reason]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def collect_pending(memories_dir: Path) -> list[tuple[Path, dict]]:
    pending = []
    for f in sorted(memories_dir.glob("feature-request-*.md")):
        text = f.read_text()
        fm = parse_frontmatter(text)
        if fm.get("type") != "feature_request":
            continue
        if fm.get("github_issue_number"):
            continue
        pending.append((f, fm))
    return pending


def build_labels(fm: dict) -> list[str]:
    kind = fm.get("kind", "feature")
    priority = fm.get("priority", "medium")
    tags = fm.get("tags") or []
    labels = [f"kind:{kind}", f"priority:{priority}"] + list(tags)
    status = fm.get("status", "new")
    if status == "planned":
        labels.append("status:planned")
    elif status == "in-progress":
        labels.append("status:in-progress")
    return labels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be promoted; don't touch anything.")
    ap.add_argument("--repo", default=None,
                    help="OWNER/NAME for gh -R; defaults to git remote inference.")
    ap.add_argument("--delay-seconds", type=float, default=2.0,
                    help="Sleep this long between gh issue create calls to avoid "
                         "GitHub anti-abuse throttling on large batches (default: 2.0). "
                         "Set to 0 to disable.")
    args = ap.parse_args()

    memories_dir = BRAIN_DIR / "memories"
    if not memories_dir.is_dir():
        print(f"memories dir not found: {memories_dir}", file=sys.stderr)
        return 1

    pending = collect_pending(memories_dir)
    if not pending:
        print("Nothing to promote.")
        return 0

    if not args.dry_run:
        gh_ensure_labels(args.repo)

    print(f"{len(pending)} file(s) to promote:")
    for f, fm in pending:
        print(f"  • [{fm.get('kind', 'feature')}] {fm.get('title', f.stem)[:70]}  ({f.name})")

    if args.dry_run:
        return 0

    failures = 0
    for idx, (f, fm) in enumerate(pending):
        title = (fm.get("title") or f.stem)[:100]
        body = split_body(f.read_text()).strip() or fm.get("title", title)
        labels = build_labels(fm)
        try:
            number = gh_create_issue(title, body, labels, args.repo)
        except subprocess.CalledProcessError as e:
            print(f"FAILED to create issue for {f.name}: {e.stderr.strip()}", file=sys.stderr)
            failures += 1
            continue
        # Stamp the local file with the GitHub issue number and keep it in memories/
        # so local memory context remains available (mirrors cmd_feature_import behavior).
        rewrite_frontmatter(f, {"github_issue_number": number})
        status = fm.get("status", "new")
        if status == "done":
            try:
                gh_close_issue(number, "completed", args.repo)
            except subprocess.CalledProcessError as e:
                print(f"WARN: created #{number} but close failed: {e.stderr.strip()}", file=sys.stderr)
        elif status == "wont-do":
            try:
                gh_close_issue(number, "not planned", args.repo)
            except subprocess.CalledProcessError as e:
                print(f"WARN: created #{number} but close failed: {e.stderr.strip()}", file=sys.stderr)
        print(f"  → #{number}  {f.name}")
        if args.delay_seconds > 0 and idx < len(pending) - 1:
            time.sleep(args.delay_seconds)

    if failures:
        print(f"{failures} failure(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
