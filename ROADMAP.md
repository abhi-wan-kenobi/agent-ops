# Roadmap

v0.1.0 (2026-09-01) shipped the core: multi-family panel over plain HTTP, coder-family
exclusion, dead-seat detection, probe-ranked rosters, lease, run records, safety hooks,
and the playbook. What's next, in order of intent — dates are aims, not promises.

## v0.2 — adoption friction (shipped 2026-09-01, see CHANGELOG.md)

- **`agent_ops init`** and a guided `/panel-setup` command: key → reviewing in two steps.
- **Close the loop as data**: record confirmed / false-positive verdicts per finding
  (`agent_ops verdict`), and surface per-seat and per-coder rates (`agent_ops stats`).
  A panel that reports its own false-positive rate trains you to read it correctly.
- **`--split-by-file`**: review a large change one file at a time under a single lease,
  one summary — splitting is the difference between a real review and a silent overrun.
- **Deletion-only diffs** reviewed instead of reported as empty.
- **Probe-informed per-seat timeouts** so a dead-slow seat fails in minutes, not the
  full budget.

## v0.3 — machine-readable output (shipped 2026-09-08, see CHANGELOG.md)

- **`--summary-json`**: one document per run, written at every exit path — including the
  ones with no report directory. A script can finally tell a dead panel from a clean one.
- **Severity per finding and per seat**, so a caller can rank and gate without re-parsing
  markdown.
- The payload edge case below, fixed and regression-tested.
- `AGENT_OPS_HOME`, multi-family `--coder`, `--version`, and the repo's own CI.

## v0.4 — exploratory, demand-driven

- CI is already wired by hand, and stays that way in core: `--summary-json` plus the
  worked example in the README are the seam, and they are free forever. What core will
  **not** grow is a packaged GitHub Action with a pull-request comment renderer — that is
  a wrapper, it belongs outside the tool, and if it ships it ships as a separate paid
  distribution rather than as a core feature. Nothing that works today moves behind that
  line; the earlier plan to maintain a first-party free Action does not stand.
- `--fail-on <severity>` in the core, if the one-line gate in the README proves too thin.
  Held back deliberately: core exit codes answer "did a review happen", and overloading
  them with "and it found something bad" would conflate a dead panel with a busy one.
- A hosted seat provider as *one more* `type` in the registry — for people without any
  API key. BYOK stays first-class and free, always.
- Windows file-locking (hooks already run everywhere and fail open).
- Pruning `~/.agent-ops/audits/`, which currently grows without bound. Noted, not urgent,
  and a self-hosted CI runner with a persistent home is where it will bite first.

## Principles that won't move

- Bring your own key; the core never couples to a vendor.
- stdlib-only Python ≥ 3.11 — nothing for a user to install.
- The panel is a source of leads, not verdicts (see docs/PLAYBOOK.md) — features that
  inflate confidence without verification don't ship.
