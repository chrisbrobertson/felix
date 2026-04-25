"""Unit tests for memory_writer.py."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import memory_writer as mw

SAMPLE_ENTRY = {
    "url": "https://docs.litellm.ai/docs/routing",
    "title": "LiteLLM Router Documentation",
    "visit_count": 2,
    "browser": "chrome",
}

SAMPLE_BODY = """\
## Summary
LiteLLM's router supports fallback chains and load balancing.

## Key Points
- Fallback chains defined in config YAML
- Supports custom api_base for local endpoints

## Entities
- **LiteLLM**: open-source LLM proxy

**Tags:** litellm, routing, llm, infrastructure"""


@pytest.fixture
def memories_dir(tmp_path):
    return tmp_path / "memories"


@pytest.fixture
def writer(memories_dir):
    with patch.object(mw, "MEMORIES_DIR", memories_dir):
        yield mw.MemoryWriter()


async def test_creates_memories_dir(writer, memories_dir):
    assert not memories_dir.exists()
    await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    assert memories_dir.exists()


async def test_filename_format(writer, memories_dir):
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    # Format: YYYY-MM-DD-{slug}-{6-char-hash}.md
    assert filename.endswith(".md")
    stem = Path(filename).stem
    parts = stem.split("-")
    assert parts[0].isdigit() and len(parts[0]) == 4   # year
    assert parts[1].isdigit() and len(parts[1]) == 2   # month
    assert parts[2].isdigit() and len(parts[2]) == 2   # day
    assert len(parts[-1]) == 6                          # url hash


async def test_same_url_produces_same_filename(writer, memories_dir):
    f1 = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    f2 = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    assert f1 == f2


async def test_different_url_produces_different_filename(writer, memories_dir):
    entry2 = {**SAMPLE_ENTRY, "url": "https://example.com/other"}
    f1 = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    f2 = await writer.write(entry2, SAMPLE_BODY)
    assert f1 != f2


async def test_frontmatter_fields_present(writer, memories_dir):
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["source_url"] == SAMPLE_ENTRY["url"]
    assert fm["source_title"] == SAMPLE_ENTRY["title"]
    assert fm["visit_count"] == 2
    assert fm["browser"] == "chrome"
    assert "created" in fm
    assert "hostname" in fm
    assert "id" in fm
    assert "summary" in fm


async def test_frontmatter_source_fields_come_first(writer, memories_dir):
    """source_title, source_url, summary must be the first 3 frontmatter fields."""
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    raw = (memories_dir / filename).read_text()
    # Strip opening ---\n and grab first few lines
    lines = raw.split("\n")
    assert lines[0] == "---"
    assert lines[1].startswith("source_title:")
    assert lines[2].startswith("source_url:")
    assert lines[3].startswith("summary:")


async def test_summary_extracted_from_body(writer, memories_dir):
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert "LiteLLM" in fm["summary"]


async def test_summary_empty_when_no_summary_section(writer, memories_dir):
    body = "## Key Points\n- No summary section here."
    filename = await writer.write(SAMPLE_ENTRY, body)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["summary"] == ""


async def test_tags_extracted_from_body(writer, memories_dir):
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert "litellm" in fm["tags"]
    assert "routing" in fm["tags"]
    assert "llm" in fm["tags"]
    assert "infrastructure" in fm["tags"]


async def test_body_preserved_in_content(writer, memories_dir):
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    content = (memories_dir / filename).read_text()
    assert "## Summary" in content
    assert "LiteLLM's router supports" in content


async def test_atomic_write_leaves_no_tmp_file(writer, memories_dir):
    await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    assert list(memories_dir.glob("*.tmp")) == []


async def test_empty_tags_when_none_in_body(writer, memories_dir):
    body = "## Summary\nNo tags here.\n\n## Key Points\n- Something"
    filename = await writer.write(SAMPLE_ENTRY, body)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["tags"] == []


async def test_title_slug_truncated_to_50_chars(writer, memories_dir):
    entry = {**SAMPLE_ENTRY, "title": "A" * 200}
    filename = await writer.write(entry, SAMPLE_BODY)
    # Slug portion (between date prefix and hash suffix) must not be enormous
    stem = Path(filename).stem
    assert len(stem) < 80  # 10 (date) + ~50 (slug) + 1 (-) + 6 (hash) = ~67


async def test_falls_back_to_url_when_title_missing(writer, memories_dir):
    entry = {"url": "https://example.com/page", "visit_count": 1, "browser": "chrome"}
    # Should not raise even without a "title" key
    filename = await writer.write(entry, SAMPLE_BODY)
    assert filename.endswith(".md")


async def test_write_with_depth_deep(writer, memories_dir):
    """depth=deep should appear in written frontmatter."""
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY, depth="deep")
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["depth"] == "deep"


async def test_write_default_depth_standard(writer, memories_dir):
    """omitting depth should write depth: standard."""
    filename = await writer.write(SAMPLE_ENTRY, SAMPLE_BODY)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["depth"] == "standard"


# --- _canonicalize_url ---

from memory_writer import _canonicalize_url


def test_canonicalize_strips_utm_params():
    url = "https://example.com/page?utm_source=email&utm_medium=newsletter"
    assert _canonicalize_url(url) == "https://example.com/page"


def test_canonicalize_strips_fragment():
    url = "https://example.com/page#section-3"
    assert _canonicalize_url(url) == "https://example.com/page"


def test_canonicalize_lowercases_host():
    url = "HTTPS://Example.COM/Path"
    assert _canonicalize_url(url) == "https://example.com/Path"


def test_canonicalize_keeps_content_params():
    url = "https://example.com/page?page=2&sort=asc"
    assert _canonicalize_url(url) == "https://example.com/page?page=2&sort=asc"


def test_canonicalize_strips_tracking_but_keeps_content_params():
    url = "https://example.com/page?page=2&utm_source=email&fbclid=abc"
    result = _canonicalize_url(url)
    assert "page=2" in result
    assert "utm_source" not in result
    assert "fbclid" not in result


def test_canonicalize_strips_all_tracking_params():
    params = [
        "utm_source=x", "utm_medium=y", "utm_campaign=z",
        "fbclid=1", "gclid=2", "_ga=3", "mc_cid=4", "yclid=5", "igshid=6",
    ]
    url = "https://example.com/page?" + "&".join(params)
    assert _canonicalize_url(url) == "https://example.com/page"


def test_canonicalize_same_page_different_tracking_params_same_hash():
    """Two visits to the same page with different utm params → identical hash."""
    import hashlib
    url1 = "https://example.com/article?utm_source=email"
    url2 = "https://example.com/article?utm_source=twitter"
    h1 = hashlib.sha1(_canonicalize_url(url1).encode()).hexdigest()[:6]
    h2 = hashlib.sha1(_canonicalize_url(url2).encode()).hexdigest()[:6]
    assert h1 == h2


async def test_tracking_params_do_not_produce_new_file(writer, memories_dir):
    """Same URL visited via two referrers produces one memory file, not two."""
    entry1 = {**SAMPLE_ENTRY, "url": "https://example.com/page?utm_source=email"}
    entry2 = {**SAMPLE_ENTRY, "url": "https://example.com/page?utm_source=twitter"}
    f1 = await writer.write(entry1, SAMPLE_BODY)
    f2 = await writer.write(entry2, SAMPLE_BODY)
    assert f1 == f2


async def test_source_url_stored_as_original(writer, memories_dir):
    """source_url frontmatter stores the original URL, not the canonical form."""
    entry = {**SAMPLE_ENTRY, "url": "https://example.com/page?utm_source=email"}
    filename = await writer.write(entry, SAMPLE_BODY)
    parts = (memories_dir / filename).read_text().split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["source_url"] == "https://example.com/page?utm_source=email"
