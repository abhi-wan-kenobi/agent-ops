"""Judge one seat's output: did it review anything, and what did it claim?

The distinction that matters is between a seat that ANSWERED and one that never ran. A
seat that did not run has findings=None, never 0: zero means "looked and found nothing",
which is precisely the claim it is not entitled to make. Both used to print identically as
"0 findings", so a dead panel read as seats agreeing there was nothing wrong.
"""
from __future__ import annotations

import re

FINDINGS_RE = re.compile(r"AUDIT COMPLETE\s*[-—]\s*(\d+)[^\n]*", re.I)

# Below this, a body that claims findings is the terminator line and nothing else. The
# real failures measured left 0-5 characters once the terminator was removed; the shortest
# genuine report on record is several thousand. 200 sits far from both.
EMPTY_BODY_CHARS = 200

# Markdown-tolerant: seats bold the header (`**SEVERITY:** high`), indent it, or bullet
# it. A bare `^SEVERITY:` under-counted every such report — an inferred count on a
# truncated seat could read as 0 when the seat had written findings.
SEVERITY_RE = re.compile(r"^[ \t>*_#-]*\**\s*SEVERITY\s*\**\s*:", re.M | re.I)

# Ascending. A CI gate and a rendered report both need "how bad is the worst of these",
# and until now nothing anywhere read a severity VALUE — SEVERITY_RE was used only for
# presence and counting.
SEVERITY_LEVELS = ("low", "medium", "high", "critical")

# Built from SEVERITY_RE's own pattern so markdown tolerance is inherited rather than
# reimplemented: a seat that bolds or bullets the header must parse the same both ways.
#
# The gap before the level accepts the decoration seats actually emit between the colon
# and the word. Measured against a real panel (2026-09-08): `**SEVERITY:** — High`,
# ``SEVERITY: `high` `` and `SEVERITY: - High` all satisfied SEVERITY_RE — so they were
# COUNTED as findings — while a narrower gap read their severity as None. A seat that
# labelled every finding then reported as unlabelled, and a caller gating on severity had
# nothing to gate on. Letters are still excluded from the gap, so prose after the colon
# ("SEVERITY: not applicable, low priority") does not match.
_SEV_GAP = r"[\s*_`'\"\-\u2013\u2014:>\[\(]*"
SEVERITY_VALUE_RE = re.compile(
    SEVERITY_RE.pattern + _SEV_GAP + r"(" + "|".join(SEVERITY_LEVELS) + r")\b",
    re.M | re.I)

_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)


def _mask_fences(out: str) -> str:
    """Blank out fenced code blocks, preserving length so offsets stay valid.

    A seat writing a FIX often shows the report format itself, and a fenced
    `SEVERITY: critical` line satisfies SEVERITY_RE. Measured (2026-09-08): a report whose
    single real finding was `low` but whose FIX block quoted a `critical` header split
    into TWO findings and reported max_severity `critical`. A CI gate keyed on that blocks
    a change on the strength of a code sample — a silent wrong answer, no crash, no log.

    Masking rather than deleting: the caller slices the ORIGINAL text by these offsets, so
    each finding keeps its code blocks intact. Only the BOUNDARY search is masked.
    """
    return _FENCE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), out)


def split_findings(out: str) -> list[dict]:
    """Split a seat report into numbered findings, in the order they were written.

    Numbering must match what `agent_ops verdict <run-id> <family> <n>` expects, because a
    human reads finding n in a rendered report and then records a judgement against it. So
    this counts from the first SEVERITY header, top to bottom, and drops the preamble
    before it — the same order the seat's own markdown file presents.

    Headers inside fenced code blocks do not start a finding (see _mask_fences). The
    prompt asks every finding to LEAD with its SEVERITY line; a seat that trails it
    instead will have its text attributed to the previous finding, which is a known limit
    of splitting on the only marker the format guarantees.
    """
    masked = _mask_fences(out)
    starts = [m.start() for m in SEVERITY_RE.finditer(masked)]
    if not starts:
        return []
    findings: list[dict] = []
    bounds = starts + [len(out)]
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), start=1):
        text = FINDINGS_RE.sub("", out[a:b]).strip()
        sev = SEVERITY_VALUE_RE.search(text)
        findings.append({"n": i,
                         "severity": sev.group(1).lower() if sev else None,
                         "text": text})
    return findings


def max_severity(out: str) -> str | None:
    """The worst severity a seat actually labelled, or None if it labelled none.

    None is not "clean" — it means no finding carried a recognisable level. A caller that
    gates on severity must treat None as unknown, never as safe.
    """
    found = {f["severity"] for f in split_findings(out)} - {None}
    for level in reversed(SEVERITY_LEVELS):
        if level in found:
            return level
    return None


# Assembled from fragments so this FILE does not itself contain the literals it hunts for.
# Written flat, the gate tripped on its own source: any review whose payload included this
# file refused with "looks like a live credential", so the file that most wants reviewing
# was the one that could never be sent. The fragments concatenate to exactly the compiled
# pattern, and a test pins that.
_SECRET_PATTERNS = (
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",   # JWT
    r"GOCSPX-[A-Za-z0-9_-]{10,}",                   # Google OAuth client secret
    r"ghp_[A-Za-z0-9]{20,}",                        # GitHub PAT
    # The two key shapes this tool's own README tells the user to export both contain
    # hyphens inside the prefix, so the generic pattern below stops after three
    # characters and never fires. Named explicitly rather than by loosening the generic
    # one to [A-Za-z0-9_-]: unanchored, that would match "risk-management-strategies"
    # and refuse to review ordinary prose. Found 2026-09-08.
    "sk" + r"-or-v1-[A-Za-z0-9]{20,}",              # OpenRouter
    "sk" + r"-ant-[A-Za-z0-9_-]{20,}",              # Anthropic
    "sk" + r"-[A-Za-z0-9]{20,}",                    # OpenAI-style key
    "BEGIN" + r" [A-Z ]*PRIVATE KEY",
    "-" * 5 + "BEGIN",                              # any PEM armour
)
SECRET_RE = re.compile("|".join(_SECRET_PATTERNS))


def classify_seat(out: str, timed_out: bool, failed: bool,
                  reason: str = "") -> tuple[str, int | None, str]:
    """Return (status, findings, reason) for one seat's content.

    `failed` means the transport reported an error; `reason` is that error's own message
    (the provider layer hands it over structured — nothing is scraped from stdout).
    A real report wins over a transport complaint: a complete audit that arrived alongside
    a cosmetic warning must not be discarded.

    Statuses: ok | truncated | error | timeout | empty.
    """
    m = FINDINGS_RE.search(out)
    if m:
        claimed = int(m.group(1))
        # A count with nothing behind it is the worst output this tool can produce: the
        # terminator line alone satisfies FINDINGS_RE, so a seat that wrote NOTHING used to
        # sail through as `status: ok` with a confident non-zero count. Measured on a real
        # lane: one model did this in 22% of runs, once claiming "17 findings" in a 29-char
        # body.
        #
        # Deliberately narrower than the truncation check below: `AUDIT COMPLETE - 0
        # findings` with no body is a CLEAN review and must stay one; only a POSITIVE claim
        # with nothing behind it is self-contradictory.
        #
        # ⚠️ Two independent rescues, because either signal alone produced false positives
        # on real runs: keying on `^SEVERITY:` alone discarded two good reports (one seat
        # bolds the header, another emits no SEVERITY token at all and heads findings its
        # own way), and length alone flagged terse-but-real reports. A body is only
        # "nothing" when it is BOTH too short to contain a review AND carries no finding
        # header. Erring toward letting a report through is the correct direction — a false
        # "empty" silently discards a real review, the very failure this guard prevents.
        body = FINDINGS_RE.sub("", out).strip()
        if claimed > 0 and len(body) < EMPTY_BODY_CHARS and not SEVERITY_RE.search(body):
            return ("empty", None,
                    f"claimed {claimed} findings in a {len(body)}-char body")
        return "ok", claimed, ""
    if timed_out:
        return "timeout", None, "no output before the timeout"
    if failed:
        return "error", None, (reason or "seat exited without a report")
    if not out.strip():
        # The reasoning-burn shape: HTTP 200, finish=length, and nothing in content. Not a
        # transport error and not truncation of a report — there is no report. By recorded
        # decision (playbook rule 3), text left in the reasoning channel is never rescued:
        # content is the contract, and grading unaddressed deliberation would reward the
        # indiscipline this classification screens for.
        return "empty", None, "no content at all (output budget likely spent in reasoning)"
    # Masked for the same reason split_findings masks: a quoted SEVERITY line in a FIX
    # block is not a finding, and this count is what bounds `verdict <n>`. The two must
    # not disagree about how many findings a seat wrote.
    return "truncated", len(SEVERITY_RE.findall(_mask_fences(out))), ""
