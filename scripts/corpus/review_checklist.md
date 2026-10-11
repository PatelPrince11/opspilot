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

From batch 8 review (A/B/C):
- No invented check results, mid-incident check times, outside reports or rationale for the severity.
  A step nobody recorded doesn't get a clock time.
- No numeric thresholds in log lines ("back under 200ms", "cpu 94%") that no source gives.
- A runbook's remedy is cited as the runbook's in one clause, without its steps (e.g. not "restore,
  bump the serial, push to both").
- A log ramp respects the ceiling that triggers the alert or error (in-flight caps, pool sizes).
- Pacing: before 2026-08-20 the rollout tool waits 5 min between hosts; a per-host line that also
  ends the run implies there was no next host. Don't let logs imply what prose may not say.
- A client-side cause (device clock) fails against every pod behind an even load balancer; name the
  pod only as where it was logged.
- Stock idioms spread across a batch are overlap even under Jaccard: "alert on X so the page ...",
  "What is not known", "for whoever picks this up", "summary for the channel", "not recorded" said
  as commentary. Give each idiom to one document at most.
- Twin documents (same service and cause) don't share their closing list or their argument order.
- A history reference may only name events before the incident (validator now checks "Month YYYY").
