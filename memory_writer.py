import hashlib
import os
import re
import yaml
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

MEMORIES_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"

# Query parameters that carry no content identity — strip before hashing so
# the same logical page visited via different referrers or campaigns produces
# one stable memory file.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name", "utm_place",
    "fbclid", "gclid", "_ga", "_gl", "_gac", "_gid",
    "mc_cid", "mc_eid", "yclid", "igshid",
    "ref", "referrer",
})


def _canonicalize_url(url: str) -> str:
    """Return a stable canonical form of *url* for hashing and dedup.

    Strips fragments, known tracking/session query params, and lowercases
    scheme + host. Path is kept case-sensitive so paginated URLs like
    /page/2/ stay distinct from /page/3/.
    """
    try:
        p = urlparse(url)
        qs = parse_qs(p.query, keep_blank_values=True)
        qs_clean = {k: v for k, v in qs.items() if k not in _TRACKING_PARAMS}
        query = urlencode(qs_clean, doseq=True)
        return urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path,
            p.params,
            query,
            "",  # strip fragment
        ))
    except Exception:
        return url


def _extract_summary(body: str) -> str:
    """Extract the first 1-2 sentences from the ## Summary section of a memory body."""
    m = re.search(r'## Summary\n(.*?)(?=\n\n|\n##|\Z)', body, re.DOTALL)
    if not m:
        return ""
    text = m.group(1).strip().replace("\n", " ")
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return " ".join(sentences[:2])


class MemoryWriter:
    def __init__(self, cache=None):
        """Initialize MemoryWriter with optional cache for invalidation."""
        self._cache = cache

    async def write(self, entry: dict, body: str, depth: str = "standard") -> str:
        MEMORIES_DIR.mkdir(parents=True, exist_ok=True)

        date = datetime.now().strftime("%Y-%m-%d")

        title_part = re.sub(r'[^a-z0-9]+', '-',
                            entry.get("title", entry["url"])[:50].lower()).strip('-')
        url_hash = hashlib.sha1(_canonicalize_url(entry["url"]).encode()).hexdigest()[:6]
        slug = f"{title_part}-{url_hash}"
        filename = f"{date}-{slug}.md"

        # Collision note: two machines visiting the same URL on the same day produce
        # identical filenames (same title slug + same URL hash). write_text is an
        # atomic overwrite on APFS — the second write wins with the same content.
        # This is intentional: duplicate memories are harmless, not additive.
        # seen_urls on each machine prevents re-processing on that machine; iCloud
        # deduplication handles the cross-machine case via identical filenames.
        target = MEMORIES_DIR / filename

        tags_match = re.search(r'\*\*Tags.*?:\*\*\s*(.+)', body)
        tags = [t.strip() for t in tags_match.group(1).split(",")] if tags_match else []

        summary = _extract_summary(body)

        # Field order is intentional: source_title / source_url / summary first
        # so that the most useful fields land within the first ~200 chars of the
        # file. This keeps them inside the 500-char relevance-scoring header cache
        # AND makes purge / manual browsing fast.
        frontmatter = {
            "source_title": entry.get("title", ""),
            "source_url": entry["url"],
            "summary": summary,
            "id": slug,
            "created": datetime.now().isoformat(),
            "visit_count": entry.get("visit_count", 1),
            "tags": tags,
            "browser": entry.get("browser", "unknown"),
            "hostname": __import__("socket").gethostname(),
            "depth": depth,
        }
        # Add optional extra fields if present in entry
        for field in ["content_type", "source_files", "type"]:
            if field in entry:
                frontmatter[field] = entry[field]

        content = f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n\n{body}\n"

        # Atomic write: write to .tmp sibling, then rename.
        # os.rename() is atomic on APFS — a crash mid-write never leaves a partial
        # file that syncs to iCloud. The .tmp file stays local until rename commits.
        tmp_path = target.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.rename(tmp_path, target)

        # Invalidate cache to pick up the new file
        if self._cache is not None:
            await self._cache.invalidate(filename)

        return filename
