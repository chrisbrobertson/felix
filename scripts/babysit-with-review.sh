#!/bin/bash
# babysit-with-review.sh — Felix (secondbrain) feature/bug drainer with codex review.
#
# Step 1: promote any local feature-request-*.md files in the iCloud
#         memories dir to GitHub issues (mirrors /feature_import).
# Step 2: outer loop — `claude -p` picks one open kind:bug/kind:feature
#         issue and ships it as a PR.
# Step 3: when Claude ends with `HANDOFF_REVIEW <PR_NUMBER>`, run up to
#         MAX_REVIEW_CYCLES of codex review + claude fix passes.
# Step 4: on a clean codex review (zero BLOCKING findings), merge the PR
#         with `gh pr merge --merge --delete-branch`, then deploy via
#         `NONINTERACTIVE=1 ./install.sh` before the next iteration begins.
#
# Run from inside the secondbrain repo root.
#
# Env vars:
#   MAX_ITER           default 20   hard cap on outer iterations
#   SLEEP_SEC          default 10   pause between outer iterations (seconds)
#   STUCK_N            default 3    consecutive identical results = stuck
#   MAX_REVIEW_CYCLES  default 3    max codex<->claude cycles per PR

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/babysit-with-review.sh [-h|--help]

Run from the secondbrain repo root. First promotes any locally-captured
feature/bug files (feature-request-*.md in the iCloud memories dir) to
GitHub issues. Then loops `claude -p` against open kind:feature /
kind:bug issues until Claude outputs STOP, the loop gets stuck, or
MAX_ITER is hit.

When Claude ends an iteration with `HANDOFF_REVIEW <PR_NUMBER>`, this
wrapper runs up to MAX_REVIEW_CYCLES of:
  1. codex exec — strict-markdown review with BLOCKING / RECOMMENDED / INFORMATION
  2. claude -p  — addresses findings; commits and pushes

When codex reports zero BLOCKING findings, the PR is automatically merged
(`gh pr merge --merge --delete-branch`) and the daemon is redeployed
(`NONINTERACTIVE=1 ./install.sh`) before the next outer iteration starts.

Env vars:
  MAX_ITER           default 20
  SLEEP_SEC          default 10  (seconds)
  STUCK_N            default 3
  MAX_REVIEW_CYCLES  default 3

Logs land in ~/sisyphus-logs/<project>-babysit-with-review-<timestamp>-<pid>.log.

Examples:
  scripts/babysit-with-review.sh
  MAX_REVIEW_CYCLES=5 scripts/babysit-with-review.sh
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

MAX_ITER="${MAX_ITER:-20}"
SLEEP_SEC="${SLEEP_SEC:-10}"
STUCK_N="${STUCK_N:-3}"
MAX_REVIEW_CYCLES="${MAX_REVIEW_CYCLES:-3}"

PROJECT=$(basename "$PWD")
LOG_DIR="$HOME/sisyphus-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${PROJECT}-babysit-with-review-$(date +%Y%m%d-%H%M%S)-$$.log"
STOP_FILE="$LOG_DIR/${PROJECT}-babysit-with-review.stop"

if [ -f "$STOP_FILE" ]; then
  cat >&2 <<EOF
ERROR: $STOP_FILE already exists.

Another babysit-with-review may already be running for '$PROJECT'.

To check:
  pgrep -af babysit-with-review

If no other instance is running (e.g. a previous run crashed), remove the
lock file and try again:
  rm $STOP_FILE
EOF
  exit 1
fi

# ---------- promote local files to GitHub ----------

PROMOTER="$(dirname "$0")/promote_local_features.py"
if [ ! -f "$PROMOTER" ]; then
  echo "ERROR: cannot find promoter at $PROMOTER" >&2
  exit 1
fi

echo "→ Promoting any local feature/bug files to GitHub issues..."
if ! python3 "$PROMOTER"; then
  echo "ERROR: promoter failed; aborting before claude loop." >&2
  exit 1
fi

TMP_RESULT=$(mktemp)
TMP_REVIEW=$(mktemp)
TMP_REVIEW_RESULT=$(mktemp)
touch "$LOG" "$STOP_FILE"
trap 'rm -f "$STOP_FILE" "$TMP_RESULT" "$TMP_REVIEW" "$TMP_REVIEW_RESULT"' EXIT

echo "Working $PROJECT (max=$MAX_ITER, stuck=$STUCK_N, review_cycles=$MAX_REVIEW_CYCLES) → $LOG"
echo "  graceful stop: rm $STOP_FILE"

# ---------- prompts ----------

IFS= read -r -d '' BASE_PROMPT <<'PROMPT_EOF' || true
You are working autonomously on Felix (the secondbrain repo) as one iteration of a long-running loop that drains the user-reported feature/bug backlog into shipped, merged, deployed PRs.

The wrapper handles merging and deployment automatically when codex review passes — DO NOT run `gh pr merge` yourself, and DO NOT run `./install.sh` yourself. Your job is to ship one issue end-to-end into a PR; the wrapper takes it from there.

The current backlog (open kind:bug / kind:feature issues, open PRs) is provided above — use it directly without re-running discovery commands.

Required reading (in order):
  1. ./CLAUDE.md            — testing, versioning, CHANGELOG, deploy rules
  2. ./docs/finding-work.md — label vocabulary, frontmatter schema, gh CLI
                              recipes for finding actionable work

Helper scripts (available for targeted mid-iteration queries):
  ~/repos/scripts/prs       — enhanced `gh pr list` with CI check rollup and review state
  ~/repos/scripts/issues    — enhanced `gh issue list` sorted by priority labels

Pick the next unit of work in this priority order — stop at the first level that yields an actionable item:

1. Open PRs you can advance. See open PRs in the project state above. Address review comments or CI failures on any PR you (or a previous iteration) opened against a kind:bug or kind:feature issue.
2. Open issues labeled `kind:bug` — highest priority first (priority:critical > high > medium > low). These are user-reported breakage; ship a fix.
3. Open issues labeled `kind:feature` — same priority order. Implement the smallest version of the request that satisfies the captured intent.

Scope discipline: pick something completable in this iteration — roughly 1–3 hours of work. Prefer landing one small thing fully (code + tests + docs + CHANGELOG entry + VERSION bump if user-visible) over starting several things.

Per-iteration workflow:
1. State which issue you picked and why it is the most valuable next step right now. Reference it as `#NNN`.
2. Implement it fully — code, tests, docs, and a CHANGELOG entry. Bump VERSION per the rules in CLAUDE.md if the change is user-visible.
3. Run `pytest` (CLAUDE.md requires this before every commit). If it fails, fix the underlying issue.
4. Commit with a message that explains why the change was made and references the issue (e.g. "Fix /events crash on empty calendar (#42)").
5. Push the branch and open a PR via `gh pr create`. The PR body should include `Closes #NNN` so the issue auto-closes on merge.

End-of-iteration sentinels (mutually exclusive — output exactly one as the LAST line of your final message, with no surrounding quotes, code fences, or punctuation):

- HANDOFF_REVIEW <PR_NUMBER>
  Use this if you opened a new PR or pushed new commits to an existing PR during this iteration. The wrapper will run an automated code review (codex) and, if it passes, will merge the PR and redeploy the daemon before the next iteration. PR_NUMBER must be a bare integer (no leading `#`). Example: `HANDOFF_REVIEW 42`.

- STOP
  Use this ONLY if BOTH are true:
  * There are no open issues labeled kind:bug or kind:feature you can act on, AND
  * There are no open PRs against such issues that you can advance.

- (no sentinel)
  If neither applies — e.g. you committed work that isn't yet a PR, or you advanced an existing PR without making it review-ready — end your message normally. The outer loop will start the next iteration.

If you hit a transient obstacle (failing test, ambiguous requirement, missing dependency) — DO NOT output STOP. Work around it: pick a different issue, scaffold the missing dependency first, add a clarifying comment to the issue and pick another, or commit what you have on a branch and note what is blocked. STOP terminates the entire loop, so reserve it for genuine empty-queue. Do not output STOP or HANDOFF_REVIEW in code, quotes, or as part of a sentence.
PROMPT_EOF

IFS= read -r -d '' CODEX_REVIEW_PROMPT_TEMPLATE <<'PROMPT_EOF' || true
You are performing a code review on PR #__PR_NUMBER__ for this repository. The PR branch is currently checked out.

Inspect the diff of the current branch against the project's default branch (use `gh repo view --json defaultBranchRef -q .defaultBranchRef.name` to find it, then `git diff <default>...HEAD`). Read changed files and surrounding context as needed to evaluate the change.

Output your review using EXACTLY this format. Use all three headings in this order, even if a section has no findings:

## BLOCKING
- <one-line description> — <file:line> — <why it must be fixed before merge>

## RECOMMENDED
- <one-line description> — <file:line> — <why it should be addressed>

## INFORMATION
- <one-line description> — <file:line> — <context, suggestion, or fyi>

Categorization rules:
- BLOCKING = correctness bugs, security issues, broken tests, build failures, contract violations, broken invariants — anything that should not merge.
- RECOMMENDED = quality improvements, missed edge cases, better patterns, doc gaps, error-handling gaps. Should be addressed but not strictly blocking.
- INFORMATION = stylistic notes, alternative approaches, performance observations, fyi context. Optional.

Format rules:
- One bullet per finding. Be concise — single line, three em-dash-separated parts.
- If a section has no findings, write `- (none)` as the only bullet under that heading.
- Do NOT output anything before, between, or after the three sections.
- Do NOT make code changes. This is review only.
PROMPT_EOF

IFS= read -r -d '' CLAUDE_REVIEW_PROMPT_TEMPLATE <<'PROMPT_EOF' || true
A code review on PR #__PR_NUMBER__ has produced the findings below.

You MUST action every BLOCKING finding before this PR can merge.
You SHOULD action every RECOMMENDED finding (if you skip one, note the reason in the commit message).
You may CONSIDER each INFORMATION finding — apply if clearly beneficial, otherwise ignore.

For each finding you action:
1. Make the change.
2. Run `pytest` (CLAUDE.md mandates this before every commit); fix any failures introduced.
3. Commit with a message that names the finding category and what changed
   (e.g. "fix(blocking): handle nil session in auth middleware").
4. Push to the PR branch when done with this batch.

End your final message with EXACTLY ONE of these sentinels on its own line:

- DONE_REVIEW
  You have addressed everything you intend to address in this pass. The wrapper will run another codex review.

- STUCK_REVIEW <one-line reason>
  You cannot proceed (e.g. missing dependency, contradictory finding, environment issue). The wrapper will exit the review cycle.

Do not output STOP or HANDOFF_REVIEW — those belong to the outer loop.

--- review begin ---
__REVIEW__
--- review end ---
PROMPT_EOF

# ---------- helpers ----------

collect_state() {
  echo "=== project state @ $(date -u +%FT%TZ) ==="
  echo ""
  echo "## open issues (kind:bug, kind:feature)"
  gh issue list --state open --label kind:bug --limit 50 2>/dev/null || echo "(unavailable)"
  gh issue list --state open --label kind:feature --limit 50 2>/dev/null || echo ""
  echo ""
  echo "## open PRs"
  gh pr list --state open --limit 50 2>/dev/null || echo "(unavailable)"
  echo ""
  echo "==="
}

# Stream a claude -p iteration to the log AND a human-readable summary on
# stderr. Captures the final .result into the file passed as $2.
# Returns claude's exit code (PIPESTATUS[0]).
run_claude() {
  local prompt="$1"
  local out_file="$2"
  claude -p "$prompt" \
    --model opusplan \
    --dangerously-skip-permissions \
    --output-format stream-json \
    --verbose 2>>"$LOG" \
    | tee -a "$LOG" \
    | python3 -c '
import json, sys
final = ""
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        sid = (ev.get("session_id") or "?")[:8]
        print(f"  [init] session {sid}", file=sys.stderr, flush=True)
    elif t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    print(f"  [text] {txt.splitlines()[0][:200]}", file=sys.stderr, flush=True)
            elif bt == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                summary = (
                    inp.get("command") or inp.get("file_path")
                    or inp.get("pattern") or inp.get("path") or ""
                )
                summary = str(summary).splitlines()[0][:120] if summary else ""
                print(f"  [tool] {name} {summary}".rstrip(), file=sys.stderr, flush=True)
    elif t == "result":
        final = ev.get("result") or ""
sys.stdout.write(final)
' > "$out_file"
  return ${PIPESTATUS[0]}
}

# Count BLOCKING findings in a strict-markdown codex review on stdin.
# Treats a single `- (none)` bullet as zero findings.
count_blocking() {
  awk '
    BEGIN { in_block = 0; n = 0 }
    /^## BLOCKING[[:space:]]*$/ { in_block = 1; next }
    /^## /                      { in_block = 0; next }
    in_block && /^-[[:space:]]/ {
      line = $0
      sub(/^-[[:space:]]+/, "", line)
      if (line == "(none)") next
      n++
    }
    END { print n }
  '
}

# Merge a cleared PR and redeploy the daemon.
# Returns 0 on full success, 1 on any failure (merge, pull, or deploy).
merge_and_deploy() {
  local pr_num="$1"

  echo "  [merge] gh pr merge --merge --delete-branch #$pr_num" | tee -a "$LOG" >&2
  if ! gh pr merge "$pr_num" --merge --delete-branch >>"$LOG" 2>&1; then
    echo "  [merge] gh pr merge #$pr_num failed; skipping deploy, resuming outer loop" \
      | tee -a "$LOG" >&2
    return 1
  fi

  echo "  [merge] checkout main + ff-pull" | tee -a "$LOG" >&2
  if ! git checkout main >>"$LOG" 2>&1; then
    echo "  [merge] git checkout main failed; bailing" | tee -a "$LOG" >&2
    return 1
  fi
  if ! git pull --ff-only origin main >>"$LOG" 2>&1; then
    echo "  [merge] git pull --ff-only failed; bailing" | tee -a "$LOG" >&2
    return 1
  fi

  echo "  [deploy] NONINTERACTIVE=1 ./install.sh" | tee -a "$LOG" >&2
  if ! NONINTERACTIVE=1 ./install.sh >>"$LOG" 2>&1; then
    echo "  [deploy] install.sh failed; check $LOG" | tee -a "$LOG" >&2
    return 1
  fi

  echo "  [deploy] PR #$pr_num merged and deployed successfully" | tee -a "$LOG" >&2
  return 0
}

# Run the Claude<->Codex review cycle for a PR number.
# Returns 0 ONLY if codex reports zero BLOCKING findings (PR is cleared).
# Returns 1 on every other exit (checkout fail, codex fail, claude fail,
# STUCK_REVIEW, HEAD-unchanged guard, MAX_REVIEW_CYCLES hit without clearing).
run_review_cycle() {
  local pr_num="$1"
  local cycle=0

  echo "=== review handoff: PR #$pr_num @ $(date -u +%FT%TZ) ===" | tee -a "$LOG" >&2

  # Make sure we're on the PR branch.
  if ! gh pr checkout "$pr_num" >>"$LOG" 2>&1; then
    echo "  [review] gh pr checkout $pr_num failed; skipping review cycle" | tee -a "$LOG" >&2
    return 1
  fi

  while [ "$cycle" -lt "$MAX_REVIEW_CYCLES" ]; do
    cycle=$((cycle + 1))
    echo "--- review cycle $cycle / $MAX_REVIEW_CYCLES (PR #$pr_num) @ $(date -u +%FT%TZ) ---" | tee -a "$LOG" >&2

    # ---- codex pass ----
    local codex_prompt
    codex_prompt="${CODEX_REVIEW_PROMPT_TEMPLATE//__PR_NUMBER__/$pr_num}"
    : > "$TMP_REVIEW"

    echo "  [codex] reviewing PR #$pr_num..." >&2
    if ! codex exec \
        --output-last-message "$TMP_REVIEW" \
        -s read-only \
        "$codex_prompt" 2>&1 \
        | tee -a "$LOG" >&2 ; then
      echo "  [codex] non-zero exit; bailing review cycle" | tee -a "$LOG" >&2
      return 1
    fi

    local review
    review=$(cat "$TMP_REVIEW")
    if [ -z "$review" ]; then
      echo "  [codex] empty review output; bailing review cycle" | tee -a "$LOG" >&2
      return 1
    fi

    {
      echo "--- codex review (cycle $cycle) ---"
      printf '%s\n' "$review"
      echo "--- end codex review ---"
    } >> "$LOG"

    local n_blocking
    n_blocking=$(printf '%s\n' "$review" | count_blocking)
    echo "  [codex] $n_blocking blocking finding(s)" | tee -a "$LOG" >&2

    if [ "$n_blocking" -eq 0 ]; then
      echo "  [review] zero blocking findings; PR #$pr_num cleared after $cycle cycle(s)" | tee -a "$LOG" >&2
      return 0
    fi

    # ---- claude pass ----
    local claude_prompt
    claude_prompt="${CLAUDE_REVIEW_PROMPT_TEMPLATE//__PR_NUMBER__/$pr_num}"
    claude_prompt="${claude_prompt//__REVIEW__/$review}"

    local pre_sha post_sha
    pre_sha=$(git rev-parse HEAD 2>/dev/null || echo "")

    echo "  [claude] addressing findings..." >&2
    if ! run_claude "$claude_prompt" "$TMP_REVIEW_RESULT"; then
      echo "  [claude] non-zero exit during review pass; bailing review cycle" | tee -a "$LOG" >&2
      return 1
    fi

    local result trimmed last_line
    result=$(cat "$TMP_REVIEW_RESULT")
    trimmed=$(printf '%s' "$result" | sed -e 's/[[:space:]]*$//')
    last_line=$(printf '%s' "$trimmed" | tail -n 1)

    case "$last_line" in
      "STUCK_REVIEW"*)
        echo "  [claude] $last_line — bailing review cycle" | tee -a "$LOG" >&2
        return 1
        ;;
      "DONE_REVIEW")
        echo "  [claude] DONE_REVIEW — looping for another codex pass" | tee -a "$LOG" >&2
        ;;
      *)
        echo "  [claude] no review-cycle sentinel on last line; treating as DONE_REVIEW" | tee -a "$LOG" >&2
        ;;
    esac

    post_sha=$(git rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$pre_sha" ] && [ "$pre_sha" = "$post_sha" ]; then
      echo "  [claude] HEAD unchanged (no commits made) — bailing review cycle to avoid infinite loop" | tee -a "$LOG" >&2
      return 1
    fi
  done

  echo "  [review] hit MAX_REVIEW_CYCLES=$MAX_REVIEW_CYCLES on PR #$pr_num without clearing; resuming outer loop" | tee -a "$LOG" >&2
  return 1
}

# ---------- log header ----------

{
  echo "=== babysit-with-review.sh (felix) @ $(date -u +%FT%TZ) ==="
  echo "project:           $PROJECT"
  echo "cwd:               $PWD"
  echo "max_iter:          $MAX_ITER"
  echo "sleep:             ${SLEEP_SEC}s"
  echo "stuck_n:           $STUCK_N"
  echo "max_review_cycles: $MAX_REVIEW_CYCLES"
  echo "--- base prompt ---"
  printf '%s\n' "$BASE_PROMPT"
  echo "--- end base prompt ---"
  echo "--- codex review prompt template ---"
  printf '%s\n' "$CODEX_REVIEW_PROMPT_TEMPLATE"
  echo "--- end codex review prompt template ---"
  echo "--- claude review prompt template ---"
  printf '%s\n' "$CLAUDE_REVIEW_PROMPT_TEMPLATE"
  echo "--- end claude review prompt template ---"
} >> "$LOG"

# ---------- outer loop ----------

declare -a HASHES=()

iter=0
while [ "$iter" -lt "$MAX_ITER" ]; do
  iter=$((iter + 1))
  HEADER="=== iter $iter @ $(date -u +%FT%TZ) ==="
  echo "$HEADER" | tee -a "$LOG" >&2
  echo "  [stop file: $STOP_FILE]" >&2
  if [ ! -f "$STOP_FILE" ]; then
    echo "Stop file removed; exiting before iter $iter." | tee -a "$LOG"
    break
  fi

  STATE=$(collect_state)
  PROMPT="${STATE}

${BASE_PROMPT}"

  {
    echo "--- state ---"
    printf '%s\n' "$STATE"
    echo "--- end state ---"
  } >> "$LOG"

  if ! run_claude "$PROMPT" "$TMP_RESULT"; then
    echo "claude exited non-zero on iter $iter; see $LOG" >&2
    break
  fi
  echo "---" >> "$LOG"

  RESULT=$(cat "$TMP_RESULT")
  TRIMMED=$(printf '%s' "$RESULT" | sed -e 's/[[:space:]]*$//')
  LAST_LINE=$(printf '%s' "$TRIMMED" | tail -n 1)

  # Sentinel detection. HANDOFF_REVIEW triggers a review cycle; if cleared,
  # the PR is merged and the daemon is redeployed before the next iteration.
  # STOP terminates the loop.
  case "$LAST_LINE" in
    "HANDOFF_REVIEW "*)
      pr_num="${LAST_LINE#HANDOFF_REVIEW }"
      pr_num="${pr_num%% *}"
      if [[ "$pr_num" =~ ^[0-9]+$ ]]; then
        if run_review_cycle "$pr_num"; then
          merge_and_deploy "$pr_num" || true
        fi
      else
        echo "  [outer] HANDOFF_REVIEW with non-numeric PR '$pr_num'; ignoring" | tee -a "$LOG" >&2
      fi
      ;;
    "STOP")
      echo "STOP signal received on iter $iter."
      break
      ;;
  esac

  # Stuck-loop guard.
  HASH=$(printf '%s' "$RESULT" | shasum -a 256 | awk '{print $1}')
  HASHES+=("$HASH")
  if [ "${#HASHES[@]}" -gt "$STUCK_N" ]; then
    HASHES=("${HASHES[@]: -$STUCK_N}")
  fi
  if [ "${#HASHES[@]}" -eq "$STUCK_N" ]; then
    STUCK=1
    for h in "${HASHES[@]}"; do
      [ "$h" = "${HASHES[0]}" ] || { STUCK=0; break; }
    done
    if [ "$STUCK" -eq 1 ]; then
      echo "Stuck: last $STUCK_N results identical. Bailing on iter $iter." | tee -a "$LOG"
      break
    fi
  fi

  sleep "$SLEEP_SEC"
done

if [ "$iter" -ge "$MAX_ITER" ]; then
  echo "Hit MAX_ITER=$MAX_ITER. Bailing." | tee -a "$LOG"
fi

echo "Done after $iter iterations. See $LOG"
