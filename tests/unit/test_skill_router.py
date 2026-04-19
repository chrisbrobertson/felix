"""Unit tests for skill_router — content-type detection and skill routing."""
import pytest

from skill_router import detect_content_type, get_skill_and_depth, SKILL_REGISTRY, DEPTH_REGISTRY


# ── detect_content_type — URL patterns ───────────────────────────────────────

def test_arxiv_url_is_research_paper():
    assert detect_content_type("https://arxiv.org/abs/2301.00001") == "research-paper"


def test_semanticscholar_is_research_paper():
    assert detect_content_type("https://semanticscholar.org/paper/some-paper") == "research-paper"


def test_github_repo_url_is_code_repo():
    assert detect_content_type("https://github.com/openai/gpt-2") == "code-repo"


def test_github_issues_url_is_not_code_repo():
    # Issues path → falls through to default (no other signals)
    result = detect_content_type("https://github.com/openai/gpt-2/issues")
    assert result != "code-repo"


def test_docs_python_org_is_documentation():
    assert detect_content_type("https://docs.python.org/3/library/os.html") == "documentation"


def test_readthedocs_domain_is_documentation():
    assert detect_content_type("https://requests.readthedocs.io/en/latest/") == "documentation"


def test_docs_subdomain_is_documentation():
    assert detect_content_type("https://docs.django-rest-framework.org/") == "documentation"


def test_youtube_watch_is_video_transcript():
    # www.youtube.com is not in video_domains; the router matches bare youtube.com
    assert detect_content_type("https://youtube.com/watch?v=dQw4w9WgXcQ") == "video-transcript"


def test_youtu_be_short_url_is_video_transcript():
    assert detect_content_type("https://youtu.be/dQw4w9WgXcQ") == "video-transcript"


def test_vimeo_is_video_transcript():
    assert detect_content_type("https://vimeo.com/123456789") == "video-transcript"


def test_unknown_url_returns_default():
    assert detect_content_type("https://example.com/some-blog-post") == "default"


def test_invalid_url_returns_default_without_raising():
    result = detect_content_type("not-a-url-at-all!!!###")
    assert result == "default"


def test_empty_url_returns_default_without_raising():
    result = detect_content_type("")
    assert result == "default"


# ── detect_content_type — Content-Type header ─────────────────────────────────

def test_pdf_header_is_research_paper():
    assert detect_content_type("https://example.com/paper", content_type_header="application/pdf") == "research-paper"


def test_video_header_is_video_transcript():
    assert detect_content_type("https://example.com/vid", content_type_header="video/mp4") == "video-transcript"


def test_python_source_header_is_code_repo():
    result = detect_content_type("https://example.com/file", content_type_header="text/x-python")
    assert result == "code-repo"


# ── detect_content_type — content signals ────────────────────────────────────

def test_research_paper_signals_detected():
    # Content must be > 100 chars for _detect_by_content to run
    content = (
        "abstract introduction methodology conclusion references doi: et al. "
        "figure 1 table 1 arxiv preprint — this paper presents novel findings"
    )
    result = detect_content_type("https://example.com/paper", content=content)
    assert result == "research-paper"


def test_video_transcript_signal_overrides_others():
    # Content must be > 100 chars; transcript signal wins regardless of other signals
    content = (
        "[music] 0:00 abstract introduction methodology conclusion "
        "and many other research-paper-like terms padded to exceed the threshold"
    )
    result = detect_content_type("https://example.com/video", content=content)
    assert result == "video-transcript"


def test_short_content_returns_none_for_content_detection():
    # Content < 100 chars → content detection skips → falls to default
    result = detect_content_type("https://example.com", content="short")
    assert result == "default"


def test_no_signals_returns_default():
    content = "This is just some random blog post with no special signals at all."
    result = detect_content_type("https://example.com/blog", content=content)
    assert result == "default"


# ── get_skill_and_depth ───────────────────────────────────────────────────────

def test_research_paper_maps_to_summarize_deep():
    skill, depth = get_skill_and_depth("research-paper")
    assert skill == "summarize-deep"
    assert depth == "deep"


def test_default_maps_to_summarize_webpage():
    skill, depth = get_skill_and_depth("default")
    assert skill == "summarize-webpage"
    assert depth == "standard"


def test_documentation_maps_to_summarize_docs():
    skill, depth = get_skill_and_depth("documentation")
    assert skill == "summarize-docs"
    assert depth == "standard"


def test_code_repo_maps_to_summarize_repo():
    skill, depth = get_skill_and_depth("code-repo")
    assert skill == "summarize-repo"
    assert depth == "standard"


def test_video_transcript_maps_to_summarize_transcript():
    skill, depth = get_skill_and_depth("video-transcript")
    assert skill == "summarize-transcript"
    assert depth == "standard"


def test_long_default_article_promoted_to_deep():
    skill, depth = get_skill_and_depth("default", word_count=2001)
    assert skill == "summarize-deep"
    assert depth == "deep"


def test_short_default_article_stays_standard():
    skill, depth = get_skill_and_depth("default", word_count=500)
    assert skill == "summarize-webpage"
    assert depth == "standard"


def test_long_research_paper_stays_deep():
    # Already deep — word count promotion doesn't apply
    skill, depth = get_skill_and_depth("research-paper", word_count=5000)
    assert skill == "summarize-deep"
    assert depth == "deep"


def test_unknown_content_type_falls_back_to_summarize_webpage():
    skill, depth = get_skill_and_depth("made-up-type")
    assert skill == "summarize-webpage"
    assert depth == "standard"


# ── Registry completeness ─────────────────────────────────────────────────────

def test_all_content_types_in_skill_registry():
    from skill_router import CONTENT_TYPES
    for ct in CONTENT_TYPES:
        assert ct in SKILL_REGISTRY, f"Missing from SKILL_REGISTRY: {ct}"


def test_all_content_types_in_depth_registry():
    from skill_router import CONTENT_TYPES
    for ct in CONTENT_TYPES:
        assert ct in DEPTH_REGISTRY, f"Missing from DEPTH_REGISTRY: {ct}"
