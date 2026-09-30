# Providers

A **provider** is an endpoint that runs a seat's model. Every `[[seats]]` entry names one,
and every provider is a `[providers.<name>]` table in `panel.toml`. The `type` key picks
the client. Three types ship; anything else is a file you add yourself.

| `type` | Talks to | Key |
|---|---|---|
| `openrouter` | OpenRouter | `api_key_env` |
| `ollama` | Ollama — a local daemon or the hosted API | none locally, `api_key_env` hosted |
| `openai-compatible` | Any endpoint that clones OpenAI's `/chat/completions` | `api_key_env` if it wants one |
| `module:ClassName` | Your own class — see [Custom providers](#custom-providers) | up to you |

`api_key_env` **names** an environment variable. The key itself never lives in the file, and
an `Authorization` header in `panel.toml` is refused for the same reason.

## Ollama

Two ways to reach it, and they name models differently.

**Local daemon** — no key. Ids are what `ollama list` prints. A signed-in daemon also serves
Ollama's cloud models: add `:cloud` (for example `gpt-oss:120b-cloud`).

```toml
[providers.ollama]
type = "ollama"
base_url = "http://localhost:11434/v1"
```

**Hosted API** — one key, no daemon. Ids come from the public listing
(`curl https://ollama.com/v1/models`) and carry no `-cloud` suffix (`gpt-oss:120b`).

```toml
[providers.ollama-cloud]
type = "ollama"
base_url = "https://ollama.com/v1"
api_key_env = "OLLAMA_API_KEY"
```

`agent_ops init --ollama` and `agent_ops init --ollama-cloud` write a starter panel for each.

A seat may leave the tag off (`model = "llama3.1"`): Ollama serves that as `:latest`, and
the preflight check knows it. Only `:latest` is implied — a bare name does not match another
pulled tag, and an explicit tag never falls back.

The hosted API bills usage against your plan's credits, and running several panels at once
queues on your plan's concurrency limit. Keep `lease_slots = 1` until you have measured
otherwise.

## Any OpenAI-compatible endpoint

No code needed. Give it a name, a base URL and, if it wants one, a key variable:

```toml
[providers.my-endpoint]
type = "openai-compatible"
base_url = "https://api.example.com/v1"
api_key_env = "MY_ENDPOINT_KEY"

[providers.my-endpoint.headers]        # optional: attribution, routing, spend tagging
"X-Cost-Center" = "platform-eng"
```

`base_url` is joined with `/chat/completions` (and `/models` for the preflight listing).
That covers most hosted and self-hosted inference servers.

## Custom providers

When an endpoint speaks a different dialect — another path, another auth header, another
request or response shape — write a provider class. It is one small file.

1. Put a Python file in `~/.agent-ops/providers/` (or anywhere on `PYTHONPATH`).
   `AGENT_OPS_HOME` moves that directory, exactly as it moves the rest of agent-ops.
2. Subclass `agent_ops.providers.BaseProvider` and override only what differs.
3. Point a provider at it with `type = "<file name without .py>:<ClassName>"`.

```toml
[providers.mine]
type = "mydialect:MessagesProvider"
base_url = "https://api.example.com/v1"
api_key_env = "MY_KEY"
```

`BaseProvider` already owns everything hard: retries with backoff, timeouts, 429 handling,
turning transport failures into a seat error instead of a crash, redacting secrets out of
error text, and the preflight listing. You override the dialect hooks:

| Hook | Default | Override to |
|---|---|---|
| `chat_path` | `"/chat/completions"` | call a different path under `base_url` |
| `build_body(model, messages, *, max_tokens, temperature)` | OpenAI request body | change the request shape |
| `parse_response(data)` | OpenAI `choices[0].message` | read another response shape |
| `_headers()` | JSON headers plus `Authorization: Bearer <key>` | authenticate another way |
| `list_models()` | `GET {base_url}/models` | return `None` when there is no listing |
| `lists_model(model, listed)` | exact id match | match ids the way the listing spells them |

`parse_response` returns a dict of `content`, `reasoning` and `finish_reason` — or
`{"error": "..."}` when the body reports a failure. A body of the wrong shape can simply
raise `KeyError`, `IndexError`, `AttributeError`, `TypeError` or `ValueError`: the panel
records "malformed response shape" for that seat and carries on.

A complete provider for an Anthropic-style Messages endpoint:

```python
# ~/.agent-ops/providers/mydialect.py
from agent_ops.providers import BaseProvider


class MessagesProvider(BaseProvider):
    chat_path = "/messages"

    def _headers(self):
        h = super()._headers()
        h.pop("Authorization", None)            # this endpoint wants a different header
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    def build_body(self, model, messages, *, max_tokens, temperature):
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [m for m in messages if m["role"] != "system"]}
        system = [m["content"] for m in messages if m["role"] == "system"]
        if system:
            body["system"] = "\n".join(system)
        return body

    def parse_response(self, data):
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        return {"content": text, "finish_reason": data.get("stop_reason") or ""}
```

Extra headers a dialect needs on every request (a version pin, say) go in
`[providers.<name>.headers]` like any other provider.

### Rules and limits

- **It is code you run.** A custom type imports a Python module with your privileges. Load
  only modules you wrote or read. Do not point `--config` at a `panel.toml` from a repo you
  do not trust: a `module:ClassName` type in it will import whatever answers to that name.
- The drop-in directory is **appended** to the import path, never prepended, so a stray file
  there cannot shadow the standard library or agent-ops itself.
- Every failure — module missing, import raises, class not a `BaseProvider`, constructor
  raises — is reported as a config error before any seat runs, naming what to fix.
- Seats get no tools: a provider only ever sends one chat request and reads one reply.
- Run `agent_ops probe` after adding one. It scores each seat on a known-defect diff, which
  is the fastest way to find out your `parse_response` is reading the wrong field.
