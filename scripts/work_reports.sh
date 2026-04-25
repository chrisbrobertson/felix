#!/bin/bash
# work_reports.sh — drain the captured feature/bug backlog into PRs.
#
# Step 1: promote any local feature-request-*.md files in the iCloud
#         memories dir to GitHub issues (mirrors /feature_import).
# Step 2: loop `claude -p` against open kind:feature / kind:bug issues
#         until Claude says STOP, the loop gets stuck, or MAX_ITER is hit.
#
# Run from inside the secondbrain repo root.
#
# Env vars:
#   MAX_ITER   default 20   hard cap on iterations
#   SLEEP_SEC  default 10   pause between iterations
#   STUCK_N    default 3    consecutive identical results that count as "stuck"

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/work_reports.sh [-h|--help]

Run from the secondbrain repo root. First promotes any locally-captured
feature/bug files (feature-request-*.md in the iCloud memories dir) to
GitHub issues, then loops `claude -p` against open kind:feature /
kind:bug issues until Claude outputs STOP, the loop gets stuck, or
MAX_ITER is hit.

Env vars:
  MAX_ITER   default 20   hard cap on iterations
  SLEEP_SEC  default 10   pause between iterations (seconds)
  STUCK_N    default 3    consecutive identical results that count as "stuck"

Logs land in ~/sisyphus-logs/<project>-work-reports-<timestamp>-<pid>.log.
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

PROJECT=$(basename "$PWD")
LOG_DIR="$HOME/sisyphus-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${PROJECT}-work-reports-$(date +%Y%m%d-%H%M%S)-$$.log"
STOP_FILE="$LOG_DIR/${PROJECT}-work-reports.stop"

if [ -f "$STOP_FILE" ]; then
  cat >&2 <<EOF
ERROR: $STOP_FILE already exists.

Another work_reports.sh may already be running for '$PROJECT'.

To check:
  pgrep -af work_reports

If no other instance is running (e.g. a previous run crashed), remove the
lock file and try again:
  rm $STOP_FILE
EOF
  exit 1
fi

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
touch "$LOG" "$STOP_FILE"
trap 'rm -f "$STOP_FILE" "$TMP_RESULT"' EXIT

echo "Working reports for $PROJECT (max=$MAX_ITER, stuck=$STUCK_N) → $LOG"
echo "  graceful stop: rm $STOP_FILE"

BASE_PROMPT=$(cat <<'PROMPT_EOF'
You are working autonomously on this project as one iteration of a long-running loop that drains the user-reported feature/bug backlog into shipped PRs. Each invocation should ship ONE issue end-to-end.

The current backlog (open issues, open PRs) is provided above — use it directly without re-running discovery commands.

Start by reading ./CLAUDE.md. Follow every convention in it (testing, versioning, CHANGELOG, deploy rules).

Helper scripts (available for targeted mid-iteration queries):
  ~/repos/scripts/prs              — enhanced `gh pr list` with CI check rollup and review state
  ~/repos/scripts/issues           — enhanced `gh issue list` sorted by priority labels

Pick the next unit of work in this priority order — stop at the first level that yields an actionable item:

1. Open PRs you can advance. Address review comments or CI failures on any PR you (or a previous iteration) opened against a kind:bug or kind:feature issue.
2. Open issues labeled `kind:bug` — highest priority first (priority:critical > high > medium > low). These are user-reported breakage; ship a fix.
3. Open issues labeled `kind:feature` — same priority order. Implement the smallest version of the request that satisfies the captured intent.

Scope discipline: pick something completable in this iteration — roughly 1–3 hours of work. Prefer landing one small thing fully (code + tests + docs + CHANGELOG entry + version bump if warranted) over starting several things.

Per-iteration workflow:
1. State which issue you picked and why it is the most valuable next step right now. Reference it as `#NNN`.
2. Implement it fully — code, tests, docs, and a CHANGELOG entry. Bump VERSION per the rules in CLAUDE.md if the change is user-visible.
3. Run `pytest` (CLAUDE.md requires this before every commit). If it fails, fix the underlying issue.
4. Commit with a message that explains why the change was made and references the issue (e.g. "Fix /events crash on empty calendar (#42)").
5. Push the branch and open a PR via `gh pr create`. The PR body should include `Closes #NNN` so the issue auto-closes on merge.

End your final message with the literal token STOP on its own line ONLY if BOTH:
- There are no open issues labeled kind:bug or kind:feature you can act on, AND
- There are no open PRs against such issues that you can advance.

If you hit a transient obstacle (failing test, ambiguous requirement, missing dependency) — DO NOT output STOP. Work around it: pick a different issue, scaffold the missing dependency first, add a clarifying comment to the issue and pick another, or commit what you have on a branch and note what is blocked. Outputting STOP terminates the entire loop, so reserve it for genuine completion. Do not output STOP in code, quotes, or as part of a sentence.
PROMPT_EOF
)

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

{
  echo "=== work_reports.sh @ $(date -u +%FT%TZ) ==="
  echo "project:  $PROJECT"
  echo "cwd:      $PWD"
  echo "max_iter: $MAX_ITER"
  echo "sleep:    ${SLEEP_SEC}s"
  echo "stuck_n:  $STUCK_N"
  echo "--- base prompt ---"
  printf '%s\n' "$BASE_PROMPT"
  echo "--- end base prompt ---"
} >> "$LOG"

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

  echo "--- state ---" >> "$LOG"
  printf '%s\n' "$STATE" >> "$LOG"
  echo "--- end state ---" >> "$LOG"

  claude -p "$PROMPT" \
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
' > "$TMP_RESULT"
  RC=${PIPESTATUS[0]}
  echo "---" >> "$LOG"

  if [ "$RC" -ne 0 ]; then
    echo "claude exited $RC on iter $iter; see $LOG" >&2
    break
  fi

  RESULT=$(cat "$TMP_RESULT")

  TRIMMED=$(printf '%s' "$RESULT" | sed -e 's/[[:space:]]*$//')
  LAST_LINE=$(printf '%s' "$TRIMMED" | tail -n 1)
  if [ "$LAST_LINE" = "STOP" ]; then
    echo "STOP signal received on iter $iter."
    break
  fi

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
