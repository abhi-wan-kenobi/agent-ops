# agent-ops

**Adversarial review for AI-written code.**

A multi-model audit panel, safety-rail hooks, and the operating rules for when the
reviewers themselves are wrong.

---

## The problem

Agentic coding tools write a lot of code quickly. The usual answer is "have the model
review it" — which fails in ways that are easy to miss:

- A model reviewing its own family's work grades itself.
- A model can report a finding that is **real** while proposing a fix that is **wrong**.
- A reasoning-heavy model can spend its entire output budget thinking and emit an empty
  report that *looks* like a clean pass.
- A review scoped to a diff cannot see the callers, so it confidently declares live code dead.
- Two models agreeing feels like proof. It is not — a scoped payload fools both the same way.

Every one of those produced a real, recorded failure before this tool existed. The tool is
the residue. `docs/PLAYBOOK.md` is the written form; start there.

## What it does

- **Panel review** — runs N independent models from *different families* over a change,
  mechanically excluding the family that wrote the code (`--coder`).
- **Dead-seat detection** — a seat that returns a bare finding count with an empty body is
  classified as dead, never as agreement. Timeouts, transport errors and truncation are
  all distinguished, loudly, with no findings count fabricated for any of them.
- **Scoped payloads** — the diff plus the full text of touched files, inlined. Reviewers
  get no shell and no filesystem, and a secret gate scans the exact bytes that leave.
- **Seat probing** — `probe` scores every configured seat on a known-defect diff and
  ranks the usable ones, because provider catalogues drift and a dead seat reads as a
  clean review.
- **Safety rails** — two zero-config hooks: one blocks destructive git commands
  (`push --force`, `reset --hard`, …), one blocks edits to protected paths and
  tag-protected files. Both fail open and are configurable via `hooks.toml`.
- **The playbook** — how to read what the panel gives you without being had by it.

## Requirements

- Python ≥ 3.11 (stdlib only — no packages to install)
- An [OpenRouter](https://openrouter.ai) API key **or** a local
  [Ollama](https://ollama.com) — or any OpenAI-compatible endpoint

## Install

In Claude Code:

```
/plugin marketplace add abhi-wan-kenobi/agent-ops
/plugin install agent-ops@agent-ops
```

Then configure a panel (once) — `/panel-setup` walks you through it, or directly:

```bash
PYTHONPATH=<plugin>/core python3 -m agent_ops init       # or: init --ollama for the keyless path
export OPENROUTER_API_KEY=sk-or-...                      # init prints this line too
```

The starter panel is three cheap, diverse OpenRouter families. **Measured 2026-09-08**
against the live API: one three-seat review of a ~9k-char change cost **US$0.0047**.
Local Ollama seats are free. `init` never overwrites an existing panel.toml.

## Use

Ask Claude to review its work (the skill triggers on "audit this", "review this code"),
or run the panel directly from any checkout:

```bash
PYTHONPATH=<plugin>/core python3 -m agent_ops <repo> --coder <model-that-wrote-it>
                                                           # comma-separate several coders
PYTHONPATH=<plugin>/core python3 -m agent_ops <repo> --coder <model> --split-by-file
                                                           # one panel per changed file, one summary
PYTHONPATH=<plugin>/core python3 -m agent_ops probe        # score & rank your seats
PYTHONPATH=<plugin>/core python3 -m agent_ops runs list    # inspect / cancel runs
```

Reports land under `~/.agent-ops/audits/<run-id>/`, one markdown file per seat, plus the
exact payload that was sent. Read them with the playbook's rules in hand: verify every
finding, treat single-seat findings as leads, and never accept a truncated report as clean.

Then close the loop — record what each finding turned out to be, and the panel starts
reporting its own error rate:

```bash
PYTHONPATH=<plugin>/core python3 -m agent_ops verdict <run-id> <family> <n> confirmed|fp --note "why"
PYTHONPATH=<plugin>/core python3 -m agent_ops stats        # per-seat / per-coder false-positive rates
```

## In CI, or any script

Every run writes a machine-readable summary next to its reports, and `--summary-json`
puts a copy wherever you want it:

```bash
PYTHONPATH=<plugin>/core python3 -m agent_ops <repo> --coder <model> \
  --scope "$BASE_SHA...$HEAD_SHA" --summary-json summary.json
```

`--scope base...head` reviews the branch against its merge base, which is what a pull
request means. It needs full history, so a shallow clone must be deepened first.

The summary is written on **every** exit path, including the ones that produce no report
directory at all. That is the point: a run that could not resolve the ref, refused on a
secret, or found no routable seat must not be mistaken for one that reviewed everything
and found nothing.

```json
{ "schema": 1, "outcome": "done", "exit_code": 0, "reported_seats": 2,
  "max_severity": "high", "seats": [ { "family": "glm", "status": "ok",
  "findings": 3, "max_severity": "high", "report": "…/glm.md" } ] }
```

Gate on it in one line. Note the two conditions: a dead panel is not a pass.

```bash
python3 -c 'import json,sys; d=json.load(open("summary.json")); \
  sys.exit(0 if d["outcome"]=="done" and d["max_severity"] not in ("high","critical") else 1)'
```

`max_severity` is `null` when no finding carried a recognisable level. That means
*unknown*, never *safe*.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | A panel convened and at least one seat reported |
| 1 | Nothing was reviewed: no diff, or no seat reported, or a split run left files unreviewed |
| 2 | Config, repo path, provider, or an unresolvable git ref |
| 3 | Refused: the outbound payload looked like it carried a live credential |
| 7 | Another panel holds the lease |
| 8 | Cancelled |
| 130 | Killed by a signal |

Set `AGENT_OPS_HOME` to move config, reports and state together, which is usually what a
CI job wants:

```bash
export AGENT_OPS_HOME="$RUNNER_TEMP/agent-ops"
```

## Hook configuration (optional — sane defaults apply with none)

`~/.agent-ops/hooks.toml`, overlaid per-project by `<project>/.agent-ops/hooks.toml`:

```toml
[protection]
readonly_roots = ["~/notes/vault"]          # no agent edits under these, ever
# tags = { readonly = "claude-readonly", ignore = "claude-ignore" }

[dangerous_git]
# always_block defaults cover push --force, reset --hard, clean -f, branch -D, ...
shared_worktrees = ["~/work/shared"]        # extra blocks (rebase, amend, add -A) inside
```

## Development

```bash
python3 -m pytest tests/          # the whole suite; no dependencies beyond pytest itself
```

## Licence

MIT. See `LICENSE`. Third-party components and their licences are listed in
`THIRD-PARTY.md`.
