---
description: Guided first-run setup — key → reviewing in two steps
allowed-tools: Bash(PYTHONPATH=*), Read, AskUserQuestion
---

Set up the agent-ops review panel for this user. Follow these steps in order; every
command below already prints what to do next, so relay its output rather than paraphrasing
from memory.

Every command below finds the plugin through Claude Code's plugin-root variable, and for
omp through its plugin cache (the second `PYTHONPATH` entry). If neither applies (a
project-scoped or hand-copied install) it fails with `No module named agent_ops`; then add
the plugin root, three directories above the agent-ops skill file, as another `PYTHONPATH`
entry. Never pip-install anything to fix it: `agentops` on PyPI is an unrelated product.

1. If `~/.agent-ops/panel.toml` already exists, say so and skip to step 3 — `init` never
   overwrites, and neither should you.

2. Ask which path they want (one question):
   - **OpenRouter** (recommended): one API key, three cheap diverse model families.
   - **Local Ollama**: no key at all, runs on their machine, weaker seats.
   - **Ollama hosted API**: one `OLLAMA_API_KEY`, no daemon, cloud-sized models.

   Then run the matching command:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core:$HOME/.omp/plugins/cache/plugins/agent-ops___agent-ops___0.4.1/core" python3 -m agent_ops init            # OpenRouter
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core:$HOME/.omp/plugins/cache/plugins/agent-ops___agent-ops___0.4.1/core" python3 -m agent_ops init --ollama   # local
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core:$HOME/.omp/plugins/cache/plugins/agent-ops___agent-ops___0.4.1/core" python3 -m agent_ops init --ollama-cloud   # hosted
   ```

3. Make sure the credential/daemon side is ready:
   - OpenRouter: they need `OPENROUTER_API_KEY` exported (keys at https://openrouter.ai/keys).
     Do not ask them to paste the key into the chat — they export it in their shell.
   - Ollama: the models in panel.toml must match `ollama list`; help them edit the file if not.
   - Ollama hosted API: they need `OLLAMA_API_KEY` exported (keys at
     https://ollama.com/settings/keys), same rule: never ask for it in chat.

   If their endpoint is none of these, `docs/PROVIDERS.md` covers any OpenAI-compatible
   endpoint (config only) and writing a custom provider class (one small file).

4. Probe the seats and read the result to them honestly:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core:$HOME/.omp/plugins/cache/plugins/agent-ops___agent-ops___0.4.1/core" python3 -m agent_ops probe
   ```

   Exit 0 = healthy. Exit 1 = fewer than two usable families — the panel cannot fill and
   the fix is adding/replacing seats, not ignoring the warning.

5. Offer to run a first real review on the current repo:

   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/core:$HOME/.omp/plugins/cache/plugins/agent-ops___agent-ops___0.4.1/core" python3 -m agent_ops <repo> --coder <model that wrote the code>
   ```

   Point them at the agent-ops skill and `docs/PLAYBOOK.md` for how to read reports:
   findings are leads to verify, not verdicts — and after verifying each one, close the
   loop with `python3 -m agent_ops verdict <run-id> <family> <n> confirmed|fp` so
   `agent_ops stats` can report each seat's real false-positive rate.
