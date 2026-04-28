"""Unit tests for heartbeat module."""
import json
from pathlib import Path
import pytest
import heartbeat as hb


@pytest.fixture(autouse=True)
def _reset_heartbeat_state():
    """Reset global heartbeat state between tests."""
    hb._brain_dir = None
    hb._role = "unknown"
    hb._version = "unknown"
    hb._daemon_started = ""
    hb._loop_state.clear()
    yield
    hb._brain_dir = None
    hb._role = "unknown"
    hb._version = "unknown"
    hb._daemon_started = ""
    hb._loop_state.clear()


def test_record_beat_noop_before_init():
    """record_beat is silent when init() has not been called."""
    hb.record_beat("some_loop", "ok")  # must not raise


def test_init_sets_brain_dir(tmp_path):
    hb.init(tmp_path, "full", "1.2.3")
    assert hb._brain_dir == tmp_path
    assert hb._role == "full"
    assert hb._version == "1.2.3"
    assert hb._daemon_started != ""


def test_record_beat_writes_file(tmp_path):
    hb.init(tmp_path, "watcher", "2.0.0")
    hb.record_beat("browser_watcher", "ok")

    files = list(tmp_path.glob("heartbeat-*.json"))
    assert len(files) == 1

    data = json.loads(files[0].read_text())
    assert data["role"] == "watcher"
    assert data["version"] == "2.0.0"
    assert "browser_watcher" in data["loops"]
    assert data["loops"]["browser_watcher"]["status"] == "ok"
    assert data["loops"]["browser_watcher"]["error"] is None


def test_record_beat_error_preserves_message(tmp_path):
    hb.init(tmp_path, "full", "1.0.0")
    hb.record_beat("code_scanner", "error", "Connection refused")

    files = list(tmp_path.glob("heartbeat-*.json"))
    data = json.loads(files[0].read_text())
    assert data["loops"]["code_scanner"]["status"] == "error"
    assert data["loops"]["code_scanner"]["error"] == "Connection refused"


def test_record_beat_multiple_loops(tmp_path):
    hb.init(tmp_path, "full", "1.0.0")
    hb.record_beat("browser_watcher", "ok")
    hb.record_beat("email_scanner", "error", "timeout")
    hb.record_beat("code_scanner", "ok")

    files = list(tmp_path.glob("heartbeat-*.json"))
    data = json.loads(files[0].read_text())
    assert len(data["loops"]) == 3
    assert data["loops"]["email_scanner"]["status"] == "error"


def test_record_beat_updates_last_heartbeat(tmp_path):
    hb.init(tmp_path, "full", "1.0.0")
    hb.record_beat("loop_a", "ok")
    data1 = json.loads(list(tmp_path.glob("heartbeat-*.json"))[0].read_text())

    hb.record_beat("loop_b", "ok")
    data2 = json.loads(list(tmp_path.glob("heartbeat-*.json"))[0].read_text())

    assert "last_heartbeat" in data2
    assert data2["last_heartbeat"] >= data1["last_heartbeat"]


def test_read_all_returns_empty_for_no_files(tmp_path):
    result = hb.read_all(tmp_path)
    assert result == []


def test_read_all_returns_multiple_instances(tmp_path):
    for host in ["macstudio", "macbook-pro"]:
        data = {
            "hostname": host,
            "role": "full" if host == "macstudio" else "watcher",
            "version": "1.0.0",
            "daemon_started": "2026-04-28T10:00:00+00:00",
            "last_heartbeat": "2026-04-28T14:00:00+00:00",
            "loops": {"browser_watcher": {"last_run": "2026-04-28T14:00:00+00:00", "status": "ok", "error": None}},
        }
        (tmp_path / f"heartbeat-{host}.json").write_text(json.dumps(data))

    results = hb.read_all(tmp_path)
    assert len(results) == 2
    hostnames = {r["hostname"] for r in results}
    assert hostnames == {"macstudio", "macbook-pro"}


def test_read_all_skips_malformed_files(tmp_path):
    (tmp_path / "heartbeat-badhost.json").write_text("{invalid json")
    (tmp_path / "heartbeat-goodhost.json").write_text(
        json.dumps({"hostname": "goodhost", "loops": {}})
    )
    results = hb.read_all(tmp_path)
    assert len(results) == 1
    assert results[0]["hostname"] == "goodhost"


def test_flush_uses_atomic_rename(tmp_path):
    """Verify no .tmp file is left behind after flush."""
    hb.init(tmp_path, "full", "1.0.0")
    hb.record_beat("test_loop", "ok")

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Leftover .tmp file: {tmp_files}"
    assert len(list(tmp_path.glob("heartbeat-*.json"))) == 1
