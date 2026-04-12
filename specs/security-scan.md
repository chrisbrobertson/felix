---
kind: security-scan
created: 2026-04-11
status: open
---

# Security Scan

## CRITICAL

**[C1] AppleScript injection — `email_scanner.py:315-317`**
Mailbox names from config are interpolated directly into AppleScript code. A mailbox name containing `"` ends the string literal and injects arbitrary AppleScript. Fix: escape double quotes in mailbox names before interpolation.

**[C2] Prompt injection — `chat_handler.py:85-115`**
Webpage content fetched by the browser watcher flows into LLM context verbatim. A page containing `Ignore previous instructions and...` can hijack bot behaviour. Fix: wrap external content in explicit delimiters with a system-level note that it is untrusted data, never instructions.

**[C3] SSRF — `browser_watcher.py:107-121`**
`_fetch_content()` requests any URL from browser history with no scheme or IP filtering. An attacker who can visit `http://localhost:8080/` or `http://169.254.169.254/` triggers internal network scans. Fix: reject non-HTTPS schemes and block private/link-local IP ranges before fetching.

**[C4] Zoom token in URL query param — `zoom_scanner.py:504-505`**
`access_token=` is appended to the download URL, so the bearer token appears in server logs and HTTP Referer headers. Fix: pass `Authorization: Bearer {token}` header instead.

**[C5] Predictable `/tmp` paths — `browser_watcher.py:43`, `email_scanner.py:144`**
SQLite copies land at fixed paths (`/tmp/History`, `/tmp/second-brain-envelope-index`). Another local process can pre-create a symlink there to redirect the copy. Fix: use `tempfile.NamedTemporaryFile()` for an unpredictable path with mode 600.

---

## HIGH

**[H1] Credential files world-readable**
`~/.litellm/config.yaml` and the launchd plist are created with default umask (644). Any process running as the same user can read Gemini, Anthropic, and Zoom credentials. Fix: `chmod 600` both files in `install.sh` immediately after writing.

**[H2] API keys in environment variables visible to all user processes**
`ps eww` exposes env vars; the plist `EnvironmentVariables` dict is readable by any process running as the user. Fix: load secrets from a Keychain item at startup rather than injecting via env.

**[H3] Telegram command auth is pattern-not-middleware**
Auth is checked manually in each handler body. A future handler that forgets the check silently opens the bot to all Telegram users. Fix: centralise the check in a single decorator applied at handler registration time.

**[H4] Unbounded `seen_urls` set**
The seen-URLs file grows forever and is loaded entirely into memory at startup. After a year of active browsing this is tens of thousands of entries. Fix: replace with a SQLite table with a timestamp index; evict entries older than 90 days.

---

## MEDIUM

**[M1] Unpinned dependencies — `requirements.txt`**
All packages use `>=` version constraints. A compromised release of `litellm`, `python-telegram-bot`, or `httpx` would be auto-installed on the next `pip install`. Fix: pin exact versions (`==`) and commit a `pip freeze` lockfile.

**[M2] User queries logged in plaintext — `chat_handler.py:509`**
`log.info("Processing query: %r", query[:80])` persists potentially sensitive content to `out.log`. Fix: log at DEBUG level, or replace the query with a hash/length for INFO-level logs.

**[M3] `/delete` and `/purge` have no confirmation or audit trail**
Destructive file operations are irreversible and unlogged beyond a single INFO line. Fix: require a confirmation reply and append deletions to an append-only `audit.log`.

**[M4] YAML config written without file lock — `chat_handler.py:134-136`**
`/skip` and `/unskip` read-modify-write `config.yaml`. Two simultaneous commands cause a lost update. Fix: use `fcntl.flock()` around the read-modify-write cycle.

**[M5] SHA-1 for commitment IDs — `commitment_tracker.py:38-40`**
SHA-1 is deprecated. Fix: swap to `hashlib.sha256`.

**[M6] Skill files are unauthenticated code — `skill_executor.py:29-37`**
Any write access to the iCloud `skills/` directory can plant a skill file that controls what the LLM does. Fix: verify loaded skill files match a checksum stored outside iCloud (e.g., in `~/secondbrain/`).

**[M7] Swallowed exceptions hide real failures**
Several `except Exception: pass` blocks (e.g., `daemon.py`, `email_scanner.py`) silently discard errors. Fix: at minimum `log.debug("...", exc_info=True)` so failures are traceable.

**[M8] iCloud sync conflict copies not handled**
macOS creates `file (Chris's MacBook Pro's conflicted copy).md` files when two devices write simultaneously. Glob patterns in commitment_tracker and chat_handler pick these up as real memories. Fix: filter filenames matching `(.*conflicted copy.*)` before processing.

**[M9] No LLM token budget or cost guard**
`chat_handler.py` can load up to 20 memory files plus index.md into a single context with no token ceiling. Fix: enforce a hard token limit before the LLM call and log estimated cost.

**[M10] Zoom VTT parser assumes regex match — `zoom_scanner.py:~221`**
`m.group(1)` is used without checking `if m`. Malformed VTT content causes `AttributeError` and kills the scan cycle. Fix: guard every regex match result before accessing groups.

---

## LOW

**[L1] Git hooks could execute on repo scan — `project_scanner.py:~124`**
`git log` etc. run inside arbitrary repos in `~/repos/`. A malicious repo with `core.hooksPath` pointing to executable scripts runs those scripts. Fix: pass `-c core.hooksPath=/dev/null` in every git subprocess call.

**[L2] Memory file paths not validated against `MEMORIES_DIR` — `memory_writer.py:27-31`**
A title surviving the regex substitution with `..` could theoretically write outside the memories directory. Fix: assert `path.resolve().parent == MEMORIES_DIR.resolve()` before writing.

**[L3] Error messages written to `errors.log` unsanitized — `skill_executor.py:58-61`**
`str(e)` can contain newlines that break log format and allow injection into log parsers. Fix: replace newlines/control characters with spaces in the error string.

**[L4] Dependency scanning absent**
No `pip-audit`, Dependabot, or equivalent. Fix: add `pip-audit` to `requirements-dev.txt` and run it in pre-commit or CI.

**[L5] AppleScript date format locale-sensitive — `email_scanner.py:314`**
`strftime("%m/%d/%Y %H:%M:%S")` is interpreted by AppleScript using the system locale. A non-US locale could parse month/day order differently, silently scanning the wrong date range. Fix: use an unambiguous ISO 8601 date format.
