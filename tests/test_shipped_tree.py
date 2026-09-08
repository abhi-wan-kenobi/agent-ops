"""Nothing machine-specific may reach the public tree.

This repo's whole premise is that the panel was decoupled from the machine it grew on. A
hostname, an internal address, an account name or a developer's home path in a shipped
file would be both an embarrassment and a leak, and the gitignored working documents in
this repo are full of them — one careless `git add -A` away.

Two layers, because neither alone is enough:

  * generic patterns catch shapes nobody should ship (private and CGNAT addresses, home
    directories, internal-only hostname suffixes) without needing to know the specifics;
  * a hashed denylist catches the specific tokens this machine actually uses. They are
    stored as SHA-256 digests so THIS FILE does not contain the strings it hunts for —
    the same trick, and for the same reason, as the secret gate's fragment assembly in
    classify.py.

Scope is `git ls-files` minus tests/, i.e. exactly what a `/plugin install` delivers.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Shapes that are never legitimate in a shipped file.
_GENERIC = [
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private address (10/8)"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private address (172.16/12)"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private address (192.168/16)"),
    (r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", "CGNAT address"),
    (r"/home/[a-z][a-z0-9_-]+/", "developer home path"),
    (r"/Users/[A-Za-z][A-Za-z0-9_-]+/", "developer home path"),
    # (?!\.) so a FILENAME like panel.local.toml is not read as a hostname; a real host
    # ends there or is followed by a port, slash or quote.
    (r"\b[a-z0-9-]+\.(local|lan|internal)\b(?!\.)", "internal-only hostname"),
    (r"\b[a-z0-9-]+\.ts\.net\b", "tailnet hostname"),
    # Ollama's documented default port is the one legitimate localhost in the tree.
    (r"\blocalhost:(?!11434\b)\d+", "hard-coded local service port"),
]

# SHA-256 of this machine's own tokens: host, accounts, user, employer, the old gateway.
_DENY_HASHES = {
    "e23eed321b04c128e9aba0d73a00f5c99fc5082c9a2703c2b95799cbf636aa1b",
    "043f384f1a423199609f748dc48ced4cadc4ac422a410e4f03b4bd32d85109c7",
    "0a0ecbb7a6ea8511818619dcd2a180a3efbb56da0684d015a1abebea996ff9d3",
    "b7444991afa472669fd34d049c82a9e9e003aa1431eee27b78a7a7eb7c49f53c",
    "ceb09f4cec679117ae39707cf8f0efc2447870912b51489e47cdea169ede967d",
    "19ca9dfd84957206bd38d114177971fa4b7dcbfadc0c09d79adc0257d30b5ded",
    "bb72a486832ae5421e157118c9032493e585798f5f7d360c3f67fa7bbd12625d",
    "dcc8d7506b63db23eb69667c87a92d82a8bfa0f1320fda7c40f425b5b5db8d42",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


def _tokens(text: str):
    """Whole tokens and their hyphen/dot/colon-separated pieces.

    Both, because a denylisted name appears in the wild either bare or as part of a
    compound ('<host>-infra', 'ollama-<account>'), and a whole-token check alone would
    miss the compound while a pieces-only check would miss the bare form.
    """
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        yield tok
        for piece in re.split(r"[.:-]", tok):
            if len(piece) > 3:
                yield piece


def check(text: str) -> list[str]:
    """Return human-readable reasons this text must not ship. Empty means clean."""
    hits = []
    for pattern, why in _GENERIC:
        m = re.search(pattern, text)
        if m:
            hits.append(f"{why}: {m.group(0)!r}")
    for tok in _tokens(text):
        if hashlib.sha256(tok.encode()).hexdigest() in _DENY_HASHES:
            hits.append(f"denylisted token of length {len(tok)}")
    return hits


def _shipped_files() -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout.split()
    return [ROOT / f for f in out
            if not f.startswith("tests/") and (ROOT / f).is_file()]


def test_there_are_shipped_files_to_check():
    """A gate that silently checks nothing passes forever. Playbook rule 1, applied to
    the gate itself: require positive evidence of work."""
    files = _shipped_files()
    assert len(files) > 15, f"only {len(files)} shipped files found — is git ls-files working?"


@pytest.mark.parametrize("path", _shipped_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_machine_specifics_in_shipped_file(path):
    text = path.read_bytes().decode("utf-8", errors="replace")
    hits = check(text)
    assert not hits, f"{path.relative_to(ROOT)} must not ship: {'; '.join(hits)}"


@pytest.mark.parametrize("planted,why", [
    ("base_url = \"http://192.168.1.9:4000/v1\"", "private address"),
    ("path = \"/home/someone/projects/x\"", "home path"),
    ("host = \"box.ts.net\"", "internal hostname"),
    ("url = \"http://localhost:4001/v1\"", "hard-coded port"),
])
def test_the_gate_catches_a_planted_hit(planted, why):
    """A green gate is only meaningful if a red one is reachable."""
    assert check(planted), f"gate missed a planted {why}: {planted!r}"


def test_the_documented_ollama_default_is_allowed():
    """The one localhost that legitimately ships. If this ever starts failing, the
    exception was written too narrowly and the example config will not load."""
    assert check('base_url = "http://localhost:11434/v1"') == []
