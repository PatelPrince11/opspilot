# Review checklist (judgement-only findings; packet includes this file)

From batch 7 review (A/B/C), for writers and reviewers:
- Don't retell a runbook in its own order: its procedure steps, table rows, FAQ answers or decision
  tree walked in sequence with swapped words. Jaccard and 8-word checks don't catch this. Organise
  around the incident (impact, what was seen when, decisions, what changed) and cite the runbook once.
- A runbook written about this incident (label only) is not retold beyond the row's own facts.
- Cause before effect: a stated action ("restart a minute ago") must not come after the errors it
  explains started.
- A timeline entry uses only what was known at its own time (no end-time arithmetic in a mid-incident comment).
- Logs respect thresholds: values before an alert fires stay below its threshold.
- In-process state (caches, heaps) resets when the process is killed; log ramps must reset too.
- Owner/Team fields agree with who speaks in the document.
- No invented check results, causes or fixes. If the packet gives no fix, the resolution stays vague
  ("handed to team-X; errors stopped by HH:MM").
- No absolute claims ("every lookup", "never", "nobody").
- No distinctive heading or closing phrase shared with a twin or another write-up of the batch.
- Facts scoped to one object stay there (12 partitions is ledger.entries only).
- VM config pushes before 2026-08-20: hosts are about 5 min apart (fixed wait).
- A Deployment roll replaces every instance in turn; don't name one instance as the survivor of the whole roll.
- A sidecar string blocked by the host rule (e.g. a non-row hostname) is paraphrased; the coordinator records it.
