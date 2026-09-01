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

# Assembled from fragments so this FILE does not itself contain the literals it hunts for.
# Written flat, the gate tripped on its own source: any review whose payload included this
# file refused with "looks like a live credential", so the file that most wants reviewing
# was the one that could never be sent. The fragments concatenate to exactly the compiled
# pattern, and a test pins that.
_SECRET_PATTERNS = (
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",   # JWT
    r"GOCSPX-[A-Za-z0-9_-]{10,}",                   # Google OAuth client secret
    r"ghp_[A-Za-z0-9]{20,}",                        # GitHub PAT
    "sk" + r"-[A-Za-z0-9]{20,}",                    # OpenAI/Anthropic-style key
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
        # transport error and not truncation of a report — there is no report.
        return "empty", None, "no content at all (output budget likely spent in reasoning)"
    return "truncated", len(SEVERITY_RE.findall(out)), ""
