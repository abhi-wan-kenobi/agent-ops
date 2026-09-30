# Changelog

## v0.4.1 — 2026-09-30

Theme: **the plugin's commands work under omp.**

### Fixed

- **Every documented command failed under omp with `No module named agent_ops`.** The skill
  and `/panel-setup` locate the plugin through `${CLAUDE_PLUGIN_ROOT}`. Claude Code
  substitutes that inline in Markdown; omp does not, and the variable is empty in its shell,
  so `PYTHONPATH` became `/core`. Found by installing v0.4.0 into a clean omp profile from
  GitHub and running a real session: `omp plugin install` and skill discovery worked, then
  the first command failed. A weak model then went hunting, and pip-installed unrelated PyPI
  packages (`agentops`, `agent-ops-cli`) into a venv. Each command now carries a second
  `PYTHONPATH` entry naming omp's plugin cache directory. It embeds the plugin version, and a
  test fails if a bump leaves it stale. Python ignores a `PYTHONPATH` entry that does not
  exist, so Claude Code is unaffected.
- Two shapes were tried and rejected on the way, both on real Claude Code: a
  `${CLAUDE_PLUGIN_ROOT:-…}` default is not substituted by Claude Code at all (the variable is
  not in its Bash environment), and a `$(ls … | tail -1)` search makes its permission check
  ask for approval on every command despite the skill's `Bash(PYTHONPATH=*)` allowance.

### Notes

- omp installs this plugin natively (`/marketplace add abhi-wan-kenobi/agent-ops`, then
  `/marketplace install agent-ops@agent-ops`), but only the skill and `/panel-setup` apply
  there. The two safety hooks in `hooks/hooks.json` are Claude Code command hooks, which omp
  does not load, so they do not run under omp. Use omp's own `bash.patterns` deny rules for
  destructive git commands instead.

## v0.4.0 — 2026-09-30

Theme: **Ollama as a first-class provider, and providers you can add yourself.**

### Added

- **Custom providers.** `type = "module:ClassName"` loads a `BaseProvider` subclass from
  `~/.agent-ops/providers/` (or anywhere on `PYTHONPATH`), so an endpoint with another
  path, auth header or request/response shape is one small file instead of a fork.
  `BaseProvider` exposes the dialect as hooks — `chat_path`, `build_body`,
  `parse_response`, `lists_model` — and keeps the retries, timeouts, secret redaction and
  error triage. Every load failure is a config error before any seat runs. The drop-in
  directory is appended to the import path, never prepended, so it cannot shadow the
  standard library. Guide and a complete worked example: `docs/PROVIDERS.md`.
- **`init --ollama-cloud`.** A starter panel for Ollama's hosted API: one `OLLAMA_API_KEY`,
  no daemon. The `ollama` type already accepted a key; nothing wrote or documented that
  path, and hosted ids differ from the local daemon's (`gpt-oss:120b`, not
  `gpt-oss:120b-cloud`).

### Fixed

- **Bare Ollama model names were reported NOT ROUTABLE.** Ollama's `/v1/models` lists every
  tag spelled out (`llama3.1:latest`) and serves a bare `llama3.1` as `:latest`, but the
  preflight compared ids exactly. `init --ollama` writes bare names, so its own starter
  panel was rejected whole. Reproduced against a live daemon: `nomic-embed-text` listed
  only as `nomic-embed-text:latest`, served fine, refused by the panel. Only `:latest` is
  implied — a bare name does not match another pulled tag, and an explicit tag never falls
  back.
- **Cloud models on a local Ollama daemon were reported NOT ROUTABLE.** The daemon's
  `/v1/models` lists only models you have `ollama pull`ed, but it serves every real cloud
  model on demand. On daemon 0.31.2 `glm-5.3-flash:cloud` and `deepseek-v4.1-flash:cloud`
  were unlisted, answered chat calls, and were dropped at preflight — a panel of only those
  two ended "NO ROUTABLE SEAT". A listing miss is now confirmed with the provider's own
  targeted probe (`confirm_unlisted`; Ollama asks `/api/show`, 200 for a real id, 404 for
  one that does not exist) before the seat is dropped. Never a chat call, and a fake id is
  still dropped. Custom providers get the same hook.
- **A 200 whose JSON body was not an object crashed the panel.** The error-envelope check
  ran outside the shape guard, so `[]` raised out of `call()` and took every seat down with
  it. It is now a seat error like any other malformed reply, and the guard also covers
  `KeyError` and `ValueError`, which is what indexing a wrong-shaped body raises.

### Changed

- The `ollama` type is its own class (an `OpenAICompatProvider` with Ollama's model
  naming) instead of an alias of the generic one. Requests on the wire are unchanged.

## v0.3.1 — 2026-09-09

Theme: **everything a stranger's first run hits.** Every item here was found by reinstalling
the plugin from GitHub into a clean profile and following the README, which is the one test
no unit suite performs. Four of the five fixes are silent-absence bugs, the class the
playbook's first rule is about.

### Fixed

- **`init` now follows `AGENT_OPS_HOME`.** It read the frozen module-level default instead
  of resolving the variable, so on a clean machine it wrote `~/.agent-ops/panel.toml` while
  `audit` and `probe` read the scratch home and found no config at all. v0.3.0 claimed the
  variable moved config, reports and state together; it moved two of the three.
- **The secret gate recognises the two keys this project tells you to export.** The generic
  pattern is `sk-` followed by twenty or more alphanumerics, and both `sk-or-v1-…`
  (OpenRouter, the documented default) and `sk-ant-…` (Anthropic) put a hyphen three
  characters in, ending the run. Neither ever matched, in either direction: the outbound
  refusal that stops a live credential reaching a third-party model, and the inbound
  redaction of provider error text. Both shapes are now named explicitly rather than the
  generic pattern being loosened, because `sk-[A-Za-z0-9_-]{20,}` matches
  `risk-management-strategies` and would refuse to review ordinary English.
- **An HTTP 200 carrying an error envelope is an error, not a dead seat.** OpenRouter
  answers 200 with `{"error": {...}}` and no `choices` when the upstream it routed to
  fails. That became empty content, which classifies as a dead seat, so the provider's own
  explanation was thrown away and the operator went hunting the model instead of the route.
  Measured live: 2 of 6 identical calls to one starter-panel seat.
- **`--only` that matches nothing says so.** It takes one path substring; hand it a comma
  list or a typo and every changed file is filtered out, which reported "produced no diff —
  nothing to review" and exited 1. A CI job wired that way stays green until somebody
  looks. The two cases differ by one `git` call, so the message now differs too and names
  the files that did change.
- **The secret gate's own tests can be reviewed.** Five credential-shaped literals sat flat
  in `tests/test_classify.py`, so any review touching that file refused on its own
  fixtures. `classify.py` has been fragmented against this since v0.1; its tests had not
  been. Now pinned by a test.

### Changed

- **The starter panel's third seat is `z-ai/glm-4.7-flash`**, replacing
  `z-ai/glm-5.3-flash`. This is not a price or a quality decision: OpenRouter fans one
  model id across several upstream providers, and they are not equally reliable. Six
  identical probe calls on 2026-09-08 gave `glm-5.3-flash` four usable replies, one empty
  body from Morph and one 200 error envelope, while `minimax/minimax-m2.7`,
  `openai/gpt-oss-120b` and the new `glm-4.7-flash` each returned six from six. A bare
  model id does not describe a seat. Both copies of the starter panel now say so, and a
  test pins them to each other, because they had already drifted once.
- The marketplace manifest carries a description, so `claude plugin validate` passes with
  no warnings.

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
