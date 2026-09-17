# ocr delegate integration

`ocr` (Alibaba OpenCodeReview) has a **delegation mode** that needs no LLM provider and no API key. It reads the repository, groups files by content, and prints a deterministic file selection plus the per-file rule set. We can feed that selection straight into `agent_ops` so the panel reviews exactly the files the rule set has already scoped, instead of a hand-picked `--only` substring.

## Why this is worth doing

`agent_ops` is deliberately adversarial about its own failure modes. The playbook documents three that this integration directly addresses:

1. **Payloads over ~150k chars can blow the shell argument list** — or, more precisely, make a single huge payload hard to review well. `ocr delegate preview` gives the changed file list up front; `ocr delegate rule` maps each file to its rule group. Feeding one file at a time (or one rule-group at a time) through `--only` keeps every `agent_ops` payload small and reviewable.
2. **A report marked `TRUNCATED` is a partial audit, not a clean pass**. If `ocr delegate` shows N files under the same rule group, use `--split-by-file` so each file gets its own subdir, payload, and truncation warning. A truncated file is then isolated and obvious, not buried in a giant combined payload.
3. **Roughly one third of findings are false positives with line/position drift**. `ocr` rules are tied to concrete files and invariants you verified by reading the repo; they narrow what the panel should look at, so the panel wastes less budget on out-of-scope noise and the human verification step has a smaller, sharper surface.

In short: `ocr delegate` replaces the brittle step where a human picks `--only <guess>` and hopes it matches what the rule set actually cares about.

## Prerequisites

- `ocr` is installed and on `PATH`. This repo does not configure an LLM provider for `ocr`; delegation mode needs none.
- `agent_ops` is available via `PYTHONPATH="$HOME/projects/agent-ops/core"`.
- A `panel.toml` exists at `~/.agent-ops/panel.toml`.

## Two-step command sequence

Run from inside the target checkout.

### Step 1 — preview the selection

```bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/projects/hermes-web
ocr delegate preview
```

This emits the reviewable file list, mode, and ref metadata without touching any model.

### Step 2 — get the rule grouping, then run the panel

```bash
# Example: two files that share a rule group
ocr delegate rule app/audio.py app/main.py

# Feed the files one at a time into agent_ops. The --only flag takes ONE substring,
# not a list, so run it per file (or per tight group if the rule set treats them
# identically). Use the family that wrote the code for --coder exclusion.

PYTHONPATH="$HOME/projects/agent-ops/core" \
  python3 -m agent_ops ~/projects/hermes-web \
  --coder claude \
  --only app/audio.py

PYTHONPATH="$HOME/projects/agent-ops/core" \
  python3 -m agent_ops ~/projects/hermes-web \
  --coder claude \
  --only app/main.py
```

For changes that span several files and all belong to the same rule group, prefer `--split-by-file` under a single `--only` prefix that still narrows the scope:

```bash
PYTHONPATH="$HOME/projects/agent-ops/core" \
  python3 -m agent_ops ~/projects/hermes-web \
  --coder claude \
  --only app/ \
  --split-by-file
```

This produces one panel per changed file, sequentially, under one lease, with a separate payload and report subdir for each file.

## Hard rule: never swap `ocr delegate` for `ocr review`

`ocr review` and `ocr scan` invoke an LLM through a provider. The agent-ops panel on this machine is configured to use the local Ollama daemon's cloud models, which share accounts with the family Hermes lane and the agent-ops panel itself. Running `ocr review` or `ocr scan` would burn those shared Ollama cloud accounts.

`ocr delegate` is the only `ocr` subcommand in scope here. It performs zero LLM calls and therefore consumes no provider quota. Do not change the integration to call `ocr review`, `ocr scan`, or any other LLM-backed `ocr` command.

## Deterministic file selection

`ocr delegate preview` / `ocr delegate rule` use the same file-discovery path that `ocr review` would use, but stop before calling a model. The output is reproducible for a given checkout state, so the set of files you feed to `agent_ops` is the set the rule set was actually written for.

If `ocr delegate preview` returns zero reviewable files, there is nothing to delegate and `agent_ops` will correctly report `no-diff`.

## Mapping rule groups to `--only` scoping

Each rule in `.opencodereview/rule.json` targets a `path`. When multiple files share a rule group, `ocr delegate rule` lists them together. Choose the narrowest `--only` that still covers the files you care about:

- One file changed: `--only <file>`.
- Several files in the same directory with the same rule group: `--only <dir>/` or `--only <dir>/<prefix>`.
- A broad refactor touching many rule groups: run `--split-by-file` with `--only` narrowed to each group in turn, or omit `--only` entirely only if the combined payload is well under `max_payload`.

Remember that `--only` is a substring match, not a glob. Passing a comma-separated list silently matches nothing.

## What the panel receives

`agent_ops` builds a payload of the git diff plus the full text of each touched file. With `--only`, the file list is already constrained. The prompt head is the same; the per-seat focus is the same; only the scope shrinks. This is exactly the discipline the playbook recommends for large changes.
