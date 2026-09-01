# Changelog

## v0.2.0 — 2026-09-01

Theme: adoption. v0.1 proved "installable by a stranger"; v0.2 removes the frictions hit
while actually using it — first-run setup, and the manual labour around splitting reviews
and closing the loop on findings.

### Added

- **`agent_ops init`** writes a starter `panel.toml` (OpenRouter default, `--ollama` for
  the keyless local path), never overwrites, and prints the exact next commands. The new
  `/panel-setup` command wraps it as a guided path: key → reviewing in two steps.
- **`agent_ops verdict <run-id> <family> <n> confirmed|fp [--note]`** records what each
  finding turned out to be, validated against the run's own stats line, appended to
  `stats.jsonl` (append-only; the last verdict for a finding wins).
- **`agent_ops stats`** reports per-seat and per-coder finding counts and false-positive
  rates — computed over judged findings only, with dead seats counted separately from
  "found nothing".
- **`--split-by-file`** runs one panel per changed file, sequentially under a single
  lease, with per-file report subdirs, per-file stats lines (`<run-id>/<subdir>`, so
  verdicts land on the report a human actually read), and one summary.
- **Probe-informed per-seat timeouts**: with a fresh roster, a seat is capped at ~6× its
  measured probe latency (floor 120 s, ceiling the 900 s default), so a dead-slow seat
  fails in minutes instead of burning the whole budget. Explicit `--timeout` overrides.
  Caps are printed per seat in the run output.

### Fixed

- **Deletion-only diffs are reviewed** instead of reported as "nothing to review": a
  deleted file's path is now taken from the `--- a/` header when `+++` is `/dev/null`,
  so its hunks ship (no file text is inlined — there is nothing on disk).

### Changed

- Run lines in `stats.jsonl` now carry `"kind": "run"`; v0.1 lines (no `kind`) still
  parse as runs.
- In `--split-by-file` mode the secret gate also scans each per-file payload (the exact
  text that leaves), catching secrets a truncated whole-scope scan could not have seen;
  the offending file is skipped loudly and the run exits non-zero.

## v0.1.0 — 2026-09-01

Initial release: multi-family adversarial review panel over plain HTTP (OpenRouter /
Ollama / any OpenAI-compatible endpoint), coder-family exclusion, dead-seat detection,
probe-ranked rosters, file-lock lease, durable run records, safety hooks (dangerous-git,
path/tag protection), the playbook, and the ported test suite.
