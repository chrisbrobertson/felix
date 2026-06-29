---
specmas: 3.0
kind: feature
id: feat-skill-routing
version: 1.1.0
created: 2026-04-12
updated: 2026-04-12
status: implemented
shipped_version: "1.17.0"
complexity: moderate
maturity: 2
parent_system: second-brain
related_specs:
  - feat-skill-optimizer
  - feat-skill-utility-scoring
  - feat-skill-creation
---

# Feature Spec: Skill Routing

## Overview / Problem Statement

The current browser watcher uses a single `summarize-webpage.md` skill to process all captured URLs, regardless of content type. This one-size-fits-all approach produces mediocre results because different content types have fundamentally different quality criteria:

- **Research papers** need methodology, findings, and citations extracted
- **API documentation** needs API surface, usage patterns, and prerequisites
- **Code repositories** need language, architecture, and dependency analysis
- **Video transcripts** need speakers, decisions, and action items

Each content type deserves a specialized skill variant that can optimize its extraction strategy independently. The skill optimizer currently runs nightly against a single generic skill, but cannot specialize when all content types are mixed together.

**Solution:** Add content-type detection and skill routing so the browser watcher selects the best-fit skill variant for each URL. This allows:

1. Specialized extraction strategies per content type
2. Independent optimization of each skill variant by the skill optimizer
3. Higher-quality memory files tailored to the specific content structure
4. Graceful fallback when specialized skills don't exist yet

## Content Types

The system recognizes five content types, detected via cascading rules:

| Type Key | Skill File | URL Pattern | Content Signal |
|---|---|---|---|
| `research-paper` | `summarize-paper.md` | `arxiv.org`, `doi.org`, `semanticscholar.org`, `scholar.google.com`, `.pdf` extension | Presence of "abstract", "methodology", "conclusion", "references" sections |
| `documentation` | `summarize-docs.md` | `docs.*`, `readthedocs.io`, `developer.*`, `/docs/`, `/api/`, `/reference/` in path | Code blocks + structured headers, API endpoint patterns (`GET`, `POST`, `PUT`, `DELETE` followed by paths) |
| `code-repo` | `summarize-repo.md` | `github.com`, `gitlab.com`, `bitbucket.org` | README-style structure (# header followed by description), high code density (`def `, `class `, `function `, `import `, `const `, `let `) |
| `video-transcript` | `summarize-transcript.md` | `youtube.com`, `youtu.be`, `rev.com`, `vimeo.com`, `/transcript` in path | Speaker-attributed lines (`Name:` or `[Speaker]`) or timestamp patterns (`00:00:00`, `[0:00]`) |
| `default` | `summarize-webpage.md` | All other URLs | No specialized signals detected |

**Detection Priority:**

1. **URL pattern matching** (highest priority) — domain and path regex, no content needed
2. **Content-Type header** — covers PDFs and other MIME-typed content
3. **Content signals** (lowest priority) — substring search in first 3000 chars of content

Rationale: URL patterns are cheapest (no I/O), most reliable, and cover 80%+ of cases. Content signals are a fallback for ambiguous URLs.

**Edge Cases:**

- GitHub URLs containing "abstract" in README → still classified as `code-repo` (URL wins)
- PDF research papers on arbitrary domains → classified as `research-paper` via Content-Type header
- YouTube URLs with no transcript → still classified as `video-transcript` (assumes transcript fetching happens elsewhere)
- Malformed or empty content → falls back to `default`, never raises

## Architecture

### New Module: `skill_router.py`

Pure function module with no mutable state.

**Public API:**

```python
def detect_content_type(
    url: str,
    content: str | None = None,
    content_type_header: str | None = None
) -> str:
    """
    Detect content type for a URL using cascading rules.
    
    Returns a type key string from CONTENT_TYPES.
    Never raises — always returns 'default' as fallback.
    
    Detection order:
    1. URL pattern matching (domain + path)
    2. Content-Type header matching
    3. Content signal matching (if content provided)
    4. Fall back to 'default'
    """
```

**Internal Implementation:**

```python
CONTENT_TYPES = [
    "research-paper",
    "documentation",
    "code-repo",
    "video-transcript",
    "default",
]

SKILL_REGISTRY: dict[str, str] = {
    "research-paper": "summarize-paper",
    "documentation":  "summarize-docs",
    "code-repo":      "summarize-repo",
    "video-transcript": "summarize-transcript",
    "default":        "summarize-webpage",
}

# URL pattern detectors
def _is_research_paper_url(url: str) -> bool:
    """Match arxiv.org, doi.org, semanticscholar.org, scholar.google.com, *.pdf"""
    
def _is_documentation_url(url: str) -> bool:
    """Match docs.*, readthedocs.io, developer.*, /docs/, /api/, /reference/ in path"""
    
def _is_code_repo_url(url: str) -> bool:
    """Match github.com, gitlab.com, bitbucket.org"""
    
def _is_video_transcript_url(url: str) -> bool:
    """Match youtube.com, youtu.be, rev.com, vimeo.com, /transcript in path"""

# Content signal detectors
def _has_research_paper_signals(content: str) -> bool:
    """Check for abstract, methodology, conclusion, references sections"""
    
def _has_documentation_signals(content: str) -> bool:
    """Check for code blocks + API patterns"""
    
def _has_code_repo_signals(content: str) -> bool:
    """Check for README structure + high code density"""
    
def _has_video_transcript_signals(content: str) -> bool:
    """Check for speaker attribution or timestamp patterns"""
```

**Detection Logic:**

```python
def detect_content_type(url, content=None, content_type_header=None):
    try:
        # 1. URL pattern matching
        if _is_research_paper_url(url):
            return "research-paper"
        if _is_documentation_url(url):
            return "documentation"
        if _is_code_repo_url(url):
            return "code-repo"
        if _is_video_transcript_url(url):
            return "video-transcript"
        
        # 2. Content-Type header
        if content_type_header:
            if "application/pdf" in content_type_header.lower():
                return "research-paper"
        
        # 3. Content signals (only if URL didn't match)
        if content:
            sample = content[:3000].lower()
            if _has_research_paper_signals(sample):
                return "research-paper"
            if _has_documentation_signals(sample):
                return "documentation"
            if _has_code_repo_signals(sample):
                return "code-repo"
            if _has_video_transcript_signals(sample):
                return "video-transcript"
        
        # 4. Fallback
        return "default"
    
    except Exception:
        # Never propagate exceptions — routing failures should not crash the watcher
        return "default"
```

### Changes to `browser_watcher.py`

**Current State:**

```python
class BrowserWatcher:
    def __init__(self, ...):
        self.executor = SkillExecutor("summarize-webpage")
    
    async def _process_url(self, url, ...):
        ...
        summary = await self.executor.execute(...)
```

**New State:**

```python
from skill_router import detect_content_type, SKILL_REGISTRY

class BrowserWatcher:
    def __init__(self, ...):
        # Replace single executor with lazy pool
        self._executor_pool: dict[str, SkillExecutor] = {}
    
    def _get_executor(self, skill_name: str) -> SkillExecutor:
        """
        Get or create SkillExecutor for the given skill name.
        
        Caches executors in _executor_pool for reuse.
        Falls back to default executor if skill file doesn't exist.
        """
        if skill_name not in self._executor_pool:
            try:
                self._executor_pool[skill_name] = SkillExecutor(skill_name)
                logger.debug(f"Created executor for skill: {skill_name}")
            except FileNotFoundError:
                logger.warning(
                    f"Skill file {skill_name}.md not found, falling back to default"
                )
                # Create default executor if not already cached
                if "summarize-webpage" not in self._executor_pool:
                    self._executor_pool["summarize-webpage"] = SkillExecutor("summarize-webpage")
                return self._executor_pool["summarize-webpage"]
        
        return self._executor_pool[skill_name]
    
    async def _process_url(self, url, ...):
        ...
        content = await self._fetch_content(url)
        
        # NEW: Detect content type and route to appropriate skill
        content_type = detect_content_type(url, content[:1000] if content else None)
        skill_name = SKILL_REGISTRY[content_type]
        executor = self._get_executor(skill_name)
        
        logger.debug(f"Routing {url} to {skill_name} (type: {content_type})")
        
        summary = await executor.execute(...)
        
        # NEW: Pass content_type to memory_writer
        await memory_writer.write_memory(
            ...,
            metadata={..., "content_type": content_type}
        )
```

**Fallback Chain:**

1. Detect content type → get skill name from `SKILL_REGISTRY`
2. Try to create `SkillExecutor(skill_name)` → cache in pool
3. If `FileNotFoundError`, log WARNING and create/return default executor
4. If default executor also fails, propagate exception (unrecoverable)

**Executor Pool Semantics:**

- Pool is a `dict[str, SkillExecutor]` mapping skill name → executor instance
- Initialized empty at `__init__` (lazy creation)
- `_get_executor()` creates on first access, returns cached instance on subsequent calls
- Each executor maintains its own execution history in the skill file
- Pool persists for the lifetime of the `BrowserWatcher` instance
- No eviction or size limits (max 5 executors under current design)

### Changes to `memory_writer.py`

**Current `write_memory` signature:**

```python
async def write_memory(title, url, summary, tags, metadata=None):
    """
    Write a memory markdown file to iCloud.
    
    metadata: dict of additional frontmatter fields (optional)
    """
```

**New behavior:**

```python
async def write_memory(title, url, summary, tags, metadata=None):
    """
    Write a memory markdown file to iCloud.
    
    metadata: dict of additional frontmatter fields (optional)
              Commonly includes 'content_type' from skill routing.
    """
    frontmatter = {
        "title": title,
        "url": url,
        "created": datetime.utcnow().isoformat(),
        "tags": tags,
    }
    
    # Merge caller-provided metadata
    if metadata:
        frontmatter.update(metadata)
    
    # Write file with atomic rename...
```

**Frontmatter Example (Research Paper):**

```yaml
---
title: "Attention Is All You Need"
url: "https://arxiv.org/abs/1706.03762"
created: "2026-04-12T10:30:00"
tags: ["transformers", "nlp", "machine-learning"]
content_type: research-paper
---

## Summary
...
```

**No validation** of `content_type` value in `memory_writer.py` — it's an arbitrary string field. Future chat handler filters can use it for search.

### New Module: `skill_creator.py`

Handles automatic creation of new skill files when a content type is encountered
that has no registered skill variant.

```python
class SkillCreator:
    def __init__(self, brain_dir: str, config: dict): ...
    
    async def handle_gap(
        self,
        content_type: str,
        example_url: str,
        example_content: str
    ) -> str | None:
        """
        Called when SKILL_REGISTRY has no entry for content_type.
        Returns skill_name if a new skill was created or is pending approval,
        returns None if the gap was suppressed (cooldown period or creation disabled).
        """
    
    async def _generate_seed(
        self,
        content_type: str,
        example_url: str,
        example_content: str
    ) -> str:
        """Returns the full markdown text of a new skill file."""
    
    def _is_in_cooldown(self, content_type: str) -> bool:
        """
        Returns True if this content_type was rejected within the cooldown window
        (default 24h). Reads from skills-registry.json.
        """
    
    def _load_registry(self) -> dict: ...
    def _save_registry(self, registry: dict) -> None: ...
    
    def run_probation_check(self, optimizer: "SkillOptimizer") -> None:
        """
        Called by SkillOptimizer after each nightly run.
        Checks probation skills against graduation criteria; triggers pre-graduation
        rewrites for skills that fail to meet the threshold.
        """
```

`skills-registry.json` is the authoritative lifecycle ledger:
```json
{
  "require_approval_runtime_override": null,
  "skills": {
    "summarize-paper": {
      "status": "active",
      "category": "auto-created",
      "content_types": ["research-paper"],
      "created": "2026-04-12T10:00:00",
      "probation_count": 5,
      "probation_target": 5,
      "graduated": "2026-04-13T03:15:00"
    }
  },
  "rejected_types": {
    "research-paper": "2026-04-11T14:30:00"
  }
}
```

**`require_approval_runtime_override`**: `null` means use the config-file default; `true`/`false` overrides it at runtime (set by `/skill-approval` command; persists across restarts).

## New Skill Files

Four new skill files must be created in `skills/` alongside the existing `summarize-webpage.md`.

### Common Structure

All skill files share this structure:

```markdown
---
name: <skill-name>
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
exemplar_eligible: true
content_type: <type-key>
---

## Instructions

<Type-specific extraction instructions>

## Execution History

| Timestamp | Model | Success | Latency | Tokens | Error |
|-----------|-------|---------|---------|--------|-------|
```

### `skills/summarize-paper.md`

**Frontmatter:**

```yaml
name: summarize-paper
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
exemplar_eligible: true
content_type: research-paper
```

**Instructions:**

```markdown
## Instructions

You are summarizing a research paper. Extract the following information in exactly this structure:

**Title:** The paper's full title

**Authors:** Primary authors (max 5, add "et al." if more)

**Institution:** Primary affiliation

**Year:** Publication year (if present)

**Venue:** Journal or conference name (if present)

**Abstract:** 1-sentence distillation of the core contribution

**Methodology:** 2-3 sentences describing the experimental approach or theoretical framework

**Key Findings:**
- First major finding
- Second major finding
- Third major finding (if applicable)

**Limitations:** 1-2 sentences on acknowledged limitations or open questions

**Tags:** 5-7 topic tags, including methodology type (e.g., "empirical", "theoretical")

Target length: 400-600 words. Prioritize precision over completeness. If the paper is behind a paywall or only the abstract is available, extract what you can and note the limitation.
```

### `skills/summarize-docs.md`

**Frontmatter:**

```yaml
name: summarize-docs
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
exemplar_eligible: true
content_type: documentation
```

**Instructions:**

```markdown
## Instructions

You are summarizing API documentation or technical reference material. Extract the following information:

**Product/Library:** Name and version (if present)

**Purpose:** 1-sentence description of what this library/API does

**Prerequisites:** Required dependencies, runtime versions, or setup steps (if mentioned)

**Key APIs/Concepts:**
- `ConceptName`: Brief description of what it does
- `ClassName` or `function_name()`: Purpose and primary use case
- (Continue for top 3-5 most important APIs/concepts)

**Typical Usage Pattern:** 2-3 sentence description of the common workflow or integration pattern

**Notable Features:** Bullet list of standout capabilities or differentiators

**Tags:** 5-7 tags including language/platform (e.g., "python", "rest-api", "authentication")

Target length: 400-600 words. Focus on the API surface and usage patterns, not implementation details. If version information is present, include it in tags (e.g., "v2.0").
```

### `skills/summarize-repo.md`

**Frontmatter:**

```yaml
name: summarize-repo
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
exemplar_eligible: true
content_type: code-repo
```

**Instructions:**

```markdown
## Instructions

You are summarizing a code repository (typically from GitHub/GitLab). Extract the following information:

**Repository Name:** Full name including org/user

**Primary Language(s):** Top 1-3 languages by volume

**Purpose:** 1-sentence description of what this project does

**Architecture Highlights:** 2-3 sentences describing the structural approach (e.g., "monorepo with microservices", "CLI tool with plugin system", "React SPA with REST backend")

**Notable Dependencies:** Key libraries or frameworks this project relies on (max 5)

**Activity Level:** Based on README badges, last commit date, or release history: `active` (commits in last month), `maintained` (commits in last 6 months), or `archived` (older or explicitly archived)

**Installation/Usage:** 1-2 sentences on how to get started (if documented)

**Tags:** 5-7 tags including primary language(s) and domain (e.g., "python", "machine-learning", "web-framework")

Target length: 400-600 words. Focus on what the code does, not how it's implemented. If the README is missing or sparse, note that limitation.
```

### `skills/summarize-transcript.md`

**Frontmatter:**

```yaml
name: summarize-transcript
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
exemplar_eligible: true
content_type: video-transcript
```

**Instructions:**

```markdown
## Instructions

You are summarizing a video transcript or recorded conversation. Extract the following information:

**Speakers:** Names of participants (if attributed), otherwise "Multiple speakers" or "Single speaker"

**Topic:** 1-sentence description of the primary subject matter

**Key Points:**
- First major point or decision
- Second major point or decision
- Third major point or decision (if applicable)

**Action Items/Decisions:** Bullet list of concrete next steps or commitments (if any)

**Notable Quotes:** Up to 2 memorable or insightful quotes with speaker attribution (if possible)

**Sentiment/Tone:** 1 sentence describing overall tone (e.g., "collaborative problem-solving", "heated debate", "educational lecture")

**Tags:** 5-7 tags including speaker names (if known) and topic domains

Target length: 400-600 words. Prioritize decisions and action items over generic discussion. If speaker names are not in the transcript, omit them from tags.
```

### Deployment via `install.sh`

The installer already copies `skills/*.md` to iCloud. No change needed — new skill files will be deployed automatically.

**Verification step** (optional): Add a check that all skills in `SKILL_REGISTRY` have corresponding files in `skills/`:

```bash
# In install.sh, after copying skills
echo "Verifying skill registry completeness..."
for skill in summarize-webpage summarize-paper summarize-docs summarize-repo summarize-transcript; do
    if [ ! -f "$BRAIN_DIR/skills/$skill.md" ]; then
        echo "WARNING: Skill file $skill.md not found in iCloud skills directory"
    fi
done
```

## Configuration

**No new config keys** are needed for routing in v1.0. The skill registry is code-defined because:

1. Adding a new content type requires a new skill file anyway (not just config)
2. Detection logic is code (regex patterns, content signals) — can't be config-driven without a DSL
3. Simplicity — no YAML parsing, validation, or schema versioning

**Future Extension (out of scope for v1.0):**

```yaml
# In config.yaml
skill_router:
  overrides:
    - domain: "docs.example.com"
      content_type: documentation
    - url_pattern: "^https://internal.corp/wiki/"
      content_type: documentation
```

This would allow per-user or per-domain overrides without editing code. Deferred until there's evidence it's needed.

**v1.1.0 Addition — Skill Creation Config:**

```yaml
skill_creation:
  enabled: true                         # enable automatic skill creation on gap detection
  require_approval: false               # default: new skills enter probation without approval
  probation_executions: 5               # shadow-mode runs before graduation check
  graduation_utility_threshold: 0.6     # minimum utility score to graduate to active
  model_route: chat                     # LiteLLM route for seed generation (default: claude-sonnet)
  rejection_cooldown_hours: 24          # hours before a rejected content type can re-trigger creation
  max_graduation_attempts: 3            # skill marked 'failed' after this many failed graduations
```

## Functional Requirements

**FR-1: Content Type Detection**

`detect_content_type(url, content, content_type_header)` must:

- Accept all three parameters as optional (but at minimum `url` is required)
- Return a string from `CONTENT_TYPES` (never `None`, never raises)
- Implement cascading detection in priority order: URL → Content-Type → content signals → default
- Use case-insensitive matching for all string comparisons
- Limit content signal analysis to first 3000 characters (performance + relevance)
- Handle malformed URLs, empty content, and `None` inputs gracefully
- Log detection decisions at DEBUG level for observability

**FR-2: Executor Pool in BrowserWatcher**

`_get_executor(skill_name)` must:

- Check `_executor_pool` dict for existing instance
- Create new `SkillExecutor(skill_name)` if absent and cache it
- Catch `FileNotFoundError` from `SkillExecutor.__init__` and fall back to default
- Return the same object instance on repeated calls (pool reuse)
- Never raise for missing skill files (always fall back)
- Log executor creation at DEBUG level and fallback at WARNING level

**FR-3: Routing in `_process_url`**

After `_fetch_content()` returns, the browser watcher must:

- Call `detect_content_type(url, content[:1000])` to get type key
- Look up skill name in `SKILL_REGISTRY[content_type]`
- Call `_get_executor(skill_name)` to get cached or new executor
- Pass `content_type` to `memory_writer.write_memory()` in metadata dict
- Log routing decision at DEBUG level: `"Routing {url} to {skill_name} (type: {content_type})"`

**FR-4: Skill Variant Seed Files**

Four new skill files must be created in `skills/`:

- `summarize-paper.md` — research paper specialization
- `summarize-docs.md` — API/technical documentation specialization
- `summarize-repo.md` — code repository specialization
- `summarize-transcript.md` — video/audio transcript specialization

Each must:

- Follow the standard skill file structure (frontmatter + Instructions + Execution History table)
- Include `content_type: <type-key>` in frontmatter
- Have type-specific extraction instructions matching the structure documented above
- Start with `total_runs: 0` (no execution history)
- Use `gemini/gemini-2.0-flash` as preferred and fallback model (consistent with existing skills)
- Be deployable via `install.sh` (no code change needed — glob already covers `skills/*.md`)

**FR-5: Content Type Logged in Memory Frontmatter**

`memory_writer.py` must:

- Accept `metadata` dict parameter (already supported)
- Write `content_type` field to frontmatter if present in metadata
- Not validate or constrain the `content_type` value (arbitrary string)
- Preserve all other frontmatter fields (title, url, created, tags)

Example frontmatter output:

```yaml
---
title: "Transformer Architecture Explained"
url: "https://arxiv.org/abs/1706.03762"
created: "2026-04-12T10:30:00.123456"
tags: ["transformers", "nlp", "attention-mechanism"]
content_type: research-paper
---
```

**FR-6: Skill File Existence Guard**

`SkillExecutor.__init__` currently raises `FileNotFoundError` if the skill file doesn't exist. This is the correct contract.

Callers (specifically `BrowserWatcher._get_executor()`) must:

- Wrap `SkillExecutor(skill_name)` in `try/except FileNotFoundError`
- Log warning with skill name: `f"Skill file {skill_name}.md not found, falling back to default"`
- Fall back to `SkillExecutor("summarize-webpage")`
- Cache the fallback executor in the pool under the key `"summarize-webpage"`, not the missing skill name

This ensures:

- Missing specialized skills don't crash the daemon
- System degrades gracefully to generic summarization
- Only one warning per missing skill per daemon lifetime
- Skill files can be added incrementally (not an all-or-nothing deployment)

---

## Skill Creation (v1.1.0 Addition)

This section extends the routing architecture with automatic skill creation when a routing gap is detected.

**FR-7: Gap Detection**

When `_get_executor()` falls back to the default skill (FR-2/FR-6), `BrowserWatcher` must also call `skill_creator.handle_gap(content_type, url, content[:500])`. This call is fire-and-forget (`asyncio.create_task`) — it must not block the URL processing pipeline.

- Gap detection fires only when `content_type != "default"` (routing to default for an *unknown* type is a gap; routing to default because the URL is genuinely default content is not).
- If `skill_creator.handle_gap` returns a skill name, `_get_executor()` is called again with the new name in the NEXT processing cycle (not the current one — the current URL continues with default).
- If `skill_creator._is_in_cooldown(content_type)` returns True: `handle_gap` returns None immediately (skip, log DEBUG).
- If `skill_creation.enabled: false` in config: `handle_gap` is a no-op.

**FR-8: Seed Skill Generation**

`SkillCreator._generate_seed()` produces a complete skill markdown file via LLM:

**Input context passed to LLM:**
- The full text of `skills/summarize-webpage.md` as the structural template
- The content_type label (e.g. "research-paper")
- A 500-char snippet from `example_content`
- The example URL

**Prompt structure:**
```
You are creating a new LLM skill file for a personal knowledge system.

Base template (follow this structure exactly):
{summarize_webpage_content}

Task: Create a skill variant specialized for content of type "{content_type}".

Example URL: {example_url}
Example content snippet:
{example_content_snippet}

Requirements:
- Keep the same frontmatter structure (add content_type: {content_type} field)
- Replace the Instructions section with type-specific extraction guidance
- Keep the Variables section identical to the template
- Keep the Execution History table header identical (7 columns)
- Do not fill in any Execution History rows
- Instructions should be 150-250 words, actionable, specific to this content type
- Set version: 1, total_runs: 0

Output the complete skill markdown file. Nothing else.
```

**LLM route:** `skill_creation.model_route` config key (default: `chat` → `claude-sonnet-4-20250514`)

**Output handling:**
- Strip any leading/trailing code fences (` ```markdown ` etc.)
- Validate: must contain `## Instructions`, `## Variables`, `## Execution History`; if any are missing, retry once; if still missing, log ERROR and abort
- Derive skill name: `summarize-{content_type}` (hyphens preserved)

**FR-9: Probation Mode**

New skills enter a probation period before becoming fully active.

**Skill file frontmatter additions for probation skills:**
```yaml
status: probation
probation_runs: 0
probation_target: 5
```

**Shadow execution (during probation):**
- `SkillExecutor.execute()` runs normally — the LLM is called, output is produced
- `memory_writer.write_memory()` is NOT called — the output is discarded
- `probation_runs` in the skill file frontmatter is incremented on each execution
- Execution is still appended to the skill's Execution History table (for scoring purposes)
- `BrowserWatcher` must check skill frontmatter for `status: probation` before calling `memory_writer`
  - This check is done by `_get_executor()` returning a flag OR by `BrowserWatcher` reading skill frontmatter directly after execution

**Probation count tracking:**
- `probation_runs` is written to both the skill file frontmatter AND `skills-registry.json`
- `probation_target` defaults to `skill_creation.probation_executions` config value (default 5)
- Shadow mode ends when `probation_runs >= probation_target`

**FR-10: Graduation Criteria**

After `probation_runs >= probation_target`, the graduation check runs on the next `SkillOptimizer.run_probation_check()` call (invoked after each nightly optimizer pass):

**Graduation path (success):**
- The skill's utility score (from `feat-skill-utility-scoring.md`) must be >= `skill_creation.graduation_utility_threshold` (default 0.6)
- If passing: `status` in skill frontmatter and `skills-registry.json` updated to `active`
- `graduated` timestamp written to registry
- `SKILL_REGISTRY` in memory updated with the new skill name (so new URLs start routing to it immediately)
- Telegram notification: "✓ New skill graduated: summarize-{type} (utility score: X.XX)"

**Graduation path (failure):**
- If utility score < threshold: skill is flagged for a pre-graduation rewrite by the optimizer on the NEXT nightly run
- `probation_runs` is reset to 0; `probation_target` stays the same (the skill re-enters shadow mode for another round after rewrite)
- Maximum 3 graduation attempts before the skill is marked `status: failed` and permanently excluded from routing
- Telegram notification: "⚠ Skill summarize-{type} failed graduation (score: X.XX, attempt N/3)"

**FR-11: Human-in-the-Loop (HITL)**

HITL is available at two layers: config (startup default) and runtime (Telegram command).

**Config layer:**
- `skill_creation.require_approval: false` (default) — new skills enter probation automatically
- `skill_creation.require_approval: true` — new skills require operator approval before probation

**Runtime layer:**
- `/skill-approval on|off|status` Telegram command overrides the config value for the running process
- State persisted in `skills-registry.json` under key `require_approval_runtime_override` (null = use config)
- The override survives daemon restarts

**When approval is required:**
1. `_generate_seed()` output is written to `BRAIN_DIR/skill-drafts/{skill_name}.md` with status `pending-approval`
2. Registry entry created with `status: pending-approval`
3. Telegram notification sent: "New skill draft ready for review: {skill_name} (content type: {content_type}, example: {example_url}). Use /skill-drafts to review."
4. Skill does NOT execute until approved

**When approval is not required:**
- Seed is written directly to `BRAIN_DIR/skills/{skill_name}.md`
- Registry entry created with `status: probation`
- No Telegram interrupt

**FR-12: Skill Draft Telegram Commands**

All commands require the standard auth check (`_check_auth`). All are registered in `COMMAND_REGISTRY` at implementation time.

**`/skill-drafts`**
- Lists all skills with `status: pending-approval` in registry
- Format: `N. summarize-{type} — content_type: {type}, drafted: {date}`
- Empty: "No pending skill drafts."
- Stores result in `_last_skill_draft_set` (same list+detail pattern as `/contacts`)

**`/skill-draft <N>`**
- Shows the full markdown text of draft N from `_last_skill_draft_set`
- Displays in a code block (4096-char limit applies — chunk if needed)
- Error if N not in set: "Run /skill-drafts first."

**`/approve-skill <N>`**
- Moves `BRAIN_DIR/skill-drafts/{skill_name}.md` → `BRAIN_DIR/skills/{skill_name}.md`
- Updates registry entry to `status: probation`, sets `probation_runs: 0`
- Adds entry to `SKILL_REGISTRY` in memory (so routing starts immediately)
- Reply: "Skill {skill_name} approved and entering probation (0/{probation_target} shadow runs)."
- Error if N not in `_last_skill_draft_set`: "Run /skill-drafts first."

**`/reject-skill <N>`**
- Deletes `BRAIN_DIR/skill-drafts/{skill_name}.md`
- Updates registry entry to `status: rejected`
- Writes `rejected_types[content_type] = now_iso` in registry (24h cooldown)
- Reply: "Skill {skill_name} rejected. Content type '{content_type}' is on a 24h cooldown."

**`/skill-approval on|off|status`**
- `on`: sets `require_approval_runtime_override: true` in registry, reply: "Skill approval mode ON. New skill drafts will require /approve-skill before running."
- `off`: sets `require_approval_runtime_override: false`, reply: "Skill approval mode OFF. New skills will enter probation automatically."
- `status`: reads effective approval mode (runtime override if set, else config), reply: "Skill approval: on (runtime override)" or "Skill approval: off (from config)"
- No args: same as `status`

**FR-13: `skills-registry.json` Data Model**

Full JSON schema for `BRAIN_DIR/skills-registry.json` (or `DEPLOY_DIR/skills-registry.json` — co-locate with other state files):

```json
{
  "require_approval_runtime_override": null,
  "skills": {
    "<skill_name>": {
      "status": "pending-approval | probation | active | failed | rejected",
      "category": "auto-created | manual",
      "content_types": ["<content_type_string>"],
      "created": "<iso_datetime>",
      "probation_count": 0,
      "probation_target": 5,
      "graduation_attempts": 0,
      "graduated": null,
      "last_utility_score": null,
      "draft_path": null
    }
  },
  "rejected_types": {
    "<content_type>": "<iso_datetime_of_rejection>"
  }
}
```

Writes to this file use the atomic write pattern from `memory_writer.py` (temp file + rename). File is created with empty `skills` and `rejected_types` dicts on first run if absent.

**FR-14: Optimizer Integration**

`SkillOptimizer` must respect skill lifecycle status:

- Skills with `status: probation` in their frontmatter are EXCLUDED from nightly optimization (probation executions provide the signal; optimizer rewrites would corrupt the signal)
- Skills with `status: failed` are EXCLUDED from all optimization
- Skills with `status: active` behave identically to existing skills (optimizer runs normally)
- Skills with `status: pending-approval` have no skill file in `skills/` yet — the optimizer never sees them
- The urgent queue (FR-16/FR-17 from feat-skill-optimizer.md v1.1.0) also skips probation skills
- After each nightly optimizer run, `SkillOptimizer` calls `skill_creator.run_probation_check()` to handle graduation decisions

## Non-Functional Requirements

**NFR-1: Performance**

- URL pattern detection must complete in <1ms (regex on domain + path only)
- Content signal detection must complete in <10ms (substring search on 3000 chars)
- Executor pool lookup must be O(1) dict access
- No additional I/O beyond existing `_fetch_content()` call

**NFR-2: Observability**

- Log detection decision for every URL: `DEBUG: Routing {url} to {skill_name} (type: {content_type})`
- Log executor creation: `DEBUG: Created executor for skill: {skill_name}`
- Log fallback: `WARNING: Skill file {skill_name}.md not found, falling back to default`
- Log unhandled exceptions in `detect_content_type`: `ERROR: Content type detection failed for {url}: {error}`

**NFR-3: Backward Compatibility**

- Existing `summarize-webpage.md` skill file must continue to work
- Memory files without `content_type` field must be readable by chat handler
- Skill optimizer must handle skills with `content_type` field in frontmatter (no code change needed — it already ignores unknown fields)

**NFR-4: Testability**

- `detect_content_type()` must be a pure function (no side effects, no I/O)
- All URL and content pattern detection must be unit-testable with mock inputs
- Executor pool behavior must be testable with mock skill files
- Fallback chain must be testable by removing skill files from temp directory

## Files to Create/Modify

| File | Change Type | Description |
|---|---|---|
| `specs/feat-skill-routing.md` | Create | This specification document |
| `skill_router.py` | Create | New module with `detect_content_type()` and `SKILL_REGISTRY` |
| `browser_watcher.py` | Modify | Replace single executor with pool, add routing call in `_process_url` |
| `memory_writer.py` | Modify | Accept and write `content_type` field to memory frontmatter |
| `skills/summarize-paper.md` | Create | Research paper skill variant |
| `skills/summarize-docs.md` | Create | Documentation skill variant |
| `skills/summarize-repo.md` | Create | Code repository skill variant |
| `skills/summarize-transcript.md` | Create | Video transcript skill variant |
| `install.sh` | Modify (optional) | Add verification that all registry skills exist in `skills/` |
| `tests/unit/test_skill_router.py` | Create | Test suite for content type detection |
| `tests/unit/test_browser_watcher.py` | Modify | Add tests for routing logic and executor pool |

## Test Specifications

All tests use `pytest` with `unittest.mock.patch` for isolation. Skill files are created in `tmp_path` fixtures.

### Unit Tests: `tests/unit/test_skill_router.py`

**Test: URL Pattern Detection (Research Papers)**

```python
def test_detect_research_paper_by_url():
    """ArXiv, DOI, and .pdf URLs should be classified as research-paper"""
    assert detect_content_type("https://arxiv.org/abs/1706.03762") == "research-paper"
    assert detect_content_type("https://doi.org/10.1038/s41586-020-2649-2") == "research-paper"
    assert detect_content_type("https://semanticscholar.org/paper/abc123") == "research-paper"
    assert detect_content_type("https://example.com/paper.pdf") == "research-paper"
```

**Test: URL Pattern Detection (Documentation)**

```python
def test_detect_docs_by_url():
    """docs.* and /docs/ paths should be classified as documentation"""
    assert detect_content_type("https://docs.python.org/3/library/") == "documentation"
    assert detect_content_type("https://readthedocs.io/en/latest/") == "documentation"
    assert detect_content_type("https://developer.apple.com/documentation/") == "documentation"
    assert detect_content_type("https://example.com/api/reference/") == "documentation"
```

**Test: URL Pattern Detection (Code Repositories)**

```python
def test_detect_code_repo_by_url():
    """GitHub, GitLab, Bitbucket URLs should be classified as code-repo"""
    assert detect_content_type("https://github.com/user/repo") == "code-repo"
    assert detect_content_type("https://gitlab.com/project/repo") == "code-repo"
    assert detect_content_type("https://bitbucket.org/team/repo") == "code-repo"
```

**Test: URL Pattern Detection (Video Transcripts)**

```python
def test_detect_video_transcript_by_url():
    """YouTube and transcript URLs should be classified as video-transcript"""
    assert detect_content_type("https://youtube.com/watch?v=abc123") == "video-transcript"
    assert detect_content_type("https://youtu.be/abc123") == "video-transcript"
    assert detect_content_type("https://rev.com/transcript/12345") == "video-transcript"
    assert detect_content_type("https://example.com/lecture/transcript") == "video-transcript"
```

**Test: Content-Type Header Detection**

```python
def test_detect_pdf_by_content_type_header():
    """PDFs should be detected via Content-Type header"""
    url = "https://example.com/download?id=12345"
    content_type = "application/pdf"
    assert detect_content_type(url, content_type_header=content_type) == "research-paper"
```

**Test: Content Signal Detection (Research Papers)**

```python
def test_detect_by_content_signals():
    """Content with research paper signals should be classified correctly"""
    content = """
    Abstract
    This paper presents a novel approach to neural machine translation.
    
    Methodology
    We trained a transformer model on WMT datasets.
    
    Conclusion
    Our results demonstrate state-of-the-art performance.
    
    References
    1. Vaswani et al. (2017)
    """
    assert detect_content_type("https://example.com/article", content=content) == "research-paper"
```

**Test: URL Pattern Precedence Over Content**

```python
def test_url_pattern_beats_content_signal():
    """URL patterns should take precedence over content signals"""
    # GitHub URL with research paper content should still be classified as code-repo
    content = "Abstract\nMethodology\nConclusion\nReferences"
    assert detect_content_type("https://github.com/user/repo", content=content) == "code-repo"
```

**Test: Default Fallback**

```python
def test_unknown_url_returns_default():
    """URLs without matching patterns should return default"""
    assert detect_content_type("https://example.com/random-page") == "default"
    assert detect_content_type("https://news.ycombinator.com/item?id=12345") == "default"
```

**Test: Exception Handling**

```python
def test_detect_never_raises():
    """Malformed inputs should never raise, always return default"""
    assert detect_content_type("not-a-url") == "default"
    assert detect_content_type("") == "default"
    assert detect_content_type("https://", content=None) == "default"
    assert detect_content_type("https://example.com", content="\x00\x01\x02") == "default"
```

**Test: Case Insensitivity**

```python
def test_case_insensitive_detection():
    """Detection should be case-insensitive"""
    content = "ABSTRACT\nMETHODOLOGY\nCONCLUSION"
    assert detect_content_type("https://example.com", content=content) == "research-paper"
```

**Test: Content Truncation**

```python
def test_content_truncation():
    """Only first 3000 chars should be analyzed"""
    # Signal appears after char 3000 — should not be detected
    prefix = "x" * 3000
    suffix = "Abstract\nMethodology\nConclusion\nReferences"
    assert detect_content_type("https://example.com", content=prefix + suffix) == "default"
    
    # Signal appears within first 3000 chars — should be detected
    prefix_short = "x" * 100
    assert detect_content_type("https://example.com", content=prefix_short + suffix) == "research-paper"
```

### Unit Tests: `tests/unit/test_browser_watcher.py`

**Test: Routing to Specialized Skill**

```python
@patch("browser_watcher.detect_content_type")
@patch("browser_watcher.SkillExecutor")
async def test_browser_watcher_routes_arxiv(mock_executor_class, mock_detect, tmp_path):
    """ArXiv URL should be routed to summarize-paper skill"""
    mock_detect.return_value = "research-paper"
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Summary of paper"
    mock_executor_class.return_value = mock_executor
    
    watcher = BrowserWatcher(...)
    await watcher._process_url("https://arxiv.org/abs/1706.03762")
    
    mock_detect.assert_called_once()
    mock_executor_class.assert_called_with("summarize-paper")
    mock_executor.execute.assert_called_once()
```

**Test: Fallback When Skill Missing**

```python
@patch("browser_watcher.detect_content_type")
@patch("browser_watcher.SkillExecutor")
async def test_browser_watcher_falls_back_when_skill_missing(mock_executor_class, mock_detect, tmp_path):
    """Missing specialized skill should fall back to default"""
    mock_detect.return_value = "research-paper"
    
    # First call (summarize-paper) raises FileNotFoundError
    # Second call (summarize-webpage) succeeds
    default_executor = AsyncMock()
    default_executor.execute.return_value = "Summary"
    mock_executor_class.side_effect = [FileNotFoundError, default_executor]
    
    watcher = BrowserWatcher(...)
    await watcher._process_url("https://arxiv.org/abs/1706.03762")
    
    assert mock_executor_class.call_count == 2
    mock_executor_class.assert_any_call("summarize-paper")
    mock_executor_class.assert_any_call("summarize-webpage")
    default_executor.execute.assert_called_once()
```

**Test: Executor Pool Reuse**

```python
@patch("browser_watcher.detect_content_type")
@patch("browser_watcher.SkillExecutor")
async def test_executor_pool_reuses_instances(mock_executor_class, mock_detect, tmp_path):
    """Same skill should return same executor instance"""
    mock_detect.return_value = "research-paper"
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "Summary"
    mock_executor_class.return_value = mock_executor
    
    watcher = BrowserWatcher(...)
    
    # Process two URLs with same content type
    await watcher._process_url("https://arxiv.org/abs/1706.03762")
    await watcher._process_url("https://arxiv.org/abs/1234.56789")
    
    # SkillExecutor should only be instantiated once
    mock_executor_class.assert_called_once_with("summarize-paper")
    assert mock_executor.execute.call_count == 2
```

**Test: Content Type Passed to Memory Writer**

```python
@patch("browser_watcher.detect_content_type")
@patch("browser_watcher.memory_writer.write_memory")
async def test_content_type_in_memory_metadata(mock_write_memory, mock_detect, tmp_path):
    """Content type should be passed to memory writer in metadata"""
    mock_detect.return_value = "documentation"
    
    watcher = BrowserWatcher(...)
    await watcher._process_url("https://docs.python.org/3/library/asyncio.html")
    
    mock_write_memory.assert_called_once()
    call_kwargs = mock_write_memory.call_args.kwargs
    assert "metadata" in call_kwargs
    assert call_kwargs["metadata"]["content_type"] == "documentation"
```

**Test: Multiple Content Types in Same Session**

```python
@patch("browser_watcher.detect_content_type")
@patch("browser_watcher.SkillExecutor")
async def test_multiple_content_types_create_separate_executors(mock_executor_class, mock_detect, tmp_path):
    """Different content types should create separate executors"""
    paper_executor = AsyncMock()
    docs_executor = AsyncMock()
    repo_executor = AsyncMock()
    
    mock_executor_class.side_effect = [paper_executor, docs_executor, repo_executor]
    mock_detect.side_effect = ["research-paper", "documentation", "code-repo"]
    
    watcher = BrowserWatcher(...)
    
    await watcher._process_url("https://arxiv.org/abs/1")
    await watcher._process_url("https://docs.python.org/")
    await watcher._process_url("https://github.com/user/repo")
    
    assert mock_executor_class.call_count == 3
    mock_executor_class.assert_any_call("summarize-paper")
    mock_executor_class.assert_any_call("summarize-docs")
    mock_executor_class.assert_any_call("summarize-repo")
```

### Integration Tests: `tests/integration/test_skill_routing_e2e.py`

**Test: End-to-End Routing with Real Skill Files**

```python
async def test_e2e_routing_with_skill_files(tmp_path):
    """Full flow from URL to memory file with real skill files"""
    # Set up temp directories
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    
    # Create real skill files
    (skills_dir / "summarize-paper.md").write_text("""---
name: summarize-paper
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: gemini/gemini-2.0-flash
content_type: research-paper
---

## Instructions
Extract title, authors, abstract, methodology, findings.

## Execution History
| Timestamp | Model | Success | Latency | Tokens | Error |
|-----------|-------|---------|---------|--------|-------|
""")
    
    with patch("browser_watcher.SKILLS_DIR", skills_dir), \
         patch("browser_watcher.MEMORIES_DIR", memories_dir), \
         patch("browser_watcher.acompletion") as mock_llm:
        
        mock_llm.return_value.choices[0].message.content = "Research paper summary"
        
        watcher = BrowserWatcher(...)
        await watcher._process_url("https://arxiv.org/abs/1706.03762")
        
        # Verify memory file created with correct content_type
        memory_files = list(memories_dir.glob("*.md"))
        assert len(memory_files) == 1
        
        content = memory_files[0].read_text()
        assert "content_type: research-paper" in content
```

## Migration Path

**Phase 1: Deploy Infrastructure (Zero Behavior Change)**

1. Add `skill_router.py` with detection logic
2. Modify `browser_watcher.py` to use executor pool but always return "default"
3. Deploy and verify no regressions

**Phase 2: Enable Detection (Read-Only)**

1. Enable `detect_content_type()` in `_process_url` but don't change executor
2. Log detected types at INFO level for 24h to verify detection accuracy
3. Analyze logs for false positives/negatives

**Phase 3: Create Specialized Skills**

1. Deploy four new skill files to `skills/`
2. Test each skill file manually with sample content
3. Verify skill optimizer doesn't break on new frontmatter fields

**Phase 4: Enable Routing (Full Deployment)**

1. Enable executor routing in `_process_url`
2. Monitor skill execution logs for fallback warnings
3. Tune detection rules based on observed misclassifications

**Rollback Plan:**

If routing causes issues, revert by:

1. Set `detect_content_type()` to always return "default"
2. Redeploy via `install.sh`
3. No data loss — memory files retain `content_type` field for future use

## Success Metrics

**Correctness:**

- <1% misclassification rate on manually labeled test set (100 URLs)
- Zero crashes or exceptions in production logs over 7 days

**Quality:**

- Skill optimizer shows improvement in per-type success rates over 30 days
- Manual review of 20 random memories per type shows better extraction quality vs. baseline

**Performance:**

- No measurable increase in `_process_url` latency (detection overhead <10ms)
- Executor pool reuse confirmed via DEBUG logs (no duplicate instantiation)

**Adoption:**

- All four specialized skill files accumulate execution history (>0 runs in 7 days)
- Fallback warnings <5% of total executions (indicates good skill file coverage)

## Future Extensions

**v1.1: User-Configurable Overrides**

Add `config.yaml` section:

```yaml
skill_router:
  overrides:
    - domain: "internal.corp.com"
      content_type: documentation
    - url_pattern: "^https://wiki\\.example\\.com"
      content_type: default
```

**v1.2: Dynamic Skill Discovery**

Allow skill files to declare their own detection rules in frontmatter:

```yaml
detection:
  url_patterns:
    - "arxiv\\.org"
    - "\\.pdf$"
  content_signals:
    - "abstract"
    - "methodology"
```

Load `SKILL_REGISTRY` dynamically from all skill files at startup.

**v1.3: Multi-Skill Execution**

For ambiguous content (e.g., a GitHub repo containing research papers), run multiple skills and merge outputs.

**v1.4: LLM-Based Classification**

For URLs that fail all heuristic rules, use a small classifier LLM to detect content type. Cache decisions in a `url-type-cache.json` file.

## Open Questions

**Q1: Should we detect sub-types within categories?**

Example: `research-paper-ml` vs `research-paper-bio` for domain-specific extraction.

**Answer (deferred):** Start with broad categories. Sub-type routing can be added in v1.1 if the skill optimizer shows benefit from further specialization.

**Q2: What if a URL matches multiple patterns?**

Example: `https://github.com/paperswithcode/paper.pdf` — repo or paper?

**Answer:** URL pattern priority order is defined. GitHub domain beats `.pdf` extension. Content signals only apply if no URL pattern matches.

**Q3: Should we store the skill name in memory frontmatter?**

Example: `skill_used: summarize-paper`

**Answer (deferred):** Not in v1.0. The `content_type` field is sufficient for filtering. If we need to track which skill version produced a memory (for optimizer debugging), add in v1.1.

**Q4: How do we handle skill file renames or deletions?**

**Answer:** Skill files are user data (in iCloud). If a skill is deleted, the fallback guard in `_get_executor()` handles it gracefully. If a skill is renamed, update `SKILL_REGISTRY` in `skill_router.py` and redeploy.

## Appendix: Detection Rule Details

### Research Paper URL Patterns

```python
import re

RESEARCH_PAPER_DOMAINS = [
    r"arxiv\.org",
    r"doi\.org",
    r"semanticscholar\.org",
    r"scholar\.google\.com",
    r".*\.pdf$",  # Any URL ending in .pdf
]

def _is_research_paper_url(url: str) -> bool:
    for pattern in RESEARCH_PAPER_DOMAINS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False
```

### Documentation URL Patterns

```python
DOCUMENTATION_PATTERNS = [
    r"^https?://docs\.",           # docs.python.org, docs.microsoft.com
    r"readthedocs\.io",            # any.readthedocs.io
    r"developer\.",                # developer.apple.com, developer.mozilla.org
    r"/docs/",                     # example.com/docs/guide
    r"/api/",                      # example.com/api/reference
    r"/reference/",                # example.com/reference/spec
]

def _is_documentation_url(url: str) -> bool:
    for pattern in DOCUMENTATION_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False
```

### Code Repo URL Patterns

```python
CODE_REPO_DOMAINS = [
    r"github\.com",
    r"gitlab\.com",
    r"bitbucket\.org",
]

def _is_code_repo_url(url: str) -> bool:
    for pattern in CODE_REPO_DOMAINS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False
```

### Video Transcript URL Patterns

```python
VIDEO_TRANSCRIPT_PATTERNS = [
    r"youtube\.com",
    r"youtu\.be",
    r"rev\.com",
    r"vimeo\.com",
    r"/transcript",
]

def _is_video_transcript_url(url: str) -> bool:
    for pattern in VIDEO_TRANSCRIPT_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False
```

### Research Paper Content Signals

```python
RESEARCH_PAPER_SIGNALS = [
    "abstract",
    "methodology",
    "conclusion",
    "references",
]

def _has_research_paper_signals(content: str) -> bool:
    content_lower = content.lower()
    # Require at least 3 of 4 signals to avoid false positives
    matches = sum(1 for signal in RESEARCH_PAPER_SIGNALS if signal in content_lower)
    return matches >= 3
```

### Documentation Content Signals

```python
DOCUMENTATION_SIGNALS = [
    ("```", 3),              # At least 3 code blocks
    ("GET /", 1),            # API endpoint patterns
    ("POST /", 1),
    ("PUT /", 1),
    ("DELETE /", 1),
]

def _has_documentation_signals(content: str) -> bool:
    content_lower = content.lower()
    
    # Check for code blocks
    code_block_count = content.count("```")
    if code_block_count >= 3:
        # Check for API patterns
        api_patterns = ["get /", "post /", "put /", "delete /"]
        if any(pattern in content_lower for pattern in api_patterns):
            return True
    
    return False
```

### Code Repo Content Signals

```python
CODE_REPO_SIGNALS = [
    "# ",                   # Markdown header (README structure)
    "## ",
    "### ",
    "def ",                 # Python
    "class ",               # Python/JS/Java
    "function ",            # JS
    "import ",              # Python/JS
    "const ",               # JS
    "let ",                 # JS
]

def _has_code_repo_signals(content: str) -> bool:
    # Check for README-style structure (headers)
    has_headers = content.count("# ") >= 2 or content.count("## ") >= 2
    
    # Check for code density (multiple code keywords)
    code_keywords = ["def ", "class ", "function ", "import ", "const ", "let "]
    code_density = sum(1 for keyword in code_keywords if keyword in content)
    
    return has_headers and code_density >= 3
```

### Video Transcript Content Signals

```python
TRANSCRIPT_SIGNALS = [
    r"\d{2}:\d{2}:\d{2}",   # Timestamp: 00:00:00
    r"\[\d+:\d{2}\]",       # Timestamp: [0:00]
    r"^\w+:",               # Speaker attribution: "John:"
    r"\[Speaker\]",         # Speaker label: [Speaker]
]

def _has_video_transcript_signals(content: str) -> bool:
    # Check for timestamps or speaker attribution
    for pattern in TRANSCRIPT_SIGNALS:
        if re.search(pattern, content, re.MULTILINE):
            return True
    return False
```

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.1.0 | 2026-04-12 | Claude Sonnet 4.6 | Add skill creation FRs (FR-7 through FR-14): gap detection, seed generation, probation mode, graduation, HITL approval, skill draft commands, registry data model, optimizer integration |
| 1.0.0 | 2026-04-12 | Claude Sonnet 4.6 | Initial spec |
