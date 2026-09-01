# Roadmap

v0.1.0 (2026-09-01) shipped the core: multi-family panel over plain HTTP, coder-family
exclusion, dead-seat detection, probe-ranked rosters, lease, run records, safety hooks,
and the playbook. What's next, in order of intent — dates are aims, not promises.

## v0.2 — adoption friction (planned 2026-09)

- **`agent_ops init`** and a guided `/panel-setup` command: key → reviewing in two steps.
- **Close the loop as data**: record confirmed / false-positive verdicts per finding
  (`agent_ops verdict`), and surface per-seat and per-coder rates (`agent_ops stats`).
  A panel that reports its own false-positive rate trains you to read it correctly.
- **`--split-by-file`**: review a large change one file at a time under a single lease,
  one summary — splitting is the difference between a real review and a silent overrun.
- **Deletion-only diffs** reviewed instead of reported as empty.
- **Probe-informed per-seat timeouts** so a dead-slow seat fails in minutes, not the
  full budget.

## v0.3 — exploratory, demand-driven

- GitHub Action: run the panel on pull requests, post the seat reports as a comment.
- A hosted seat provider as *one more* `type` in the registry — for people without any
  API key. BYOK stays first-class and free, always.
- Windows file-locking (hooks already run everywhere and fail open).

## Principles that won't move

- Bring your own key; the core never couples to a vendor.
- stdlib-only Python ≥ 3.11 — nothing for a user to install.
- The panel is a source of leads, not verdicts (see docs/PLAYBOOK.md) — features that
  inflate confidence without verification don't ship.
