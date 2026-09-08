"""One machine-readable document per run, written at EVERY exit path.

Why this exists: everything the panel knows about a run was, until now, either printed to
stderr for a human or scattered across a run record, a stats line and N markdown files.
Anything automating the panel — a CI job, a dashboard, a bot posting a review comment —
had to re-parse prose to learn what the run already knew.

The hard requirement is coverage of the FAILURE paths, not the happy one. A run that
refused on a secret, could not resolve a ref, found no routable seat, or was killed
mid-flight has no report directory and no stats line, and those are exactly the runs a
caller most needs to distinguish from "reviewed everything, found nothing". Playbook rule
1 in machine-readable form: the document always states what was read, and when nothing
was, it says which of the many reasons applied.

`outcome` is therefore a closed vocabulary, and `exit_code` travels with it:

    done              a panel convened and at least one seat reported
    failed            a panel convened and no seat reported, or a split run left files unreviewed
    no-diff           the scope resolved but contained no reviewable change
    git-error         the scope could not be resolved at all
    config-error      panel.toml, the repo path, or a provider was unusable
    no-seats          no seats configured
    no-eligible-seats coder-family exclusion (or --models) left nothing
    no-routable-seat  every seat's model is absent from its provider's catalogue
    refused-secret    the outbound payload looked like it carried a live credential
    lease-denied      another panel holds the lease
    cancelled         a cooperative cancel or a signal ended the run
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import sys

from . import __version__

SCHEMA = 1

# A consumer that pins `schema` can trust these; anything added later must be additive.
OUTCOMES = ("done", "failed", "no-diff", "git-error", "config-error", "no-seats",
            "no-eligible-seats", "no-routable-seat", "refused-secret", "lease-denied",
            "cancelled")

_SEAT_KEYS = ("seat", "family", "model", "status", "findings", "max_severity",
              "severities", "seconds", "truncated", "reason", "report")


def _seat(r: dict) -> dict:
    """Project a run_seat result onto the published shape.

    Explicit key list rather than passing the dict through: the internal result grew
    fields (chars, reasoning_chars) that are stats-lane detail, and a published schema
    that silently mirrors an internal dict is one refactor away from leaking or losing
    something a consumer pinned.
    """
    return {k: r.get(k) for k in _SEAT_KEYS}


def _worst(severities) -> str | None:
    from .classify import SEVERITY_LEVELS
    present = {s for s in severities if s}
    for level in reversed(SEVERITY_LEVELS):
        if level in present:
            return level
    return None


@dataclasses.dataclass
class Summary:
    """Accumulates what is known so far, so any exit path can emit a complete document.

    Every field has a defined value from construction. A summary emitted from the very
    first return is a valid document that simply says very little — which is the point:
    the caller always gets a file, and never has to distinguish "no summary" from "the
    run failed early".
    """
    repo: str = ""
    scope: str = ""
    coder: str | None = None
    path: pathlib.Path | None = None          # explicit --summary-json target
    run_id: str | None = None
    outdir: pathlib.Path | None = None
    scope_desc: str = ""
    files: list[str] = dataclasses.field(default_factory=list)
    payload_chars: int = 0
    payload_truncated: bool = False
    panel: list[str] = dataclasses.field(default_factory=list)
    seats: list[dict] = dataclasses.field(default_factory=list)
    split: list[dict] | None = None
    _emitted: bool = False
    _emitted_code: int = 0

    def document(self, outcome: str, exit_code: int) -> dict:
        reported = [s for s in self.seats if s.get("findings") is not None]
        doc = {
            "schema": SCHEMA,
            "version": __version__,
            "outcome": outcome,
            "exit_code": exit_code,
            "run_id": self.run_id,
            "outdir": str(self.outdir) if self.outdir else None,
            "repo": self.repo,
            "scope": self.scope,
            "scope_description": self.scope_desc,
            "coder": self.coder,
            "files": list(self.files),
            "file_count": len(self.files),
            "payload_chars": self.payload_chars,
            "payload_truncated": self.payload_truncated,
            "panel": list(self.panel),
            "panel_size": len(self.panel),
            "reported_seats": len(reported),
            "max_severity": _worst(s.get("max_severity") for s in self.seats),
            "seats": [_seat(s) for s in self.seats],
        }
        if self.split is not None:
            doc["split"] = [
                {"file": pf["file"],
                 "sub": pf["sub"],
                 "run_id": (f"{self.run_id}/{pf['sub']}"
                            if self.run_id and pf["sub"] else None),
                 "status": "reviewed" if pf["results"] is not None else "skipped",
                 "why": pf["why"],
                 "max_severity": _worst(r.get("max_severity")
                                        for r in (pf["results"] or [])),
                 "seats": [_seat(r) for r in (pf["results"] or [])]}
                for pf in self.split]
        return doc

    def emit(self, outcome: str, exit_code: int) -> int:
        """Write the document and return the exit code, so call sites read `return s.emit(...)`.

        Idempotent, and it returns the code of the FIRST emit rather than its argument.
        That is the whole invariant of this module: the process's exit status and the
        document on disk describe the same run. If a second call site could return its own
        code, a job could exit 0 while summary.json said `cancelled` — precisely the
        silent mismatch the file exists to prevent. Panel finding, 2026-09-08; not
        reachable through today's call sites, since the signal path raises SystemExit
        rather than falling through to a return, but the invariant should hold by
        construction rather than by luck.

        Never raises. A summary that cannot be written must not turn a completed review
        into a crash, so every failure is reported on stderr and the exit code survives.
        The document is built BEFORE the emitted flag is set, so a serialisation failure
        does not silently consume the one chance to describe the run.
        """
        if self._emitted:
            return self._emitted_code
        try:
            # Build AND serialise before committing. Encoding is where a bad field
            # actually fails, so validating only the dict would set the emitted flag and
            # then throw the run's one record away in the write.
            body = _render(self.document(outcome, exit_code))
        except (TypeError, ValueError) as e:                    # noqa: BLE001
            print(f">> ⚠️ could not build the run summary: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return exit_code
        self._emitted, self._emitted_code = True, exit_code
        targets = []
        if self.outdir:
            targets.append(pathlib.Path(self.outdir) / "summary.json")
        if self.path:
            targets.append(pathlib.Path(self.path))
        for t in targets:
            try:
                _write_text(t, body)
            except OSError as e:
                print(f">> ⚠️ could not write summary to {t}: {e}", file=sys.stderr)
        return self._emitted_code


def _render(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def _write_text(path: pathlib.Path, body: str) -> None:
    """Atomic within the destination directory: a reader must never see a half-written
    document. CI polls this file, and a truncated read parses as a different run."""
    path = pathlib.Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        # Residue in a directory a consumer polls is its own kind of confusion.
        tmp.unlink(missing_ok=True)
        raise


def write_summary(path: pathlib.Path, doc: dict) -> None:
    """Serialise and atomically write one summary document."""
    _write_text(path, _render(doc))
