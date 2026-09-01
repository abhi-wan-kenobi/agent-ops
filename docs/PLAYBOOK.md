# The playbook

How to run an adversarial review panel and read its output without being had by it.

Every rule below was paid for by a real failure. They are ordered by how much damage the
failure caused.

---

## 1. A clean exit is not a clean audit

A run that exits 0 having reviewed nothing looks exactly like a run that reviewed everything
and found nothing. This has happened for boring reasons: an argument list too long for the
shell, a misconfigured endpoint returning 400 for every seat, a lane that was silently
unreachable.

**Require positive evidence of work.** A report must state what it read — file count, byte
count, seat names — and a run that cannot state that is a failed run, not a passing one.

## 2. An empty body is a dead seat, never agreement

Reasoning-heavy models can spend their entire output budget in the reasoning channel and emit
almost nothing as content. The observed shape is a terminator line —
`AUDIT COMPLETE - 7 findings` — with no findings written down. In one measured sample this
happened in 5 of 23 runs (22%) from a single model.

That line satisfies a naive liveness check, so the run records success with a confident
count. It is worse than a seat that dies loudly.

**A bare count with no findings must classify as a dead seat.** And note the trap in the
obvious fix: keying on a specific header (`^SEVERITY:`) discards real reports from models
that format differently (`**SEVERITY:**`, or no such token at all). Silence should require
**both** a short body **and** no recognisable finding structure.

## 3. Heavy-reasoning models are for writing code, not reviewing it

The same property that makes a model good at planning — long internal deliberation — makes it
a poor panel seat, because the budget goes to thinking rather than reporting. Choose seats
for output discipline, not raw capability.

## 4. Never let a family review its own family's work

Exclude the model family that wrote the code, mechanically, not by convention. A model
grading its own family's output is not an independent reviewer, and the bias is invisible in
the report.

This is what makes family diversity across seats load-bearing rather than cosmetic: with one
vendor, exclusion leaves you with nothing.

## 5. Roughly a third of findings are false positives

Not because the models are bad — because they cannot see the layer that already handles the
case. **Reproduce every finding against the real code before acting on it.** The reviewer's
confidence carries no information about whether it is right.

## 6. Two models agreeing is high confidence, not proof

Corroboration is real signal and worth weighting. But independent seats fed the *same* scoped
payload share the same blind spot, so they can agree confidently and both be wrong for
identical reasons.

Documented case: two seats independently rated a function dead code at critical severity. The
review was scoped to a diff, neither could see the callers, and the function was live.

**A scoped review cannot support a "this is unused" claim.** Treat every dead-code finding
from a scoped run as unproven until checked against the whole repository.

## 7. A right finding can carry a wrong fix

Verify the remedy separately from the diagnosis. Recorded case: a seat correctly identified a
schema bug and proposed a validation rule that would have rejected every real request. The
finding was kept; the fix was discarded.

Another: a proposed "wrap the import in try/except" would have converted a loud failure into a
silent one, inverting the system's core principle. The severity rating said nothing about
whether the suggested change was safe.

## 8. TRUNCATED means partial, not clean

A report that ran out of budget mid-write is a **partial** audit. It is not evidence of
absence. Re-run it narrower rather than accepting it.

## 9. Don't stack panels

Concurrency against a rate-limited endpoint does not fail cleanly — it **queues**, and the
tail grows until a seat exceeds its budget and reports an incomplete result that looks like a
finding-free one. Measured on one lane: 1 run took ~4s, 3 runs ~6s, 6 runs ~42s.

Take a lease. Derive the concurrency cap from panel size, not from a guess.

## 10. Close the loop

For every confirmed finding:

1. Fix it.
2. Add a regression test that names the finding.
3. Re-verify — including a negative check that the test fails without the fix.
4. Report what was confirmed, what was a false positive **and why**, and what changed.

Step 4 is the one people skip. A panel that only ever reports findings trains you to trust it
uniformly; a panel that reports its own false-positive rate trains you to read it correctly.

---

## The one-line version

The panel is a source of leads, not verdicts. Its output is worth exactly as much as the
verification you do afterwards — and its failure modes are mostly **silent**, so absence of a
finding is never, by itself, evidence of correctness.
