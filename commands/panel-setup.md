---
description: Guided first-run setup — key → reviewing in two steps
allowed-tools: Bash(PYTHONPATH=*), Read, AskUserQuestion
---

Set up the agent-ops review panel for this user. Follow these steps in order; every
command below already prints what to do next, so relay its output rather than paraphrasing
from memory.

1. If `~/.agent-ops/panel.toml` already exists, say so and skip to step 3 — `init` never
   overwrites, and neither should you.

2. Ask which path they want (one question):
   - **OpenRouter** (recommended): one API key, three cheap diverse model families.
   - **Local Ollama**: no key at all, runs on their machine, weaker seats.

   Then run the matching command:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops init            # OpenRouter
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops init --ollama   # local
   ```

3. Make sure the credential/daemon side is ready:
   - OpenRouter: they need `OPENROUTER_API_KEY` exported (keys at https://openrouter.ai/keys).
     Do not ask them to paste the key into the chat — they export it in their shell.
   - Ollama: the models in panel.toml must match `ollama list`; help them edit the file if not.

4. Probe the seats and read the result to them honestly:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops probe
   ```

   Exit 0 = healthy. Exit 1 = fewer than two usable families — the panel cannot fill and
   the fix is adding/replacing seats, not ignoring the warning.

5. Offer to run a first real review on the current repo:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core" python3 -m agent_ops <repo> --coder <model that wrote the code>
   ```

   Point them at the agent-ops skill and `docs/PLAYBOOK.md` for how to read reports:
   findings are leads to verify, not verdicts — and after verifying each one, close the
   loop with `python3 -m agent_ops verdict <run-id> <family> <n> confirmed|fp` so
   `agent_ops stats` can report each seat's real false-positive rate.
