# Third-party components

Every file in this repo is either original work or a derivative of a permissively licensed
upstream. This file records which, and why — including the negative decisions, so nobody has
to re-derive them.

**Nothing ships until it has a row here.** An empty row is a blocker, not an oversight.

Provenance triage run 2026-09-01 against the source estate; every candidate for v0.1 has a
row below.

## Disposition rules

- **ship-with-attribution** — permissive upstream (MIT/Apache-2.0/BSD). Preserve the
  copyright notice; keep the upstream LICENSE text under `licenses/`.
- **rewrite** — upstream is unlicensed, copyleft, or source-available. Reimplement from a
  behaviour description without consulting upstream source while writing.
- **cut / deferred** — not shipped in this version.

## Entries

| Component | Origin | Upstream licence | Disposition | Notes |
|---|---|---|---|---|
| `core/agent_ops/` (panel, payload, classify, lease, run_state, probe, providers, config, report) | original (ported from the author's private `auditor` skill: `auditor.py`, `run_state.py`, `probe_seats.py`, `audit-lease.sh`) | n/a (same author) | ship | Original work, no third-party upstream. The shell lease was rewritten in Python; machine-specific transport (local gateway, `claude -p` subprocess, vendor accounts) was replaced by the generic provider layer. No upstream code remains beyond the author's own. |
| `tests/` | original (ported from the author's private `test_auditor.py`, `test_probe_seats.py` plus new coverage) | n/a (same author) | ship | Same provenance as the code they test. |
| `hooks/protection_hook.py` + `hooks/hook_config.py` | rewritten from a behaviour description of the author's private `check_protection.py` | n/a (same author, source not consulted during the rewrite) | ship | Generalized: hardcoded private paths replaced by `readonly_roots` config; tag names configurable. No upstream code remains. |
| `hooks/dangerous_git_hook.py` | adapted from `mattpocock/skills` `skills/misc/git-guardrails-claude-code/scripts/block-dangerous-git.sh` @ `5b15a47` | MIT © 2026 Matt Pocock | ship-with-attribution | Ported bash → Python, pattern list generalized and made configurable, heredoc stripping and `git -C` handling added. Attribution header in the file; upstream licence at `licenses/mattpocock-skills-MIT.txt`. |
| `docs/PLAYBOOK.md` | original prose | n/a | ship | Ships as-is. |
| `skills/agent-ops/SKILL.md` | original prose (distilled from the author's private skill doc + the playbook) | n/a | ship | Rewritten for the ported tool; machine-specific history removed. |
| `tdd`, `grilling`, `resolving-merge-conflicts` skills | vendored from `mattpocock/skills` | MIT © 2026 Matt Pocock | deferred | Evaluated, **not shipped in v0.1**. If shipped later: attribution rows + the same licence file already in `licenses/`. |
| `verification-before-completion` skill | vendored from `obra/superpowers` | MIT © 2025 Jesse Vincent | deferred | Evaluated, **not shipped in v0.1**. |
| `stop-slop` skill | vendored (Hardik Pandya) | MIT | deferred | Evaluated, **not shipped in v0.1**. |
| `sandbox` skill | original (author's private skill) | n/a | deferred | Evaluated, **not shipped in v0.1** — machine-specific container tooling; would need the same decoupling treatment as the panel. |
| mukul975 skills | third-party | Apache-2.0 | cut | Not differentiated enough to carry; not shipped. |
| personal agents (`~/.claude/agents/*`) | machine-personal | n/a | cut | Personal configuration, not product material. |
