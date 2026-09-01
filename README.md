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

Then configure a panel (once):

```bash
mkdir -p ~/.agent-ops
cp <plugin>/panel.example.toml ~/.agent-ops/panel.toml   # then edit, or use as-is
export OPENROUTER_API_KEY=sk-or-...                      # or use the keyless Ollama block
```

The example panel is three cheap, diverse OpenRouter families; a typical review costs
well under US$0.05, usually under a cent. Local Ollama seats are free.

## Use

Ask Claude to review its work (the skill triggers on "audit this", "review this code"),
or run the panel directly from any checkout:

```bash
PYTHONPATH=<plugin>/core python3 -m agent_ops <repo> --coder <model-that-wrote-it>
PYTHONPATH=<plugin>/core python3 -m agent_ops probe        # score & rank your seats
PYTHONPATH=<plugin>/core python3 -m agent_ops runs list    # inspect / cancel runs
```

Reports land under `~/.agent-ops/audits/<run-id>/`, one markdown file per seat, plus the
exact payload that was sent. Read them with the playbook's rules in hand: verify every
finding, treat single-seat findings as leads, and never accept a truncated report as clean.

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

## Licence

MIT. See `LICENSE`. Third-party components and their licences are listed in
`THIRD-PARTY.md`.
