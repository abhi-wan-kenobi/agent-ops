---
name: agent-ops
description: Adversarial code review by a panel of independent model families the user configures (OpenRouter, local Ollama, any OpenAI-compatible endpoint). ALWAYS run after writing or substantially changing code, before declaring work done. Also use for "audit this", "review this code", "check this for bugs", or before any release, merge or handoff.
allowed-tools: Read, Grep, Bash(PYTHONPATH=*)
---

# agent-ops — the review panel

Runs 2+ independent models from **different families** over a change and reports defects.
The family that wrote the code is excluded mechanically. The panel is a source of leads,
not verdicts — `${CLAUDE_PLUGIN_ROOT}/docs/PLAYBOOK.md` is the full operating doctrine and
is worth reading once in whole.

## Invocation

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops <repo> \
  --coder <model that wrote the code> \
  [--scope uncommitted|last|commit:<ref>|<git-ref>] \
  [--only <path-substring>] [--seats N] [--focus "..."] [--config <panel.toml>]
```

**Always pass `--coder`** (e.g. `--coder opus` when you wrote the code). It is what makes
family rotation mechanical rather than advisory: the coder's family is excluded from the
panel, so nothing ever reviews its own family's work.

Setup lives in `~/.agent-ops/panel.toml` (start from the plugin's `panel.example.toml`)
plus one API key env var. If the config is missing the run says so and exits — nothing to
diagnose. Related commands:

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops probe          # score & rank the configured seats
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops runs list     # inspect / cancel runs
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops runs cancel <run-id>
```

## What it does

- **Scoped payload, no repo access.** The diff *and* the full current text of every
  touched file are inlined; seats get no tools. One shot, bounded, no exploration.
- **Secret gate over what actually leaves.** The scan runs on the exact outbound payload.
- **Lease.** One panel at a time by default — concurrency against a rate-limited endpoint
  queues silently until a seat blows its budget and reports an empty result.
- **Stats** at `~/.agent-ops/state/stats.jsonl`, one line per run, `findings: null` for
  seats that never ran.

## Reading the result — this is the part that matters

2+ seats agreeing = high confidence, **not proof** (seats fed the same scoped payload share
blind spots — a scoped review can never support a "this is dead code" claim). A single-seat
finding is a **lead, not a fact**. Roughly a third of findings are false positives:
**reproduce every finding against the real code before acting on it.** A clean review is a
valid result.

Seat statuses, each meaning something different:

- **⚠️ TRUNCATED** — ran out of output budget mid-write. A **partial** review, not a clean
  one; findings count is inferred and may be short. Re-run narrower (`--only`).
- **⛔ SEAT DID NOT RUN** — transport error or timeout. Carries **no findings count at
  all**: `0` is a claim about the code, and a seat that never read the code is not
  entitled to make it.
- **⛔ SEAT REPORTED NOTHING** — the `AUDIT COMPLETE - n findings` terminator arrived with
  an empty body. A bare count with no findings written down is a dead seat, never
  agreement.
- The run prints `⚠️ ONLY n of N seats reported` when quorum failed, and exits non-zero if
  nothing was reviewed. **A clean exit is not a clean review** — a run must state what it
  read (file count, chars, seat names), and the summary does.

## Discipline for large changes

Review ONE commit or ONE file at a time — `--only <substring>` narrows both the file list
and the diff hunks. Splitting is the difference between a real review and a silent no-op.
A brand-new file is inlined once (its '+' hunks are dropped), so `--only` plus new files
still behaves.

## After the panel reports

For every confirmed finding, close the loop:

1. Fix it.
2. Add a regression test that names the finding.
3. Re-verify — including that the test fails without the fix.
4. Report what was confirmed, what was a false positive **and why**, and what changed.
   A right finding can carry a wrong fix — verify the remedy separately from the
   diagnosis. Never paste raw seat reports as the summary.
