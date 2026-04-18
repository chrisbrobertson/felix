"""
skill_router.py — Content-type detection and skill routing for BrowserWatcher.

Detects the content type of a URL based on URL patterns, Content-Type header,
and content signals. Returns a key from CONTENT_TYPES that maps to a skill name
via SKILL_REGISTRY.

Detection priority: URL patterns → Content-Type header → content signals → default
"""

import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    "research-paper",
    "documentation",
    "code-repo",
    "video-transcript",
    "default",
}

SKILL_REGISTRY: dict[str, str] = {
    "research-paper": "summarize-deep",
    "documentation": "summarize-docs",
    "code-repo": "summarize-repo",
    "video-transcript": "summarize-transcript",
    "default": "summarize-webpage",
}

# Mapping of content types to depth level
DEPTH_REGISTRY: dict[str, str] = {
    "research-paper": "deep",
    "documentation": "standard",
    "code-repo": "standard",
    "video-transcript": "standard",
    "default": "standard",
}


def get_skill_and_depth(content_type: str, word_count: int = 0) -> tuple[str, str]:
    """
    Get skill name and depth level for a content type.

    Args:
        content_type: A key from CONTENT_TYPES
        word_count: Word count of the content (optional)

    Returns:
        Tuple of (skill_name, depth_level)
    """
    # Check for long article promotion to deep
    if content_type == "default" and word_count > 2000:
        return ("summarize-deep", "deep")

    skill_name = SKILL_REGISTRY.get(content_type, "summarize-webpage")
    depth = DEPTH_REGISTRY.get(content_type, "standard")
    return (skill_name, depth)


def detect_content_type(
    url: str,
    content: str = "",
    content_type_header: str = "",
) -> str:
    """
    Detect the content type of a URL.

    Args:
        url: The page URL (required)
        content: First 3000 chars of page text (optional)
        content_type_header: HTTP Content-Type header value (optional)

    Returns:
        A key from CONTENT_TYPES. Never raises, never returns None.
        Falls back to "default" on any error.
    """
    try:
        # URL pattern detection
        result = _detect_by_url(url)
        if result:
            logger.debug(f"Content type detected: {result} for {url[:60]} (via url)")
            return result

        # Content-Type header detection
        result = _detect_by_header(content_type_header)
        if result:
            logger.debug(f"Content type detected: {result} for {url[:60]} (via header)")
            return result

        # Content signal detection
        result = _detect_by_content(content)
        if result:
            logger.debug(f"Content type detected: {result} for {url[:60]} (via content)")
            return result

        # Fallback to default
        logger.debug(f"Content type detected: default for {url[:60]} (via default)")
        return "default"

    except Exception as e:
        logger.warning(f"Error in content type detection for {url[:60]}: {e}")
        return "default"


def _detect_by_url(url: str) -> Optional[str]:
    """Detect content type based on URL patterns."""
    try:
        parsed = urlparse(url.lower())
        domain = parsed.netloc
        path = parsed.path

        # Research paper detection
        research_domains = {
            "arxiv.org",
            "semanticscholar.org",
            "pubmed.ncbi.nlm.nih.gov",
            "dl.acm.org",
            "ieeexplore.ieee.org",
            "scholar.google.com",
            "researchgate.net",
            "biorxiv.org",
            "medrxiv.org",
        }
        research_paths = ["/abs/", "/pdf/", "/paper/", "/papers/"]

        if domain in research_domains:
            return "research-paper"
        if any(p in path for p in research_paths) and any(d in domain for d in research_domains):
            return "research-paper"

        # Code repo detection
        code_domains = {"github.com", "gitlab.com", "bitbucket.org", "sourcehub.io"}
        exclude_paths = ["/issues", "/pulls", "/wiki", "/discussions"]

        if domain in code_domains:
            # Check for username/reponame pattern (2 segments)
            path_segments = [s for s in path.split("/") if s]
            if len(path_segments) >= 2 and not any(excl in path for excl in exclude_paths):
                return "code-repo"

        # Documentation detection
        doc_domains = {
            "docs.python.org",
            "developer.mozilla.org",
            "readthedocs.io",
            "readthedocs.org",
            "pkg.go.dev",
            "docs.rs",
            "api.reference",
            "devdocs.io",
        }
        doc_paths = ["/docs/", "/api/", "/reference/", "/documentation/"]

        if domain in doc_domains:
            return "documentation"
        if domain.startswith("docs.") or domain.endswith(".readthedocs.io"):
            return "documentation"
        if any(p in path for p in doc_paths):
            return "documentation"

        # Video transcript detection
        video_domains = {"youtube.com", "youtu.be", "vimeo.com", "loom.com"}

        if domain in video_domains:
            if "youtube.com" in domain and "/watch" in path:
                return "video-transcript"
            if "youtu.be" in domain:
                return "video-transcript"
            if domain in {"vimeo.com", "loom.com"}:
                return "video-transcript"

        return None

    except Exception:
        return None


def _detect_by_header(content_type_header: str) -> Optional[str]:
    """Detect content type based on Content-Type header."""
    if not content_type_header:
        return None

    header_lower = content_type_header.lower()

    if "application/pdf" in header_lower:
        return "research-paper"

    if any(x in header_lower for x in ["text/x-python", "text/x-c", "text/x-java"]):
        return "code-repo"

    if "video/" in header_lower:
        return "video-transcript"

    return None


def _detect_by_content(content: str) -> Optional[str]:
    """Detect content type based on content signals."""
    if not content or len(content) < 100:
        return None

    # Use first 3000 chars for analysis
    text = content[:3000].lower()

    # Research paper signals (need ≥2)
    paper_signals = [
        "abstract", "introduction", "methodology", "conclusion",
        "references", "doi:", "et al.", "figure 1", "table 1",
        "arxiv", "preprint"
    ]
    paper_count = sum(1 for signal in paper_signals if signal in text)

    # Documentation signals (need ≥2)
    doc_signals = [
        "parameters", "returns", "example:", "usage:", "```",
        "class ", "def ", "function(", "api reference", "installation"
    ]
    doc_count = sum(1 for signal in doc_signals if signal in text)

    # Code repo signals (need ≥2)
    repo_signals = [
        "clone", "fork", "pull request", "commit", "branch",
        "readme", "license", "contributing", "installation",
        "requirements.txt", "package.json"
    ]
    repo_count = sum(1 for signal in repo_signals if signal in text)

    # Video transcript signals (need ≥1)
    transcript_signals = [
        "[music]", "[applause]", ">> ", "0:00", "[inaudible]"
    ]
    has_transcript = any(signal in text for signal in transcript_signals)

    # Return highest scoring type
    if has_transcript:
        return "video-transcript"

    scores = {
        "research-paper": paper_count,
        "documentation": doc_count,
        "code-repo": repo_count,
    }

    # Need at least 2 signals
    max_type = max(scores.items(), key=lambda x: x[1])
    if max_type[1] >= 2:
        return max_type[0]

    return None
