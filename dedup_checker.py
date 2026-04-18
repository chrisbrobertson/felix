"""Memory deduplication checker — URL auto-merge + title similarity candidates."""
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

import yaml

log = logging.getLogger("dedup-checker")

# Common URL tracking parameters to strip during normalization
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}

# English stop words to exclude from Jaccard similarity
STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "will", "just",
}


def _normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication by:
    - Stripping tracking parameters (utm_*, ref, fbclid, gclid, etc.)
    - Removing www. prefix
    - Lowercasing scheme and host
    - Stripping trailing slash
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url)

        # Parse and filter query params
        query_params = parse_qs(parsed.query)
        clean_params = {k: v for k, v in query_params.items() if k not in TRACKING_PARAMS}
        clean_query = "&".join(f"{k}={v[0]}" for k, v in sorted(clean_params.items()))

        # Normalize host: lowercase, strip www.
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        # Rebuild URL
        scheme = parsed.scheme.lower()
        path = parsed.path.rstrip("/") if parsed.path else ""

        normalized = f"{scheme}://{host}{path}"
        if clean_query:
            normalized += f"?{clean_query}"

        return normalized
    except Exception as e:
        log.warning("Failed to normalize URL '%s': %s", url, e)
        return url


def _jaccard(a: str, b: str) -> float:
    """
    Compute Jaccard similarity between two title strings using word sets.
    Skips stop words and words < 3 chars.
    """
    if not a or not b:
        return 0.0

    def tokenize(s: str) -> set:
        words = re.findall(r'\b\w{3,}\b', s.lower())
        return {w for w in words if w not in STOP_WORDS}

    set_a = tokenize(a)
    set_b = tokenize(b)

    if not set_a or not set_b:
        return 0.0

    intersection = set_a & set_b
    union = set_a | set_b

    return len(intersection) / len(union)


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


def _get_file_body_length(path: Path) -> int:
    """Return the length of file content (used as richness proxy)."""
    try:
        return len(path.read_text())
    except Exception:
        return 0


def run(memories_dir: Path, deploy_dir: Path, notify_fn: Optional[Callable] = None) -> dict:
    """
    Main deduplication entry point. Runs in two passes:

    Pass 1 — URL auto-merge: Files with same (type, normalized_source_url) are merged,
    keeping the file with the longest body and deleting the rest.

    Pass 2 — Title candidates: Files with same type but different URLs are compared
    by title Jaccard similarity. Pairs with ≥0.70 similarity become candidates unless
    already dismissed.

    Returns:
        {"auto_merged": int, "new_candidates": int}
    """
    state_file = deploy_dir / "dedup-state.json"

    # Load state
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            state = {"candidates": [], "dismissed": []}
    else:
        state = {"candidates": [], "dismissed": []}

    # Ensure state has required keys
    state.setdefault("candidates", [])
    state.setdefault("dismissed", [])

    auto_merged = 0
    new_candidates = 0

    # Read all memory files
    memory_files = sorted(memories_dir.glob("*.md"))

    # Build index: filename -> frontmatter
    file_metadata = {}
    for path in memory_files:
        fm = _parse_frontmatter(path)
        file_metadata[path.name] = {
            "path": path,
            "type": fm.get("type", ""),
            "source_url": fm.get("source_url", ""),
            "source_title": fm.get("source_title", ""),
            "frontmatter": fm,
        }

    # Pass 1: URL auto-merge
    # Group by (type, normalized_url) where url is non-empty
    url_groups = {}
    for filename, meta in file_metadata.items():
        if not meta["source_url"]:
            continue
        norm_url = _normalize_url(meta["source_url"])
        if not norm_url:
            continue
        key = (meta["type"], norm_url)
        url_groups.setdefault(key, []).append((filename, meta))

    for key, group in url_groups.items():
        if len(group) < 2:
            continue

        # Find the richest file (longest body)
        group.sort(key=lambda x: _get_file_body_length(x[1]["path"]), reverse=True)
        keeper_filename, keeper_meta = group[0]

        for filename, meta in group[1:]:
            try:
                meta["path"].unlink()
                auto_merged += 1
                log.info("Auto-merged duplicate URL: deleted %s (kept %s)", filename, keeper_filename)
                # Remove from file_metadata so it won't participate in pass 2
                del file_metadata[filename]
            except Exception as e:
                log.warning("Failed to delete duplicate %s: %s", filename, e)

    # Pass 2: Title similarity candidates
    # Compare all pairs with same type but different (or empty) URLs
    dismissed_set = {(d["a"], d["b"]) for d in state["dismissed"]}
    candidate_set = {(c["a"], c["b"]) for c in state["candidates"]}

    files_list = list(file_metadata.items())
    for i, (fname_a, meta_a) in enumerate(files_list):
        for fname_b, meta_b in files_list[i+1:]:
            # Same type?
            if meta_a["type"] != meta_b["type"]:
                continue

            # Different URLs (or at least one empty)?
            norm_url_a = _normalize_url(meta_a["source_url"]) if meta_a["source_url"] else ""
            norm_url_b = _normalize_url(meta_b["source_url"]) if meta_b["source_url"] else ""
            if norm_url_a and norm_url_b and norm_url_a == norm_url_b:
                # Same URL — should have been caught in pass 1
                continue

            # Compute title similarity
            title_a = meta_a["source_title"]
            title_b = meta_b["source_title"]
            similarity = _jaccard(title_a, title_b)

            if similarity < 0.70:
                continue

            # Normalize pair order (always a < b alphabetically)
            pair = tuple(sorted([fname_a, fname_b]))

            # Skip if already dismissed or already a candidate
            if pair in dismissed_set or pair in candidate_set:
                continue

            # Add new candidate
            state["candidates"].append({
                "a": pair[0],
                "b": pair[1],
                "similarity": round(similarity, 3),
                "detected_at": datetime.now().isoformat(),
            })
            candidate_set.add(pair)
            new_candidates += 1
            log.info("New duplicate candidate: %s ~ %s (similarity=%.2f)", pair[0], pair[1], similarity)

    # Save state
    try:
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning("Failed to save dedup state: %s", e)

    # Notify if we have new candidates
    if notify_fn and new_candidates > 0:
        if asyncio.iscoroutinefunction(notify_fn):
            try:
                asyncio.create_task(notify_fn(
                    f"🔍 Found {new_candidates} potential duplicate memories — /dupes to review"
                ))
            except Exception as e:
                log.warning("Failed to send duplicate notification: %s", e)

    return {
        "auto_merged": auto_merged,
        "new_candidates": new_candidates,
    }
