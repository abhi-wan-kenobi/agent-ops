"""Load and validate panel.toml — the user-owned description of seats and providers.

The config is the seam that makes the panel portable: everything the runner needs to reach
a model (endpoint, auth env var, model id) lives here, never in code. TOML via stdlib
`tomllib`, which sets the Python floor at 3.11.

Machine-written state (roster, run records, stats) stays JSON and lives under `state_dir`;
this file is only ever written by a human.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib

DEFAULT_CONFIG_PATH = "~/.agent-ops/panel.toml"
DEFAULT_OUTROOT = "~/.agent-ops/audits"
DEFAULT_STATE_DIR = "~/.agent-ops/state"
DEFAULT_MAX_PAYLOAD = 400_000
# Output budget per seat. 8000 is a measured calibration, not a guess: a tighter cap does
# not measure reasoning burn, it MANUFACTURES it — at max_tokens=2000 seven healthy models
# all returned empty content cut off mid-reasoning. Do not lower without re-measuring.
DEFAULT_MAX_TOKENS = 8000
# One panel at a time. Concurrency against a rate-limited endpoint does not fail cleanly —
# it queues, and the tail grows until a seat blows its budget. Raise only after measuring
# YOUR provider serving that many concurrent panels cleanly.
DEFAULT_LEASE_SLOTS = 1


class ConfigError(Exception):
    """A problem a user must fix in panel.toml (or their environment) before a run."""


@dataclasses.dataclass(frozen=True)
class Seat:
    name: str
    family: str
    provider: str
    model: str


@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str
    base_url: str
    api_key_env: str | None = None


@dataclasses.dataclass(frozen=True)
class Config:
    outroot: pathlib.Path
    state_dir: pathlib.Path
    max_payload: int
    max_tokens: int
    lease_slots: int
    seats: list[Seat]
    providers: dict[str, ProviderConfig]
    path: pathlib.Path | None = None

    @property
    def runs_dir(self) -> pathlib.Path:
        return self.state_dir / "runs"

    @property
    def lease_dir(self) -> pathlib.Path:
        return self.state_dir / "lease"

    @property
    def roster_path(self) -> pathlib.Path:
        return self.state_dir / "roster.json"

    @property
    def stats_path(self) -> pathlib.Path:
        return self.state_dir / "stats.jsonl"


def _expand(p: str) -> pathlib.Path:
    # Anchored to an absolute path at load time: a relative outroot/state_dir would
    # silently point somewhere else if anything later changes the working directory.
    return pathlib.Path(os.path.abspath(pathlib.Path(p).expanduser()))


def _require_str(table: dict, key: str, where: str) -> str:
    v = table.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{where}: '{key}' is required and must be a non-empty string")
    return v.strip()


def load_config(path: str | pathlib.Path | None = None) -> Config:
    """Parse and validate the panel config.

    Raises ConfigError with a message a stranger can act on. Validation is strict on
    purpose: a mis-named provider or missing model id must fail HERE, before any seat
    runs, not surface later as a seat that silently reports nothing.
    """
    explicit = path is not None
    cfg_path = _expand(str(path or DEFAULT_CONFIG_PATH))
    if not cfg_path.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {cfg_path}")
        # No config at all: defaults with zero seats. `probe`/`audit` will refuse loudly;
        # `runs` still works. This keeps `--help`-style exploration from demanding a file.
        return Config(outroot=_expand(DEFAULT_OUTROOT), state_dir=_expand(DEFAULT_STATE_DIR),
                      max_payload=DEFAULT_MAX_PAYLOAD, max_tokens=DEFAULT_MAX_TOKENS,
                      lease_slots=DEFAULT_LEASE_SLOTS, seats=[], providers={}, path=None)

    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{cfg_path}: not valid TOML: {e}") from e

    ops = raw.get("agent_ops") or {}
    if not isinstance(ops, dict):
        raise ConfigError(f"{cfg_path}: [agent_ops] must be a table")
    max_payload = ops.get("max_payload", DEFAULT_MAX_PAYLOAD)
    max_tokens = ops.get("max_tokens", DEFAULT_MAX_TOKENS)
    lease_slots = ops.get("lease_slots", DEFAULT_LEASE_SLOTS)
    for key, val in (("max_payload", max_payload), ("max_tokens", max_tokens),
                     ("lease_slots", lease_slots)):
        if not isinstance(val, int) or val <= 0:
            raise ConfigError(f"{cfg_path}: [agent_ops].{key} must be a positive integer")

    providers: dict[str, ProviderConfig] = {}
    for name, table in (raw.get("providers") or {}).items():
        if not isinstance(table, dict):
            raise ConfigError(f"{cfg_path}: [providers.{name}] must be a table")
        where = f"{cfg_path}: [providers.{name}]"
        api_key_env = table.get("api_key_env")
        if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env):
            raise ConfigError(f"{where}: 'api_key_env' must be a non-empty string when set")
        base_url = _require_str(table, "base_url", where).rstrip("/")
        # Catch the typo class here, with a message naming the fix, instead of letting
        # every seat fail later with urllib's "unknown url type".
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}: 'base_url' must start with http:// or https:// "
                              f"(got {base_url!r})")
        providers[name] = ProviderConfig(
            name=name,
            type=_require_str(table, "type", where),
            base_url=base_url,
            api_key_env=api_key_env,
        )

    seats: list[Seat] = []
    seen_names: set[str] = set()
    for i, table in enumerate(raw.get("seats") or []):
        if not isinstance(table, dict):
            raise ConfigError(f"{cfg_path}: [[seats]] entry {i + 1} must be a table")
        where = f"{cfg_path}: [[seats]] entry {i + 1}"
        seat = Seat(
            name=_require_str(table, "name", where),
            family=_require_str(table, "family", where).lower(),
            provider=_require_str(table, "provider", where),
            model=_require_str(table, "model", where),
        )
        if seat.name in seen_names:
            raise ConfigError(f"{where}: duplicate seat name {seat.name!r}")
        seen_names.add(seat.name)
        if seat.provider not in providers:
            raise ConfigError(
                f"{where}: provider {seat.provider!r} is not defined — add a "
                f"[providers.{seat.provider}] table or fix the reference")
        seats.append(seat)

    return Config(
        outroot=_expand(str(ops.get("outroot", DEFAULT_OUTROOT))),
        state_dir=_expand(str(ops.get("state_dir", DEFAULT_STATE_DIR))),
        max_payload=max_payload,
        max_tokens=max_tokens,
        lease_slots=lease_slots,
        seats=seats,
        providers=providers,
        path=cfg_path,
    )
