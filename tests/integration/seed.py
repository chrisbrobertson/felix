"""Seed helpers for writing properly-formatted memory files in integration tests."""
import hashlib
import re
import uuid
import yaml
from datetime import datetime, timezone
from pathlib import Path


def _write_memory(path: Path, fm: dict, body: str) -> Path:
    """Serialize frontmatter dict as YAML and write the memory file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
    path.write_text(content)
    return path


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert text to a slug."""
    s = text.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:max_len].rstrip('-')


def _stable_id(source_url: str, title: str) -> str:
    """Generate stable ID for deduplication."""
    key = f"{source_url}:{title.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def commitment(brain_dir, *, title="Do thing", status="active", source="meeting", confidence=0.9) -> Path:
    """Write a commitment memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    source_url = f"{source}:test123"
    stable_id = _stable_id(source_url, title)
    slug = _slugify(title)
    path = memories / f"commitment-{slug}-{stable_id}.md"
    path.write_text(
        f"---\ntype: commitment\nsource_title: {title}\nsummary: Commitment\n"
        f"tags: []\nsource_url: {source_url}\ncommitment_type: outbound\n"
        f"owner: Chris\nassignee: Chris\nconfidence: {confidence}\n"
        f"status: {status}\ndue_date:\n---\n\n## Details\n{title}\n"
    )
    return path


def action(brain_dir, *, title="Reach out", status="pending") -> Path:
    """Write an action memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    action_id = uuid.uuid4().hex[:8]
    path = memories / f"action-{slug}-{action_id}.md"
    fm = {
        "type": "agent_action",
        "source_title": title,
        "summary": "Action",
        "tags": [],
        "status": status,
        "source_type": "goal",
        "source_slug": "test",
        "source_goal_or_project": "Test Goal",
        "target": title,
        "action_id": action_id,
        "action_type": "followup",
        "rationale": "Test rationale for automated action",
        "proposed_steps": ["Step 1: do something", "Step 2: verify it worked"],
        "proposed_at": "2026-04-27T10:00:00",
    }
    return _write_memory(path, fm, f"## Details\n{title}\n")



def calendar_event(brain_dir, *, title="Standup", start_iso="2026-04-28T09:00:00-07:00") -> Path:
    """Write a calendar event memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    event_id = uuid.uuid4().hex[:8]
    hostname = "test-host"
    date = start_iso.split("T")[0]
    path = memories / f"calendar-event-{hostname}-{date}-{slug}-{event_id}.md"
    # Derive end_time by bumping the hour; keep timezone suffix if present
    end_iso = start_iso[:10] + "T10:00:00" + start_iso[19:]
    fm = {
        "type": "calendar_event",
        "source_title": title,
        "summary": "Meeting",
        "tags": [],
        "source_url": f"calendar:{event_id}",
        "hostname": hostname,
        "participants": ["alice@example.com"],
        "start_time": start_iso,
        "end_time": end_iso,
        "last_scanned": "2026-04-27T10:00:00",
    }
    return _write_memory(path, fm, f"## Summary\n{title}\n")


def email_thread(brain_dir, *, subject="Re: Planning", classification="human") -> Path:
    """Write an email thread memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(subject)
    conv_id = uuid.uuid4().hex[:8]
    path = memories / f"email-thread-{slug}-{conv_id}.md"
    fm = {
        "type": "email_thread",
        "source_title": subject,
        "summary": "Email thread",
        "tags": [],
        "source_url": f"email:{conv_id}",
        "classification": classification,
        "participants": ["alice@example.com"],
        "last_message": "2026-04-27T10:00:00",
        "message_count": 3,
        "last_scanned": "2026-04-27T10:00:00",
    }
    return _write_memory(path, fm, "## Messages\n- Alice: Message\n")


def meeting(brain_dir, *, title="Q2 Planning") -> Path:
    """Write a meeting transcript memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    meeting_id = uuid.uuid4().hex[:8]
    date = "2026-04-27"
    path = memories / f"meeting-{date}-{slug}-{meeting_id}.md"
    path.write_text(
        f"---\ntype: meeting_transcript\nsource_title: {title}\nsummary: Meeting\n"
        f"tags: []\nsource_url: zoom:{meeting_id}\nparticipants: [alice@example.com]\n"
        f"speakers: [Alice]\nduration_minutes: 30\nmeeting_date: {date}T10:00:00\n"
        f"zoom_meeting_id: {meeting_id}\nlast_scanned: 2026-04-27T10:00:00\n---\n\n## Transcript\n- Alice: Hello\n"
    )
    return path


def contact(brain_dir, *, name="Alex Kim", email="alex@example.com") -> Path:
    """Write a contact memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(name)
    path = memories / f"contact-{slug}.md"
    path.write_text(
        f"---\ntype: contact\nsource_title: {name}\nsummary: Contact\n"
        f"tags: []\nemail: {email}\nname: {name}\nrelationship_score: 0.5\n"
        f"interaction_count: 5\nlast_interaction: '2026-04-27T10:00:00'\n---\n\n## Summary\nContact info\n"
    )
    return path


def goal(brain_dir, *, title="Ship X", status="active") -> Path:
    """Write a goal memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stable_id = _stable_id("goal", title)
    slug = _slugify(title)
    path = memories / f"goal-{slug}-{stable_id}.md"
    path.write_text(
        f"---\ntype: goal\ncategory: work\nsource_title: {title}\nsummary: Goal\n"
        f"tags: []\ncreated: '{created}'\ndue_date:\nstatus: {status}\n"
        f"priority: medium\nlinked_projects: []\nnotes:\n---\n\n## Notes\n"
    )
    return path


def project(brain_dir, *, title="Project Y", status="active", category="work") -> Path:
    """Write a project memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stable_id = _stable_id("project", title)
    slug = _slugify(title)
    path = memories / f"project-{category}-{slug}-{stable_id}.md"
    path.write_text(
        f"---\ntype: project\ncategory: {category}\nsource_title: {title}\nsummary: Project\n"
        f"tags: []\ncreated: '{created}'\ndue_date:\nstatus: {status}\n"
        f"priority: medium\nlinked_goal:\nmilestones: []\ninferred_from: []\nnotes:\n---\n\n## Notes\n"
    )
    return path


def candidate(brain_dir, *, title="Project Z", status="pending_confirmation") -> Path:
    """Write a project candidate memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    cand_id = uuid.uuid4().hex[:8]
    path = memories / f"project-candidate-{slug}-{cand_id}.md"
    path.write_text(
        f"---\ntype: project_candidate\nsource_title: {title}\nsummary: Candidate\n"
        f"tags: []\nstatus: {status}\nconfidence: 0.8\ninferred_from: []\n"
        f"category: work\n---\n\n## Summary\nCandidate project\n"
    )
    return path


def feature_request_item(brain_dir, *, title="Add feature A", kind="feature", status="new", priority="medium") -> Path:
    """Write a feature-request-*.md file in the format cmd_features expects."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    req_id = uuid.uuid4().hex[:6]
    path = memories / f"feature-request-{slug}-{req_id}.md"
    fm = {
        "title": title,
        "type": "feature_request",
        "kind": kind,
        "status": status,
        "priority": priority,
        "created": "2026-04-27T10:00:00",
        "tags": [],
        "short_id": req_id,
    }
    return _write_memory(path, fm, f"## Request\n\n{title}\n")


def feature_request(brain_dir, *, title="Add feature A", kind="feature", status="new") -> Path:
    """Write a feature request memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    req_id = uuid.uuid4().hex[:8]
    path = memories / f"{kind}-{slug}-{req_id}.md"
    path.write_text(
        f"---\ntype: {kind}\nsource_title: {title}\nsummary: Request\n"
        f"tags: []\nstatus: {status}\npriority: medium\n"
        f"created: 2026-04-27T10:00:00\n---\n\n## Details\n{title}\n"
    )
    return path


def skill_draft(brain_dir, *, name="summarize-foo") -> Path:
    """Write a skill draft memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    draft_id = uuid.uuid4().hex[:8]
    path = memories / f"skill-draft-{name}-{draft_id}.md"
    path.write_text(
        f"---\ntype: skill_draft\nsource_title: {name}\nsummary: Draft skill\n"
        f"tags: []\nstatus: pending\ncreated: 2026-04-27T10:00:00\n---\n\n# {name}\n\nPrompt here.\n"
    )
    return path


def watchlist(brain_dir, *, topic="rust async") -> Path:
    """Write a watchlist memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(topic)
    watch_id = uuid.uuid4().hex[:8]
    path = memories / f"watchlist-{slug}-{watch_id}.md"
    path.write_text(
        f"---\ntype: watchlist\nsource_title: {topic}\nsummary: Watchlist\n"
        f"tags: []\nstatus: active\ncreated: 2026-04-27T10:00:00\n---\n\n## Query\n{topic}\n"
    )
    return path


def dedup_pair(brain_dir) -> tuple[Path, Path]:
    """Write two duplicate reading files for dedup testing."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)

    p1 = memories / "2026-04-27-test-page-abc123.md"
    p1.write_text(
        "---\ntype: reading\nsource_title: Test Page\nsummary: A test\n"
        "tags: [test]\nsource_url: https://example.com/test\n---\n\n## Summary\nTest content\n"
    )

    p2 = memories / "2026-04-27-test-page-def456.md"
    p2.write_text(
        "---\ntype: reading\nsource_title: Test Page\nsummary: A test\n"
        "tags: [test]\nsource_url: https://example.com/test\n---\n\n## Summary\nTest content\n"
    )

    return p1, p2


def reading(brain_dir, *, url="https://example.com/x", title="Page X") -> Path:
    """Write a reading memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:6]
    date = datetime.now().strftime("%Y-%m-%d")
    path = memories / f"{date}-{slug}-{url_hash}.md"
    path.write_text(
        f"---\ntype: reading\nsource_title: {title}\nsummary: Reading\n"
        f"tags: []\nsource_url: {url}\n---\n\n## Summary\nContent from {url}\n"
    )
    return path


def circle(brain_dir, *, name="Family", icloud_folder="Family Circle") -> Path:
    """Write a circle config memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(name)
    path = memories / f"circle-{slug}.md"
    path.write_text(
        f"---\ntype: circle\nsource_title: {name}\nsummary: Circle\n"
        f"tags: []\nicloud_folder: {icloud_folder}\nmembers: []\n"
        f"last_sync: 2026-04-27T10:00:00\n---\n\n## Summary\n{name} circle\n"
    )
    return path


def apple_note(brain_dir, *, title="My Note", folder="Personal", has_todos: bool = False) -> Path:
    """Write an apple_notes memory file."""
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    slug = _slugify(title)
    import hashlib
    id_hash = hashlib.sha1(f"{folder}:{title}".encode()).hexdigest()[:6]
    folder_slug = _slugify(folder)
    path = memories / f"apple-notes-{folder_slug}-{slug}-{id_hash}.md"
    path.write_text(
        f"---\nsource_title: {title}\ntype: apple_notes\nfolder: {folder}\n"
        f"has_todos: {str(has_todos).lower()}\nmodified: '2026-05-01'\n"
        f"last_scanned: '2026-05-01T10:00:00'\ntags: [apple-notes]\n"
        f"---\n\n# {title}\n\nSome note content here.\n"
    )
    return path


def report_config(deploy_dir, *, name="weekly-digest", schedule="weekly") -> Path:
    """Write a report config file (in deploy_dir, not memories)."""
    reports_dir = deploy_dir / "reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"{name}.json"
    import json
    path.write_text(json.dumps({
        "name": name,
        "schedule": schedule,
        "type": "digest",
        "sources": ["email", "meetings"],
        "status": "active"
    }, indent=2))
    return path
