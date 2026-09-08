# Changelog

## v0.3.0 — 2026-09-08

Theme: **a machine can now consume a review.** v0.1 proved a stranger could install it,
v0.2 removed the frictions of using it by hand. v0.3 is what a script needs — and two
fixes for silent-absence bugs found while building it, both of the exact class the
playbook's first rule is about.

### Added

- **`--summary-json PATH`** and a `summary.json` written beside every run's reports: one
  machine-readable document per run, carrying the outcome, exit code, per-seat status,
  findings counts, severities, report paths and (for `--split-by-file`) the per-file
  verdict ids. It is written at **every** exit path, including the ones that produce no
  report directory at all — no diff, refused secret, unresolvable ref, no routable seat,
  lease denied, cancelled. That is the point of it: those runs must never be mistaken for
  "reviewed everything, found nothing". `outcome` is a closed vocabulary and `schema` is
  versioned, so a consumer can pin both.
- **Severity extraction.** `max_severity` per seat and per run, and per-finding severities,
  parsed with the same markdown-tolerant pattern the panel already used for counting.
  Findings are numbered in report order, matching what `verdict <run-id> <family> <n>`
  expects, so a judgement lands on the finding a human actually read. `null` means no
  finding carried a recognisable level — unknown, never clean.
- **`AGENT_OPS_HOME`** moves config, reports and state together. Explicit `[agent_ops]`
  keys still win; only the defaults move.
- **`--coder` accepts a comma-separated list** and excludes every named family. A branch
  carrying commits from more than one model is the normal case, and excluding only the
  first is worse than excluding nothing: it looks mechanical and is not.
- **`--version`**, so a wrapper can log and pin the core it runs against.
- **Continuous integration**: pytest on Python 3.11, 3.12 and 3.13, actions pinned by
  commit SHA, plus a gate that refuses to ship machine-specific strings and a release
  guard that requires a version tag to match the package.
- **README**: an exit-code table, a worked CI example with a one-line severity gate, and
  the command to run the tests.

### Fixed

- **An unreadable new file no longer vanishes from the payload.** The new-file dedup keyed
  on `is_file()`, then read the text and silently continued past `OSError`. For a file that
  exists but cannot be read, that dropped the hunks *and* produced no full-text section:
  the file appeared nowhere, and the panel reported cleanly on a change it had never seen.
  Measured: the entire payload collapsed to a single header line. Existence is not
  readability, so the dedup now keys on the read result, and an unreadable file gets a loud
  marker instead of a silent gap.
- **A git ref that cannot be resolved is an error, not an empty review.** `run_git`
  returned `""` on any non-zero exit, so an unfetched base ref or a shallow clone with no
  merge base produced "produced no diff — nothing to review" and exit 1, which is
  indistinguishable from a scope that genuinely had no changes. Routine in CI. It now
  exits 2 carrying git's own message.
- **One source of truth for the version.** Three files carried it independently and all
  three disagreed; a test now pins the manifest, the package and the changelog together.

### Fixed — found by running the panel on this release

The OpenRouter path had never been exercised against the real API before this release.
It has now: all three starter seats scored `good` on both probe stages, and a panel over
this release's own diff produced these, each reproduced before it was fixed.

- **A `SEVERITY:` line inside a fenced code block started a new finding.** Seats writing a
  FIX routinely quote the report format itself. Measured: a report whose only real finding
  was `low`, but whose FIX block quoted a `critical` header, split into two findings and
  reported `max_severity: critical`. A gate keyed on that blocks a change on the strength
  of a code sample. Fences are now masked for the boundary search only, so each finding
  keeps its code blocks.
- **The severity value pattern did not inherit the markdown tolerance it claimed.**
  `**SEVERITY:** — High`, a backticked level and `SEVERITY: - High` were all counted as
  findings while reading as unlabelled, so a seat that labelled everything reported as
  fully unlabelled.
- **A clean review could carry a severity.** A seat wrote a "no defects found" note shaped
  like a finding and then declared zero findings; the note graded the run `low`. The
  seat's own count is the contract.
- **Non-UTF-8 bytes in git output crashed the run.** Diff content lines are emitted raw,
  so one stray byte in a nominally-text file raised `UnicodeDecodeError` from inside
  `subprocess` — neither an empty diff nor a git error. Now decoded with replacement, as
  file text already was.
- **`git` missing from `PATH` escaped the error model**, which minimal CI images make
  routine.
- **`--coder ",,"` silently excluded nothing** while looking like exclusion was in force.
- **The summary could disagree with the exit status**, and an encoding failure could
  consume the run's only chance at a record.

### Measured

- One three-seat OpenRouter review of a ~9k-char change: **US$0.0047** (2026-09-08). The
  README and example config previously estimated this; it is now measured.

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
