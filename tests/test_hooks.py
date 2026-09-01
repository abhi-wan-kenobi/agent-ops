"""Both safety hooks, exercised the way Claude Code runs them: a subprocess fed hook JSON
on stdin, judged on exit code (0 allow, 2 block). Unit tests cover the parsing seams.

HOME and CLAUDE_PROJECT_DIR are pointed at tmp dirs in every subprocess call so no test
can ever read the developer's real hooks.toml.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

HOOKS_DIR = pathlib.Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import dangerous_git_hook  # noqa: E402
import hook_config  # noqa: E402
from protection_hook import live_tags, text_outside_code  # noqa: E402


def run_hook(script: str, payload: dict, home: pathlib.Path,
             project: pathlib.Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script)],
        input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30)


@pytest.fixture()
def dirs(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(); project.mkdir()
    return home, project


def _write_cfg(base: pathlib.Path, body: str) -> None:
    d = base / ".agent-ops"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hooks.toml").write_text(textwrap.dedent(body), encoding="utf-8")


def bash(command: str, cwd: str | None = None) -> dict:
    p = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        p["cwd"] = cwd
    return p


def edit(path: str, new: str = "harmless", tool: str = "Edit") -> dict:
    if tool == "Write":
        ti = {"file_path": path, "content": new}
    else:
        ti = {"file_path": path, "old_string": "x", "new_string": new}
    return {"tool_name": tool, "tool_input": ti}


# ── hook_config: layering and fail-open ─────────────────────────────────────────────────

def test_defaults_apply_with_no_config_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook_config, "USER_CONFIG", str(tmp_path / "absent.toml"))
    cfg = hook_config.load_hooks_config(tmp_path)
    assert "push --force" in cfg["dangerous_git"]["always_block"]
    assert cfg["protection"]["tags"]["readonly"] == "claude-readonly"


def test_project_overrides_user_per_key(tmp_path, monkeypatch):
    user_home = tmp_path / "home"; project = tmp_path / "proj"
    _write_cfg(user_home, """
        [dangerous_git]
        always_block = ["push"]
        shared_worktrees = ["/from-user"]
    """)
    _write_cfg(project, """
        [dangerous_git]
        always_block = ["rebase"]
    """)
    monkeypatch.setattr(hook_config, "USER_CONFIG",
                        str(user_home / ".agent-ops/hooks.toml"))
    cfg = hook_config.load_hooks_config(project)
    assert cfg["dangerous_git"]["always_block"] == ["rebase"], "project wins per key"
    assert cfg["dangerous_git"]["shared_worktrees"] == ["/from-user"], (
        "keys the project does not set fall through to the user layer")


def test_tags_merge_per_tag_name(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write_cfg(project, """
        [protection]
        tags = { readonly = "KEEP-OUT" }
    """)
    monkeypatch.setattr(hook_config, "USER_CONFIG", str(tmp_path / "absent.toml"))
    cfg = hook_config.load_hooks_config(project)
    assert cfg["protection"]["tags"]["readonly"] == "KEEP-OUT"
    assert cfg["protection"]["tags"]["ignore"] == "claude-ignore", (
        "renaming one tag must not silently disable the others")


def test_malformed_config_fails_open_to_defaults(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write_cfg(project, "[dangerous_git\nbroken =")
    monkeypatch.setattr(hook_config, "USER_CONFIG", str(tmp_path / "absent.toml"))
    cfg = hook_config.load_hooks_config(project)
    assert "push --force" in cfg["dangerous_git"]["always_block"], (
        "a malformed layer must degrade to the defaults, not to nothing")


# ── protection hook ──────────────────────────────────────────────────────────────────────

def test_edit_under_a_readonly_root_is_blocked(dirs):
    home, project = dirs
    guarded = project / "vault"
    guarded.mkdir()
    _write_cfg(project, f"""
        [protection]
        readonly_roots = ["{guarded}"]
    """)
    r = run_hook("protection_hook.py", edit(str(guarded / "notes.md")), home, project)
    assert r.returncode == 2, r.stderr
    assert "read-only root" in r.stderr


def test_normal_edit_passes(dirs):
    home, project = dirs
    r = run_hook("protection_hook.py", edit(str(project / "code.py")), home, project)
    assert r.returncode == 0, r.stderr


def test_editing_a_tag_protected_file_is_blocked_with_zero_config(dirs):
    home, project = dirs
    f = project / "owned.md"
    f.write_text("human-owned\nclaude-readonly\n", encoding="utf-8")
    r = run_hook("protection_hook.py", edit(str(f)), home, project)
    assert r.returncode == 2, r.stderr
    assert "claude-readonly" in r.stderr


def test_a_tag_inside_a_code_fence_does_not_protect(dirs):
    home, project = dirs
    f = project / "docs.md"
    f.write_text("docs about tags\n```\nclaude-readonly\n```\n", encoding="utf-8")
    r = run_hook("protection_hook.py", edit(str(f)), home, project)
    assert r.returncode == 0, r.stderr


def test_a_tag_in_inline_code_does_not_protect(dirs):
    home, project = dirs
    f = project / "docs.md"
    f.write_text("use the `claude-ignore` tag for that\n", encoding="utf-8")
    r = run_hook("protection_hook.py", edit(str(f)), home, project)
    assert r.returncode == 0, r.stderr


def test_introducing_a_bare_tag_is_blocked(dirs):
    home, project = dirs
    r = run_hook("protection_hook.py",
                 edit(str(project / "new.md"), new="claude-readonly\nmine now\n",
                      tool="Write"),
                 home, project)
    assert r.returncode == 2, r.stderr
    assert "introduces" in r.stderr


def test_mentioning_a_tag_in_a_fence_is_not_introducing_it(dirs):
    home, project = dirs
    r = run_hook("protection_hook.py",
                 edit(str(project / "new.md"), new="```\nclaude-readonly\n```\n",
                      tool="Write"),
                 home, project)
    assert r.returncode == 0, r.stderr


def test_custom_tag_name_from_project_config(dirs):
    home, project = dirs
    _write_cfg(project, """
        [protection]
        tags = { readonly = "DO-NOT-TOUCH" }
    """)
    f = project / "owned.md"
    f.write_text("DO-NOT-TOUCH\n", encoding="utf-8")
    r = run_hook("protection_hook.py", edit(str(f)), home, project)
    assert r.returncode == 2, r.stderr


def test_protection_hook_fails_open_on_garbage_stdin(dirs):
    home, project = dirs
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project)}
    r = subprocess.run([sys.executable, str(HOOKS_DIR / "protection_hook.py")],
                       input="not json at all", text=True, capture_output=True, env=env)
    assert r.returncode == 0


def test_text_outside_code_units():
    assert "tag" in text_outside_code("tag\n```\nfenced\n```\n")
    assert "fenced" not in text_outside_code("x\n```\nfenced\n```\n")
    assert "span" not in text_outside_code("before `span` after")
    assert live_tags("claude-readonly", {"readonly": "claude-readonly"}) == {"readonly"}
    assert live_tags("`claude-readonly`", {"readonly": "claude-readonly"}) == set()


# ── dangerous-git hook ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git push --force",
    "git push -f",
    "git push origin main --force",
    "git push --force-with-lease origin main",
    "git reset --hard HEAD~3",
    "git -C /some/repo reset --hard",
    "git clean -fdx",
    "git branch -D feature",
    "git checkout .",
    "git restore .",
    "cd /tmp && git reset --hard",
    "ls; git push -f",
])
def test_destructive_git_is_blocked_with_zero_config(dirs, cmd):
    home, project = dirs
    r = run_hook("dangerous_git_hook.py", bash(cmd), home, project)
    assert r.returncode == 2, f"{cmd!r} was allowed: {r.stderr}"
    assert "BLOCKED" in r.stderr


@pytest.mark.parametrize("cmd", [
    "git push",
    "git push origin main",
    "git status",
    "git checkout main",
    "git restore file.py",
    "git branch -d merged-branch",
    "git commit -m 'never run git push --force'",
    "echo 'git reset --hard is dangerous'",
    "ls -la",
])
def test_ordinary_commands_pass(dirs, cmd):
    home, project = dirs
    r = run_hook("dangerous_git_hook.py", bash(cmd), home, project)
    assert r.returncode == 0, f"{cmd!r} was blocked: {r.stderr}"


def test_heredoc_bodies_are_stripped_before_matching(dirs):
    home, project = dirs
    cmd = "cat > notes.md <<'EOF'\ngit reset --hard\nEOF"
    r = run_hook("dangerous_git_hook.py", bash(cmd), home, project)
    assert r.returncode == 0, f"heredoc body tripped the hook: {r.stderr}"


def test_a_dangerous_command_after_a_heredoc_is_still_caught(dirs):
    home, project = dirs
    cmd = "cat > f <<'EOF'\nharmless\nEOF\ngit push --force"
    r = run_hook("dangerous_git_hook.py", bash(cmd), home, project)
    assert r.returncode == 2, r.stderr


def test_shared_worktree_extra_blocks_apply_inside_only(dirs):
    home, project = dirs
    shared = project / "shared-wt"
    shared.mkdir()
    _write_cfg(project, f"""
        [dangerous_git]
        shared_worktrees = ["{shared}"]
    """)
    inside = run_hook("dangerous_git_hook.py", bash("git rebase main", cwd=str(shared)),
                      home, project)
    assert inside.returncode == 2, inside.stderr
    assert "shared worktree" in inside.stderr
    outside = run_hook("dangerous_git_hook.py", bash("git rebase main", cwd=str(project)),
                       home, project)
    assert outside.returncode == 0, outside.stderr


def test_git_dash_c_into_a_shared_worktree_is_scoped(dirs):
    home, project = dirs
    shared = project / "shared-wt"
    shared.mkdir()
    _write_cfg(project, f"""
        [dangerous_git]
        shared_worktrees = ["{shared}"]
    """)
    r = run_hook("dangerous_git_hook.py",
                 bash(f"git -C {shared} commit --amend", cwd=str(project)), home, project)
    assert r.returncode == 2, r.stderr


def test_project_config_can_replace_the_block_list(dirs):
    home, project = dirs
    _write_cfg(home, """
        [dangerous_git]
        always_block = ["push"]
    """)
    _write_cfg(project, """
        [dangerous_git]
        always_block = ["stash drop"]
    """)
    allowed = run_hook("dangerous_git_hook.py", bash("git push"), home, project)
    assert allowed.returncode == 0, "project layer must win per key"
    blocked = run_hook("dangerous_git_hook.py", bash("git stash drop"), home, project)
    assert blocked.returncode == 2


def test_dangerous_git_hook_fails_open_on_garbage_stdin(dirs):
    home, project = dirs
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project)}
    r = subprocess.run([sys.executable, str(HOOKS_DIR / "dangerous_git_hook.py")],
                       input="{{{", text=True, capture_output=True, env=env)
    assert r.returncode == 0


def test_unbalanced_quotes_fail_open_not_crash(dirs):
    home, project = dirs
    r = run_hook("dangerous_git_hook.py", bash("echo 'unclosed"), home, project)
    assert r.returncode == 0


# unit seams

def test_strip_heredocs_keeps_the_command_line_itself():
    out = dangerous_git_hook.strip_heredocs("cat <<EOF\nbody line\nEOF\necho after")
    assert "cat <<EOF" in out
    assert "body line" not in out
    assert "echo after" in out


def test_matches_is_token_wise_and_order_sensitive():
    assert dangerous_git_hook.matches("push --force", ["push", "origin", "--force"])
    assert not dangerous_git_hook.matches("push --force", ["--force-reading", "push"])
    assert dangerous_git_hook.matches("clean -f", ["clean", "-fdx"])
    assert not dangerous_git_hook.matches("branch -D", ["branch", "-d", "x"]), (
        "-D must not match the lowercase -d")
    assert not dangerous_git_hook.matches("checkout .", ["checkout", ".config"])


def test_git_invocation_parses_global_options():
    args, c = dangerous_git_hook.git_invocation(
        ["git", "-C", "/repo", "-c", "user.name=x", "reset", "--hard"])
    assert args[:2] == ["reset", "--hard"]
    assert c == "/repo"
    assert dangerous_git_hook.git_invocation(
        ["echo", "a quoted mention of git push --force"]) is None, (
        "quoted text is one shlex token and must not read as an invocation")
    args, _ = dangerous_git_hook.git_invocation(["echo", "$(git", "push", "--force)"])
    assert args[0] == "push", "a substitution-wrapped git is an invocation"


def test_command_substitution_cannot_hide_a_dangerous_git_command(dirs):
    """Audit finding: `$(git reset --hard)` hid the git invocation behind a non-command
    token. Substitution content is now scanned as its own segment."""
    home, project = dirs
    for cmd in ("echo $(git reset --hard)", "OUT=`git push --force`"):
        r = run_hook("dangerous_git_hook.py", bash(cmd), home, project)
        assert r.returncode == 2, f"{cmd!r} was allowed: {r.stderr}"
    ok = run_hook("dangerous_git_hook.py", bash("echo $(git rev-parse HEAD)"), home, project)
    assert ok.returncode == 0, ok.stderr


def test_quoted_paren_does_not_split_a_dangerous_command_apart(dirs):
    """Audit finding on the previous fix: splitting on `)` let a dangerous command whose
    quoted argument contained a paren escape in two pieces."""
    home, project = dirs
    r = run_hook("dangerous_git_hook.py",
                 bash('git push "(tagged)" --force'), home, project)
    assert r.returncode == 2, r.stderr


def test_subshell_wrapped_dangerous_command_is_caught(dirs):
    """Audit finding: `(git push --force)` hid the invocation behind the paren glued to
    the git token."""
    home, project = dirs
    r = run_hook("dangerous_git_hook.py", bash("(git push --force)"), home, project)
    assert r.returncode == 2, r.stderr
