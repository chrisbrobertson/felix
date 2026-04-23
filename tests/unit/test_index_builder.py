"""Unit tests for index_builder.py."""
import errno
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import index_builder as ib


@pytest.fixture
def brain_dir(tmp_path):
    d = tmp_path / "brain"
    d.mkdir()
    (d / "memories").mkdir()
    (d / "config.yaml").write_text("memory:\n  index_rebuild_interval: 3600\n")
    return d


@pytest.fixture
def builder(brain_dir):
    with patch.object(ib, "BRAIN_DIR", brain_dir), \
         patch.object(ib, "INDEX_PATH", brain_dir / "index.md"):
        yield ib.IndexBuilder()


def make_memory(memories_dir: Path, name: str, hostname: str = "mac-studio",
                body: str = "content") -> Path:
    p = memories_dir / name
    p.write_text(f"---\nhostname: {hostname}\n---\n\n{body}")
    return p


# --- _build ---

async def test_build_skips_when_no_memories(builder, brain_dir):
    await builder._build()
    assert not (brain_dir / "index.md").exists()


async def test_build_writes_index_file(builder, brain_dir):
    make_memory(brain_dir / "memories", "2026-04-11-test-abc123.md", body="useful content")

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Synthesized index text."
    with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)):
        await builder._build()

    assert (brain_dir / "index.md").exists()
    text = (brain_dir / "index.md").read_text()
    assert "Synthesized index text." in text


async def test_build_index_contains_timestamp_and_count(builder, brain_dir):
    for i in range(3):
        make_memory(brain_dir / "memories", f"2026-04-11-file{i}-{i:06x}.md")

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Index."
    with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)):
        await builder._build()

    text = (brain_dir / "index.md").read_text()
    assert "Last updated:" in text
    assert "3 memories indexed" in text


async def test_build_passes_memory_content_to_llm(builder, brain_dir):
    make_memory(brain_dir / "memories", "2026-04-11-test-abc123.md", body="unique content xyz")

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Index."
    with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)) as mock_ac:
        await builder._build()

    call_args = mock_ac.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "unique content xyz" in user_msg


async def test_build_caps_input_at_max_chars(builder, brain_dir):
    """Verify oversized memory files are truncated in the LLM input."""
    huge_body = "x" * (ib.MAX_INPUT_CHARS + 10_000)
    make_memory(brain_dir / "memories", "2026-04-11-huge-abc123.md", body=huge_body)

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Index."
    with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)) as mock_ac:
        await builder._build()

    user_msg = mock_ac.call_args.kwargs["messages"][1]["content"]
    assert len(user_msg) <= ib.MAX_INPUT_CHARS + 500  # tolerance for prompt prefix


async def test_build_handles_llm_error_gracefully(builder, brain_dir):
    make_memory(brain_dir / "memories", "2026-04-11-test-abc123.md")
    with patch("index_builder.acompletion", new=AsyncMock(side_effect=Exception("rate limited"))):
        # Should not raise
        await builder._build()
    # Index file should not be written (or previous version preserved)
    assert not (brain_dir / "index.md").exists()


# --- _log_watcher_health ---

def test_health_logs_hostname(builder, brain_dir, caplog):
    memories = brain_dir / "memories"
    make_memory(memories, "2026-04-11-a-aaa111.md", hostname="mac-studio")
    make_memory(memories, "2026-04-11-b-bbb222.md", hostname="macbook-pro")

    files = sorted(memories.glob("*.md"))
    with caplog.at_level(logging.INFO, logger="index-builder"):
        builder._log_watcher_health(files)

    messages = [r.message for r in caplog.records if "Health:" in r.message]
    hostnames = {m.split("last memory from ")[1].split(" ")[0] for m in messages}
    assert "mac-studio" in hostnames
    assert "macbook-pro" in hostnames


def test_health_warns_when_last_memory_over_1hr_old(builder, brain_dir, caplog):
    memories = brain_dir / "memories"
    p = make_memory(memories, "2026-04-11-old-aaa111.md", hostname="silent-node")
    stale_time = time.time() - 7200  # 2 hours ago
    os.utime(p, (stale_time, stale_time))

    with caplog.at_level(logging.WARNING, logger="index-builder"):
        builder._log_watcher_health([p])

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("silent-node" in w for w in warnings)


def test_health_uses_info_when_memory_recent(builder, brain_dir, caplog):
    memories = brain_dir / "memories"
    p = make_memory(memories, "2026-04-11-fresh-aaa111.md", hostname="active-node")
    # mtime is "just now" by default

    with caplog.at_level(logging.INFO, logger="index-builder"):
        builder._log_watcher_health([p])

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and "active-node" in r.message]
    assert len(info_msgs) >= 1


def test_health_only_reads_first_20_files(builder, brain_dir):
    """Should inspect at most 20 files (per spec — avoids full dir scan on large brains)."""
    memories = brain_dir / "memories"
    paths = [make_memory(memories, f"2026-04-11-file{i:02d}-{i:06x}.md") for i in range(30)]

    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        nonlocal read_count
        read_count += 1
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", counting_read_text):
        builder._log_watcher_health(paths)

    assert read_count <= 20


# --- iCloud deadlock retry ---

async def test_build_retries_on_errno_11_and_succeeds(builder, brain_dir):
    """errno 11 (EDEADLK) on first attempt retried; succeeds on second attempt."""
    make_memory(brain_dir / "memories", "2026-04-11-test-abc123.md", body="retried content")

    deadlock_err = OSError(errno.EDEADLK, "Resource deadlock avoided")
    original_read = Path.read_text
    # Track per-file call counts so _log_watcher_health's read doesn't interfere.
    # _log_watcher_health reads the file first (call 1); the build retry loop
    # reads it second (call 2 = first attempt, call 3 = retry that succeeds).
    file_call_count: dict = {}

    def flaky_read(self, *args, **kwargs):
        key = str(self)
        file_call_count[key] = file_call_count.get(key, 0) + 1
        # Raise deadlock on the 2nd call total (first call in the build retry loop)
        if "abc123" in self.name and file_call_count[key] == 2:
            raise deadlock_err
        return original_read(self, *args, **kwargs)

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Index after retry."

    with patch.object(Path, "read_text", flaky_read):
        with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)):
            with patch("index_builder.asyncio.sleep", new=AsyncMock()) as mock_sleep:
                await builder._build()

    # Should have slept at least once (retry backoff) and still written the index
    mock_sleep.assert_called()
    assert (brain_dir / "index.md").exists()


async def test_build_skips_file_after_3_deadlock_retries(builder, brain_dir, caplog):
    """File that deadlocks 3 times is skipped with a warning; build continues."""
    make_memory(brain_dir / "memories", "2026-04-11-good-aaa111.md", body="good content")
    make_memory(brain_dir / "memories", "2026-04-11-locked-bbb222.md", body="locked")

    deadlock_err = OSError(errno.EDEADLK, "Resource deadlock avoided")
    original_read = Path.read_text

    def always_deadlock_locked(self, *args, **kwargs):
        if "locked" in self.name:
            raise deadlock_err
        return original_read(self, *args, **kwargs)

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Partial index."

    with caplog.at_level(logging.WARNING, logger="index-builder"):
        with patch.object(Path, "read_text", always_deadlock_locked):
            with patch("index_builder.acompletion", new=AsyncMock(return_value=mock_resp)):
                with patch("index_builder.asyncio.sleep", new=AsyncMock()):
                    await builder._build()

    # Index should still be written (from the readable file)
    assert (brain_dir / "index.md").exists()
    # Should warn about the skipped file
    assert any("iCloud lock" in r.message for r in caplog.records)


async def test_run_loop_continues_after_build_crash(builder, brain_dir):
    """If _build raises unexpectedly, run_loop logs error and continues (no propagation)."""
    stop_event = ib.asyncio.Event()

    call_count = {"n": 0}

    async def mock_build():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Unexpected crash")
        stop_event.set()  # Stop after second call

    with patch.object(builder, "_build", mock_build):
        with patch("index_builder.yaml.safe_load", return_value={}):
            with patch("index_builder.asyncio.wait_for", new=AsyncMock()):
                await builder.run_loop(stop_event)

    # Should have called _build at least twice (continued after crash)
    assert call_count["n"] >= 2
