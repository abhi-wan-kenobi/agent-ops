# Changelog

## v0.2.2 — 2026-09-02

### Added

- **Config-driven provider headers**: `[providers.<name>.headers]` in panel.toml sends
  extra request headers (attribution, routing, tagging spend per team at the provider).
  Vendor-neutral — the core never interprets them. `Authorization` and `Content-Type` are
  refused at load time: credentials go through `api_key_env` so a key never lives in the
  config file, and even a hand-built config cannot displace auth — it is applied after
  user headers. OpenRouter's default `X-Title` attribution is overridable.

### Documented

- `list_models`' blanket exception collapse (401 / SSL / network → the same `None`) is a
  recorded decision, now stated in its docstring: every caller treats `None` as "could
  not ask, do not block", and a bad key surfaces loudly on the POST that actually runs
  the seat.

## v0.2.1 — 2026-09-01

All three items came from the dogfood queue — defects found by running agent-ops on real
work, with measurements attached.

### Added

- **Stress-stage probing.** A seat can score `good` on the small probe and still return
  nothing on a real payload: reasoning burn scales with input, and the probe diff is five
  lines (measured twice on the same seat — clean probes, then empty content with the whole
  budget in `reasoning` at 9k and 11k chars). Every seat that passes the small probe is now
  probed again with the same two defects inside a ~12k-char realistic payload; a seat that
  goes silent at stress size is demoted out of the panel, loudly, with the stress record
  kept in the roster. Only silence demotes — a seat that answers but scores thin at stress
  size stays eligible. Stress failures are re-probed once before demoting.
- **`reasoning_chars` per seat in stats run lines**, so reasoning volume per input size is
  measurable from `stats.jsonl` over time instead of only observable when a seat dies.
- **Stress-aware ranking**: among seats that pass both probes, one that stayed `good` at
  stress size ranks ahead of one that went thin there — otherwise the stress measurement
  was decorative (audit finding). Absent stress data is neutral, like unknown context.

### Fixed

- **Inbound error text is now secret-redacted.** Outbound payloads were always gated;
  upstream error bodies were written to seat reports and stderr verbatim. An endpoint that
  echoes request context into its error message would have put a live credential on disk.
  Same pattern set, inbound direction, matches replaced with `[REDACTED]`.

### Decided

- **A report landing entirely in the reasoning channel is a dead seat, never rescued** —
  now a recorded decision (playbook rule 3) rather than an accident of data flow. Content
  is the contract; grading unaddressed deliberation would reward the indiscipline the
  classification screens for.

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
  measured probe latency, scaled by payload size ÷ probe-prompt size (floor 120 s,
  ceiling the 900 s default), so a hung seat fails in minutes instead of burning the
  whole budget. The scale factor is from a measured dogfood failure: an unscaled cap
  killed at 120 s a healthy seat that completes the same 9 k-char review in 113 s —
  probe latency is measured on ~1 k chars and no constant multiplier spans a 400 k-char
  payload range. Explicit `--timeout` overrides. Caps are printed per seat (per file in
  `--split-by-file` mode, from that file's own payload size).

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
