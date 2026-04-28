"""Memory synthesis scanner — clusters related memories and generates synthesis insights."""
import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from litellm import acompletion

from llm_routes import resolve
from usage_tracker import record_usage
from memory_writer import MemoryWriter
from heartbeat import record_beat

log = logging.getLogger("synthesis-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
MEMORIES_DIR = BRAIN_DIR / "memories"
STATE_FILE = DEPLOY_DIR / "synthesis-state.json"

# Excluded types (not topical reading material):
SKIP_TYPES = {"synthesis", "code", "goal", "project", "calendar_event", "commitment"}

# English stop words to exclude from Jaccard similarity
STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "will", "just",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def load_state() -> dict:
    """Load state from STATE_FILE."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {"processed_clusters": []}
    return {"processed_clusters": []}


def save_state(state: dict):
    """Atomically save state to STATE_FILE."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.rename(str(tmp), str(STATE_FILE))
    except Exception as e:
        log.warning("Failed to save synthesis scanner state: %s", e)


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a memory file. Returns {} on any failure."""
    try:
        text = path.read_text()
    except Exception:
        return {}
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def _tokenize(text: str) -> set:
    """
    Extract meaningful words from text for Jaccard similarity.
    Keeps words ≥3 chars that aren't stop words.
    """
    if not text:
        return set()
    words = re.findall(r'\b\w{3,}\b', text.lower())
    return {w for w in words if w not in STOP_WORDS}


def _jaccard(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings using word sets."""
    set_a = _tokenize(a)
    set_b = _tokenize(b)

    if not set_a or not set_b:
        return 0.0

    intersection = set_a & set_b
    union = set_a | set_b

    return len(intersection) / len(union)


def _cluster_hash(paths: list[Path]) -> str:
    """Generate a stable hash for a cluster based on sorted filenames."""
    filenames = sorted([p.name for p in paths])
    key = "|".join(filenames)
    return hashlib.sha1(key.encode()).hexdigest()


async def _build_clusters(cache, memories_dir: Path) -> list[list[Path]]:
    """
    Build clusters of related memories.

    Two memories are related if they share ≥2 tags OR have title Jaccard ≥0.40.
    Returns connected components of size ≥3, excluding SKIP_TYPES.
    """
    memory_files = []
    metadata = {}

    # Read all memory files and parse frontmatter via cache
    all_rows = await cache.query_all(exclude_types=list(SKIP_TYPES))

    for row in all_rows:
        fm = json.loads(row["frontmatter"])
        path = memories_dir / row["filename"]
        memory_files.append(path)
        metadata[path] = fm

    # Build adjacency list for graph
    related = {f: set() for f in memory_files}

    for i, f1 in enumerate(memory_files):
        fm1 = metadata[f1]
        tags1 = set(fm1.get("tags", []))
        title1 = fm1.get("source_title", "")

        for f2 in memory_files[i+1:]:
            fm2 = metadata[f2]
            tags2 = set(fm2.get("tags", []))
            title2 = fm2.get("source_title", "")

            # Check if related by tags (≥2 shared)
            shared_tags = tags1 & tags2
            if len(shared_tags) >= 2:
                related[f1].add(f2)
                related[f2].add(f1)
                continue

            # Check if related by title similarity
            if title1 and title2:
                similarity = _jaccard(title1, title2)
                if similarity >= 0.40:
                    related[f1].add(f2)
                    related[f2].add(f1)

    # Find connected components using DFS
    visited = set()
    clusters = []

    def dfs(node: Path, component: set):
        visited.add(node)
        component.add(node)
        for neighbor in related[node]:
            if neighbor not in visited:
                dfs(neighbor, component)

    for f in memory_files:
        if f not in visited:
            component = set()
            dfs(f, component)
            if len(component) >= 3:
                clusters.append(list(component))

    return clusters


async def _synthesize(cluster_paths: list[Path], config: dict) -> Optional[str]:
    """
    Generate a synthesis insight from a cluster of related memories.

    Returns the synthesis body text, or None if synthesis fails.
    """
    # Build prompt from cluster summaries
    memories_info = []
    for path in cluster_paths:
        fm = _parse_frontmatter(path)
        title = fm.get("source_title", path.name)
        summary = fm.get("summary", "")
        if title and summary:
            memories_info.append(f"**{title}**\n{summary}")

    if not memories_info:
        log.warning("No valid memories to synthesize in cluster")
        return None

    n = len(memories_info)
    memories_text = "\n\n".join(memories_info)

    prompt = (
        f"You are analyzing a cluster of related memories from a personal knowledge system.\n\n"
        f"The following {n} memories share related topics:\n\n"
        f"{memories_text}\n\n"
        f"Generate a synthesis insight with:\n"
        f"**Synthesis**: 2-3 sentences describing what these memories collectively reveal.\n"
        f"**Cross-cutting themes** (3-5 bullets): Patterns, ideas, or connections that appear across multiple memories.\n"
        f"**Implication**: 1-2 sentences on what this cluster suggests about current focus or interests.\n\n"
        f"Keep under 500 words."
    )

    try:
        resp = await acompletion(
            model=resolve("chat"),
            messages=[{"role": "user", "content": prompt}],
            timeout=30,
        )
        if hasattr(resp, "usage") and resp.usage:
            record_usage(resolve("chat"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
        return resp.choices[0].message.content.strip()
    except Exception:
        log.exception("LLM call failed for synthesis generation")
        return None


# ── SynthesisScanner ──────────────────────────────────────────────────────────


class SynthesisScanner:
    def __init__(self, role: str = "full", cache=None):
        self.role = role
        # Cache: MemoryCache instance for queries, or None (defaults to pass-through)
        if cache is None:
            from memory_cache import MemoryCache
            cache = MemoryCache(None, MEMORIES_DIR, enabled=False)
        self._cache = cache

    async def run_loop(self, stop_event: asyncio.Event):
        """Main loop — runs every 3600 seconds (1 hour) by default."""
        interval = 3600  # 1 hour
        log.info("Synthesis scanner started — scanning every %ds", interval)

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self.run_once()
            except Exception as exc:
                log.exception("Uncaught error in synthesis scanner cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("synthesis_scanner", beat_status, beat_error)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self, max_clusters: int = 5) -> int:
        """
        Run one synthesis scan cycle.

        Args:
            max_clusters: Maximum number of new clusters to process per cycle

        Returns:
            Number of synthesis memories written
        """
        clusters = await _build_clusters(self._cache, MEMORIES_DIR)
        state = load_state()
        processed = set(state.get("processed_clusters", []))

        new_count = 0

        for cluster in clusters[:max_clusters]:
            cluster_id = _cluster_hash(cluster)

            # Skip already-processed clusters
            if cluster_id in processed:
                continue

            log.info("Processing cluster with %d memories (hash: %s)", len(cluster), cluster_id[:8])

            # Generate synthesis
            config = {}  # Config not currently used
            body = await _synthesize(cluster, config)
            if not body:
                log.warning("Synthesis generation failed for cluster %s", cluster_id[:8])
                continue

            # Extract info for memory entry
            titles = [_parse_frontmatter(p).get("source_title", "") for p in cluster]
            first_title = titles[0] if titles and titles[0] else "cluster"

            # Slugify first title for readability
            slug = re.sub(r'[^a-z0-9]+', '-', first_title[:40].lower()).strip('-')
            if not slug:
                slug = "cluster"

            uid = cluster_id[:6]
            source_files = [p.name for p in cluster]

            # Build memory entry
            entry = {
                "url": f"synthesis://{cluster_id}",
                "title": f"Synthesis: {first_title}",
                "browser": "synthesis",
                "type": "synthesis",
                "content_type": "synthesis",
                "source_files": source_files,
            }

            # Write synthesis memory
            try:
                writer = MemoryWriter()
                filename = await writer.write(entry, body)
                log.info("Wrote synthesis memory: %s", filename)
                new_count += 1

                # Mark as processed
                processed.add(cluster_id)
            except Exception:
                log.exception("Failed to write synthesis memory for cluster %s", cluster_id[:8])
                continue

        # Save state
        state["processed_clusters"] = list(processed)
        save_state(state)

        if new_count > 0:
            log.info("Synthesis scan complete — %d synthesis memory/memories written", new_count)

        return new_count
