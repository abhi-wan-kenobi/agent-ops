"""The version lives in one place. These tests are what makes that true.

Three files carried a version independently and all three disagreed at v0.2.2: the
package said 0.1.0, the plugin manifest said 0.2.2, and the outbound user agent said
0.1. Nothing was watching, so nothing complained. A wrapper that pins a core version
needs the number the core reports to be the number the core is.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

import agent_ops
from agent_ops.main import main
from agent_ops.providers import USER_AGENT

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_plugin_manifest_package_and_changelog_agree():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    changelog = (ROOT / "CHANGELOG.md").read_text()
    heading = re.search(r"^## v(\d+\.\d+\.\d+)", changelog, re.M)
    assert heading, "CHANGELOG.md has no '## vX.Y.Z' heading to compare against"
    assert manifest["version"] == agent_ops.__version__ == heading.group(1), (
        f"version drift: plugin.json={manifest['version']!r} "
        f"package={agent_ops.__version__!r} changelog={heading.group(1)!r}")


def test_omp_cache_path_in_the_docs_names_the_current_version():
    """The skill and /panel-setup give omp a fixed plugin-cache path, because omp neither
    substitutes ${CLAUDE_PLUGIN_ROOT} nor exports it. The cache directory is named after
    the plugin version, so a bump that forgets these files leaves every omp command
    pointing at a directory that no longer exists, which reads as a broken install."""
    pattern = re.compile(r"agent-ops___agent-ops___(\d+\.\d+\.\d+)")
    for rel in ("skills/agent-ops/SKILL.md", "commands/panel-setup.md"):
        found = set(pattern.findall((ROOT / rel).read_text()))
        assert found == {agent_ops.__version__}, (
            f"{rel} names omp cache version(s) {sorted(found)}, "
            f"package is {agent_ops.__version__!r}")


def test_user_agent_carries_the_package_version():
    assert USER_AGENT == f"agent-ops/{agent_ops.__version__}", USER_AGENT


@pytest.mark.parametrize("flag", ["--version", "-V", "version"])
def test_version_flag_prints_and_exits_zero(flag, capsys):
    assert main([flag]) == 0
    assert capsys.readouterr().out.strip() == f"agent-ops {agent_ops.__version__}"
