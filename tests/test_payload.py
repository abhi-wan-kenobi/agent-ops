"""Payload construction against real git repos — the parsing is only correct if it
matches what git actually emits, so no hand-written diff strings here.

The new-file dedup these pin: a new file's diff IS its full text with every line prefixed
'+', so inlining both doubles the payload for zero information. `--only` cannot shrink a
single file, so the doubling had to be fixed at the source.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

from agent_ops.payload import build_payload, run_git, split_diff_blocks


def _repo(tmp: pathlib.Path, committed: dict[str, str], then: dict[str, str]) -> pathlib.Path:
    """Build a git repo: `committed` files in HEAD, then `then` applied and staged."""
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp, check=True,
                                    capture_output=True, text=True)
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    for name, body in committed.items():
        (tmp / name).write_text(body, encoding="utf-8")
    run("add", "-A"); run("commit", "-qm", "base")
    for name, body in then.items():
        (tmp / name).write_text(body, encoding="utf-8")
    run("add", "-A")
    return tmp


BODY = "".join(f"line {i} of a file that is long enough to measure\n" for i in range(120))


def test_new_file_is_not_inlined_twice(tmp_path):
    repo = _repo(tmp_path, {"keep.txt": "x\n"}, {"new.py": BODY})
    payload, files, desc = build_payload(repo, "uncommitted")
    assert files == ["new.py"], files
    assert "===== FULL FILE: new.py" in payload, "the file's text must still be present"
    # The payload should be about one copy of the file, not two. Generous bound: the
    # header and note are small, so anything near 2x means the hunks came back.
    assert len(payload) < len(BODY) * 1.5, (
        f"new file appears to be inlined twice: payload {len(payload)} vs file {len(BODY)}")
    assert "+line 0 of a file" not in payload, "'+'-prefixed hunk lines should be gone"
    assert "newly added" in payload, "the empty DIFF section must explain itself"
    assert "new file(s) shown in full" in desc, desc


def test_modified_file_keeps_both_halves(tmp_path):
    """The dedup must NOT touch modified files: there the diff says what CHANGED, and the
    full text alone cannot. Losing that would be worse than the doubling."""
    repo = _repo(tmp_path, {"mod.py": BODY}, {"mod.py": BODY + "appended\n"})
    payload, files, desc = build_payload(repo, "uncommitted")
    assert files == ["mod.py"], files
    assert "@@" in payload, "modified file lost its diff hunks"
    assert "+appended" in payload, "modified file lost the added line"
    assert "===== FULL FILE: mod.py" in payload
    assert "new file(s)" not in desc, desc


def test_mixed_scope_drops_only_the_new_file_hunks(tmp_path):
    repo = _repo(tmp_path, {"mod.py": "a\nb\nc\n"},
                 {"mod.py": "a\nCHANGED\nc\n", "new.py": BODY})
    payload, files, _ = build_payload(repo, "uncommitted")
    assert files == ["mod.py", "new.py"], files
    assert "+CHANGED" in payload, "the modified file's hunk was dropped too"
    assert "+line 0 of a file" not in payload, "the new file's hunks survived"
    for f in files:
        assert f"===== FULL FILE: {f}" in payload, f
    # And the diff section that remains must not mention the new file's block header, or
    # the seat is told to look for hunks that are not there.
    diff_section = payload.split("===== FULL FILE:", 1)[0]
    assert "new.py" not in diff_section, diff_section[:400]


def test_unreadable_new_file_keeps_its_hunks(tmp_path):
    """If the FULL FILE section is going to be skipped, the hunks are the only record.

    Dropping both would make the change vanish from the payload silently — the same class
    of invisible-absence bug the whole panel exists to catch.
    """
    # Reachable via a commit scope: the file was ADDED in the reviewed commit and deleted
    # later, so it is a new file in that diff and absent from the worktree today.
    repo = _repo(tmp_path, {"keep.txt": "x\n"}, {"gone.py": BODY})
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                    capture_output=True, text=True)
    run("commit", "-qm", "add gone.py")
    ref = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.strip()
    (repo / "gone.py").unlink()
    run("add", "-A"); run("commit", "-qm", "remove gone.py")
    payload, files, _ = build_payload(repo, f"commit:{ref}")
    assert files == ["gone.py"], files
    assert "===== FULL FILE: gone.py" not in payload, "cannot read a file that is not there"
    assert "+line 0 of a file" in payload, (
        "hunks were dropped for a file whose full text is also missing — the change is "
        "now invisible in the payload")


def test_deletion_only_diff_is_reviewed_not_empty(tmp_path):
    """Regression: a deleted file's header is '+++ /dev/null', so its path used to stay
    None, the files list came back empty, and main() declared 'nothing to review' for a
    scope whose whole point was the deletion."""
    repo = _repo(tmp_path, {"doomed.py": BODY}, {})
    (repo / "doomed.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True, text=True)
    payload, files, _ = build_payload(repo, "uncommitted")
    assert files == ["doomed.py"], files
    assert payload.strip(), "deletion-only scope produced an empty payload"
    assert "-line 0 of a file" in payload, "the deletion hunks must ship"
    assert "===== FULL FILE: doomed.py" not in payload, "cannot inline a file that is gone"


def test_mixed_edit_and_deletion_keeps_both(tmp_path):
    repo = _repo(tmp_path, {"mod.py": "a\nb\nc\n", "gone.py": BODY},
                 {"mod.py": "a\nCHANGED\nc\n"})
    (repo / "gone.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True, text=True)
    payload, files, _ = build_payload(repo, "uncommitted")
    assert files == ["gone.py", "mod.py"], files
    assert "+CHANGED" in payload, "the edited file's hunk was dropped"
    assert "-line 0 of a file" in payload, "the deleted file's hunks were dropped"
    assert "===== FULL FILE: mod.py" in payload
    assert "===== FULL FILE: gone.py" not in payload, "cannot inline a file that is gone"


def test_only_narrows_to_a_deleted_file(tmp_path):
    repo = _repo(tmp_path, {"mod.py": "a\n", "gone.py": BODY}, {"mod.py": "a\nAAA\n"})
    (repo / "gone.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True, text=True)
    payload, files, desc = build_payload(repo, "uncommitted", only="gone")
    assert files == ["gone.py"], files
    assert "-line 0 of a file" in payload and "+AAA" not in payload, (
        "only= must narrow hunks when the match is a deletion")
    assert "only files matching 'gone'" in desc, desc


def test_only_narrows_files_and_hunks(tmp_path):
    repo = _repo(tmp_path, {"a.py": "a\n", "b.py": "b\n"},
                 {"a.py": "a\nAAA\n", "b.py": "b\nBBB\n"})
    payload, files, desc = build_payload(repo, "uncommitted", only="a.py")
    assert files == ["a.py"], files
    assert "+AAA" in payload and "+BBB" not in payload, "only= no longer filters hunks"
    assert "FULL FILE: b.py" not in payload
    assert "only files matching 'a.py'" in desc, desc


def test_split_diff_blocks_loses_no_lines_and_labels_each_block(tmp_path):
    repo = _repo(tmp_path, {"a.py": "a\n", "b.py": "b\n"},
                 {"a.py": "a\nAAA\n", "b.py": "b\nBBB\n"})
    diff = run_git(["diff", "HEAD"], repo)
    blocks = split_diff_blocks(diff)
    assert [p for p, _ in blocks] == ["a.py", "b.py"], blocks
    # Reassembly must be lossless: this splitter is what the payload is rebuilt from, so a
    # dropped line here is a silently under-reported diff.
    assert "\n".join(b for _, b in blocks) == diff.rstrip("\n"), "splitter lost content"


def test_truncation_is_loud(tmp_path):
    repo = _repo(tmp_path, {"mod.py": "a\n"}, {"mod.py": BODY})
    payload, _, _ = build_payload(repo, "uncommitted", max_payload=500)
    assert "TRUNCATED at 500 chars" in payload
    assert "did NOT see the whole change" in payload
