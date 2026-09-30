"""agent-ops — adversarial multi-model review panel.

Portable core: plain-HTTP providers the user configures, a file lease, durable run
records, and a panel runner. No vendor, host, or gateway assumptions live here; all of
that is in the user's panel.toml.
"""

# Single source of truth for the version. .claude-plugin/plugin.json, the CHANGELOG
# heading and the omp plugin-cache path in the skill and /panel-setup must agree;
# tests/test_version.py enforces that.
__version__ = "0.4.1"
