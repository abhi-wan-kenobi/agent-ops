"""classify_seat and the secret gate — the honesty layer of the whole tool.

Every case here pins a failure that happened on a real lane: dead seats printing as clean
reviews, bare terminator lines carrying confident counts, markdown-formatted reports being
discarded as empty, and the gate matching its own source.
"""
from __future__ import annotations

import pathlib

import pytest

from agent_ops import classify
from agent_ops.classify import (SEVERITY_RE, SEVERITY_VALUE_RE, SECRET_RE,
                                classify_seat, max_severity, split_findings)


def test_transport_error_seat_is_not_reported_as_zero_findings():
    """The bug this pins: a rate-limited seat printed `0 findings ⚠️ TRUNCATED`, same as a
    real seat that got cut off — so two dead seats read as two seats agreeing there was
    nothing wrong. findings must be None, because 0 is a claim about the code."""
    status, findings, reason = classify_seat(
        "", timed_out=False, failed=True,
        reason="HTTP 429: rate limit exceeded, retry later")
    assert status == "error", status
    assert findings is None, f"a seat that never ran claimed a findings count: {findings}"
    assert "429" in reason, reason


def test_timeout_is_distinguished_from_error():
    status, findings, _ = classify_seat("", timed_out=True, failed=False)
    assert (status, findings) == ("timeout", None), (status, findings)


def test_a_complete_report_survives_a_transport_complaint():
    """A real report wins over a transport error flag: a complete review that arrived
    alongside a cosmetic warning must not be discarded."""
    out = "## Findings\nSEVERITY: HIGH\nsomething real\n\nAUDIT COMPLETE - 1 findings\n"
    status, findings, _ = classify_seat(out, timed_out=False, failed=True,
                                        reason="cosmetic warning")
    assert (status, findings) == ("ok", 1), (status, findings)


def test_genuine_truncation_still_reports_inferred_findings():
    """A seat that ANSWERED and was cut off still yields an inferred count, and is still
    flagged. Only never-ran gets findings=None."""
    out = "## Findings\nSEVERITY: HIGH\nfirst\n\nSEVERITY: LOW\nsecond, cut off mid-sen"
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("truncated", 2), (status, findings)


def test_a_bare_terminator_line_is_not_a_findings_count():
    """Measured on a real lane: 5 of 23 production runs returned a 28-32 char body that was
    the terminator line and NOTHING else, e.g. the whole report being
    `AUDIT COMPLETE - 17 findings`. FINDINGS_RE matched, so the run was recorded
    `status: ok, findings: 17` — a confident count with not one finding behind it."""
    out = "AUDIT COMPLETE - 7 findings\n"
    status, findings, reason = classify_seat(out, timed_out=False, failed=False)
    assert status == "empty", status
    assert findings is None, f"a seat that reported nothing claimed a count: {findings}"
    assert "claimed 7" in reason, reason


def test_markdown_bolded_severity_headers_are_not_mistaken_for_an_empty_report():
    """Regression for a false positive the empty-body guard produced on its FIRST real
    run: one seat writes `**SEVERITY:** medium` (markdown-bolded, so `^SEVERITY:` misses
    it), and a full 3-finding report was flagged as having reported nothing."""
    out = ("## Review\n\n**SEVERITY:** medium\n**FILE:** a.py:1\n**WHAT:** " + "x" * 400 +
           "\n\nAUDIT COMPLETE - 3 findings\n")
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("ok", 3), (status, findings)


def test_a_report_that_ignores_the_severity_format_entirely_still_counts():
    """The other half of that false positive: a seat that emits no `SEVERITY` token at
    all, heading its findings `**FINDING 1 — MEDIUM**`. A report that ignores the
    requested shape is still a report — the discriminator has to be body length."""
    out = ("**FINDING 1 — MEDIUM**\n**FILE:** a.py:1\n**WHY:** " + "y" * 400 +
           "\n\nAUDIT COMPLETE - 3 findings\n")
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("ok", 3), (status, findings)


def test_severity_counter_tolerates_markdown_on_a_truncated_seat():
    """The inferred count on a truncated seat used the same bare `^SEVERITY:` pattern, so
    a seat that wrote bolded headers and got cut off was reported as 0 findings."""
    out = "**SEVERITY:** high\nfirst\n\n  - SEVERITY : low\nsecond, cut off mid-sen"
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("truncated", 2), (status, findings)


def test_a_clean_review_of_zero_is_still_a_valid_result():
    """`AUDIT COMPLETE - 0 findings` legitimately has no SEVERITY lines — that is a seat
    that looked and found nothing. Flagging it would make every clean review look broken."""
    out = "I reviewed the diff and found no real defects.\n\nAUDIT COMPLETE - 0 findings\n"
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("ok", 0), (status, findings)


def test_a_positive_claim_backed_by_findings_is_untouched():
    """Negative control for the guard: the ordinary healthy path must not regress."""
    out = ("SEVERITY: high\nFILE: a.py:1\nWHAT: boom\n\n"
           "SEVERITY: low\nFILE: b.py:2\nWHAT: meh\n\nAUDIT COMPLETE - 2 findings\n")
    status, findings, _ = classify_seat(out, timed_out=False, failed=False)
    assert (status, findings) == ("ok", 2), (status, findings)


def test_completely_empty_content_is_a_dead_seat():
    """The reasoning-burn shape: HTTP 200, nothing in content. There is no report to
    truncate — findings=None, status empty."""
    status, findings, reason = classify_seat("", timed_out=False, failed=False)
    assert (status, findings) == ("empty", None), (status, findings)
    assert "no content" in reason


def test_an_empty_seat_does_not_count_toward_the_panel():
    """`reported` in main() filters on findings is not None, so an empty seat must fall
    out of the quorum the same way a rate-limited one does — otherwise a two-seat panel
    with one fabricated count still prints as two seats having reviewed the code."""
    results = [{"findings": None, "status": "empty"}, {"findings": 3, "status": "ok"}]
    reported = [r for r in results if r["findings"] is not None]
    assert len(reported) == 1, "an empty seat was counted as having reviewed the code"


# --- the secret gate must not match its own source --------------------------------------
# Written as one flat literal, SECRET_RE contained the very strings it hunts for, so any
# review whose payload included the classifier refused with "looks like a live
# credential". The file most worth reviewing was the only one that could never be sent.

_ORIGINAL_SECRET_PATTERN = (
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{10,}"
    r"|ghp_[A-Za-z0-9]{20,}|" + "sk" + r"-[A-Za-z0-9]{20,}|" + "BEGIN"
    + r" [A-Z ]*PRIVATE KEY|" + "-" * 5 + "BEGIN"
)


def test_secret_pattern_is_unchanged_by_the_fragmenting():
    """The fragments must concatenate to exactly the original — this is a security gate,
    and 'it still looks right' is not good enough for one."""
    assert SECRET_RE.pattern == _ORIGINAL_SECRET_PATTERN


def test_the_gate_still_catches_every_credential_shape():
    for sample in ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                   "GOCSPX-abcdefghij1234",
                   "ghp_abcdefghijklmnopqrstuvwxyz012345",
                   "sk" + "-abcdefghijklmnopqrstuvwxyz0123",
                   "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5):
        assert SECRET_RE.search(sample), sample


def test_the_gate_does_not_fire_on_ordinary_content():
    for sample in ("harmless text", "deepseek/deepseek-chat", "llama3.1",
                   "model_info: {id: family-chat}", "a normal sentence about tokens"):
        assert not SECRET_RE.search(sample), sample


def test_classifier_source_does_not_trip_its_own_gate():
    src = pathlib.Path(classify.__file__).read_text(encoding="utf-8")
    hit = SECRET_RE.search(src)
    assert hit is None, f"classify.py self-matches at {hit.group(0)!r}"


# ---- severity extraction -------------------------------------------------------------

def test_split_findings_numbers_in_report_order_and_reads_severity():
    """The numbering is load-bearing: a human reads finding n in a rendered report and
    then runs `verdict <run-id> <family> n`. If these disagree, verdicts land on the
    wrong finding and the false-positive rate the panel reports about itself is wrong."""
    report = ("Some preamble the seat wrote before starting.\n\n"
              "SEVERITY: low\nFILE: a.py:1\nWHAT: first\n\n"
              "SEVERITY: critical\nFILE: b.py:2\nWHAT: second\n\n"
              "SEVERITY: medium\nFILE: c.py:3\nWHAT: third\n\n"
              "AUDIT COMPLETE - 3 findings\n")
    found = split_findings(report)
    assert [f["n"] for f in found] == [1, 2, 3]
    assert [f["severity"] for f in found] == ["low", "critical", "medium"]
    assert "first" in found[0]["text"] and "second" in found[1]["text"]
    assert "preamble" not in found[0]["text"], "preamble must not be folded into finding 1"
    assert "AUDIT COMPLETE" not in found[-1]["text"], "terminator must not survive"


def test_split_findings_tolerates_bolded_and_bulleted_headers():
    """Markdown tolerance is inherited from SEVERITY_RE on purpose. A seat that bolds its
    headers must not silently produce zero findings."""
    report = ("**SEVERITY:** high\nFILE: a.py:1\nWHAT: bolded\n\n"
              "- SEVERITY : Medium\nFILE: b.py:2\nWHAT: bulleted and spaced\n\n"
              "AUDIT COMPLETE - 2 findings\n")
    found = split_findings(report)
    assert [f["severity"] for f in found] == ["high", "medium"], found
    assert max_severity(report) == "high"


def test_max_severity_is_none_when_no_finding_carries_a_level():
    """None means 'nothing labelled', NOT 'clean'. A gate must treat it as unknown."""
    assert max_severity("AUDIT COMPLETE - 0 findings\n") is None
    unlabelled = "SEVERITY: unknown-word\nFILE: a.py:1\nWHAT: x\n\nAUDIT COMPLETE - 1 findings\n"
    assert split_findings(unlabelled)[0]["severity"] is None
    assert max_severity(unlabelled) is None


def test_max_severity_ignores_severity_words_in_prose():
    """'this is a critical path' in a finding body must not promote the run's severity."""
    report = ("SEVERITY: low\nFILE: a.py:1\n"
              "WHAT: touches a critical path and a high-traffic route\n\n"
              "AUDIT COMPLETE - 1 findings\n")
    assert max_severity(report) == "low", "a prose mention was read as a header value"


def test_max_severity_orders_by_level_not_by_position():
    report = ("SEVERITY: critical\nFILE: a.py:1\nWHAT: worst\n\n"
              "SEVERITY: low\nFILE: b.py:2\nWHAT: least\n\n"
              "AUDIT COMPLETE - 2 findings\n")
    assert max_severity(report) == "critical"


def test_severity_value_re_is_built_from_the_presence_pattern():
    """If these two ever diverge, a seat's header style would count for one and not the
    other: findings inferred but no severity, or the reverse."""
    assert SEVERITY_VALUE_RE.pattern.startswith(SEVERITY_RE.pattern)


# ---- regressions from the 2026-09-08 dogfood panel -----------------------------------

@pytest.mark.parametrize("header,expected", [
    ("**SEVERITY:** — High", "high"),          # em dash between colon and level
    ("SEVERITY: `high`", "high"),              # backticked level
    ("SEVERITY: - High", "high"),              # bulleted level
    ("**SEVERITY:** High", "high"),
    ("- SEVERITY: Medium", "medium"),
    ("**SEVERITY**: critical", "critical"),
])
def test_severity_value_tolerates_the_decoration_seats_actually_emit(header, expected):
    """Confirmed panel finding (glm, 2026-09-08). These variants all satisfy SEVERITY_RE,
    so they were COUNTED as findings, while the value pattern read them as unlabelled. A
    seat that labelled every finding reported as fully unlabelled, and a caller gating on
    severity had nothing to gate on."""
    body = f"{header}\nFILE: a.py:1\nWHAT: x\n"
    assert SEVERITY_RE.search(body), "premise: this header is counted as a finding"
    assert split_findings(body)[0]["severity"] == expected
    assert max_severity(body) == expected


def test_prose_after_the_colon_is_still_not_a_severity():
    """The widened gap must not start matching sentences. Letters block it."""
    body = "SEVERITY: not applicable, low priority\nFILE: a.py:1\nWHAT: x\n"
    assert split_findings(body)[0]["severity"] is None


def test_a_severity_line_inside_a_fenced_block_is_not_a_finding():
    """Confirmed panel finding (glm, 2026-09-08). A seat writing a FIX often quotes the
    report format itself. Measured: one real `low` finding whose FIX block quoted a
    `critical` header split into two findings and reported max_severity `critical` — a CI
    gate keyed on that blocks a change on the strength of a code sample."""
    report = ("SEVERITY: low\nFILE: a.py:1\nWHAT: the expected format is\n\n"
              "```\nSEVERITY: critical\nFILE: x.py:1\n```\n\n"
              "AUDIT COMPLETE - 1 findings\n")
    found = split_findings(report)
    assert len(found) == 1, f"the fenced header started a phantom finding: {found}"
    assert max_severity(report) == "low", "a quoted level inflated the run's severity"
    assert "```" in found[0]["text"], (
        "masking must only affect the boundary search — the finding keeps its code block")


def test_tilde_fences_are_masked_too():
    report = ("SEVERITY: low\nFILE: a.py:1\nWHAT: x\n\n"
              "~~~\nSEVERITY: critical\n~~~\n\nAUDIT COMPLETE - 1 findings\n")
    assert len(split_findings(report)) == 1
    assert max_severity(report) == "low"


def test_an_unterminated_fence_still_masks_to_the_end():
    """A truncated seat can be cut off mid-fence. The dangling block must not spray
    phantom findings into the inferred count."""
    report = ("SEVERITY: low\nFILE: a.py:1\nWHAT: x\n\n```\nSEVERITY: critical\n")
    assert len(split_findings(report)) == 1
    assert max_severity(report) == "low"


def test_inferred_truncated_count_agrees_with_split_findings():
    """These two must not disagree: the count bounds `verdict <n>`, and split_findings
    produces the numbers a human reads. A quoted header counted in one and not the other
    makes a valid verdict look out of range."""
    report = ("SEVERITY: low\nFILE: a.py:1\nWHAT: x\n\n"
              "```\nSEVERITY: critical\n```\n\n"
              "SEVERITY: high\nFILE: b.py:2\nWHAT: y\n")     # no AUDIT COMPLETE: truncated
    status, count, _ = classify_seat(report, timed_out=False, failed=False)
    assert status == "truncated"
    assert count == len(split_findings(report)) == 2, (status, count)
