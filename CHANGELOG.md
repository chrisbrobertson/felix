# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] — 2026-04-16

First tagged release. Establishes semver infrastructure (VERSION file,
`/version` command, version in startup log, tag reminder in install.sh).

### Added
- **Goal/Project Agent** (14th async loop) — periodically checks active goals and
  projects for new related memories, proposes actions via Telegram (`/actions`,
  `/action`, `/run`, `/drop`, `/defer`)
- **Project Inference Scanner** (13th loop) — infers projects from email/meeting/slack
  memories and writes candidate files for confirmation (`/review`, `/confirm`, `/reject`)
- **Report Scheduler** — configurable periodic reports delivered via Telegram
- **Notification Manager** (12th loop) — daily briefing, pre-meeting context, commitment
  and goal deadline alerts (`/briefing`, `/mute`, `/unmute`)
- **Slack Scanner** (11th loop) — polls Slack channels, writes `slack-thread-*.md` memories
- **Contact Tracker** (10th loop) — aggregates participants across memories
  (`/contacts`, `/contact`)
- **Goals and Projects** — full CRUD via Telegram (`/addgoal`, `/addproject`, `/goals`,
  `/projects`, and related commands)
- **Commitment Tracker** (8th loop) — extracts commitments from meetings and email
  (`/commitments`, `/complete`, `/dismiss`)
- **Calendar Scanner** (9th loop) — reads Apple Calendar, writes `calendar-event-*.md`
  memories (`/events`, `/event`)
- **Email Scanner** (6th loop) — reads Apple Mail, classifies and summarises threads
  (`/comms`, `/comm`)
- **Code Scanner** (5th loop) — scans git repos, writes `code-{hostname}-*.md` memories
  (`/code`)
- **Zoom Scanner** (7th loop) — polls Zoom Cloud Recordings, writes meeting transcripts
  (`/meetings`, `/meeting`)
- Semver infrastructure: `VERSION` file, `/version` command, version in daemon startup log

### Fixed
- Verb dispatch: all list+detail commands now accept natural-language verbs in addition
  to index numbers (e.g. `/goal add "..."` routes correctly rather than returning
  "Invalid index")
- Calendar scanner: AppleScript `modified_time` was `datetime.now()`, causing all events
  to be re-summarised every scan cycle — now uses `start_time` as a stable proxy
- Calendar scanner: improved diagnostic logging (data source selected, event counts,
  Automation permission errors now visible at WARNING level)

## [1.2.0] — 2026-04 (approximate)

### Changed
- Code repo memories renamed from `type: project` + `category: code` → `type: code`
- Filenames migrated from `project-{hostname}-*.md` → `code-{hostname}-*.md`
- Telegram command renamed from `/projects` → `/code`

## [1.1.0] — 2026-04 (approximate)

### Added
- Commitment Tracker, Code Scanner, Calendar Scanner (hostname-scoped filenames)
- Email classification (human / transactional / marketing / automated)

## [1.0.0] — 2026-04-11

Initial working system: browser watcher, Telegram chat handler, index builder,
skill optimizer, and the core flat-file memory architecture.
