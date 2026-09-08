"""agent-ops — adversarial multi-model review panel.

Portable core: plain-HTTP providers the user configures, a file lease, durable run
records, and a panel runner. No vendor, host, or gateway assumptions live here; all of
that is in the user's panel.toml.
"""

# Single source of truth for the version. .claude-plugin/plugin.json and the
# CHANGELOG heading must agree; tests/test_version.py enforces that.
__version__ = "0.3.0"
