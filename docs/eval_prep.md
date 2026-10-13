# Eval prep notes for Phase 1 step 4

A compilation of what NOTES.md (batches 5 to 9, the 2026-10-09 entries, the batch 7 cleanup and the
scope cut) and `eval/corpus/incidents_sidecar.toml` already say. This file adds no rules: every line
traces back to one of those sources, or to a count taken from `data/processed/incidents.jsonl` where
noted. Gold sets may cite only the 117 written incidents and the 30 runbooks (scope cut).

## (a) Twin pairs, with what tells them apart

Questions about a twin must carry the distinguishing detail (date, host, service or code).

From batch 6 and the 2026-10-09 entry:
- 0001, 0011, 0079, 0089, 0109, 0127, 0149, 0183 (`/var` journal rows): 8 equally good answers to a
  question that names no host; the question must name the host or date.
- 0025 vs RB-020's March 2026 incident: March 2025, orders-api, internal-dns-01 vs March 2026,
  inventory-service, internal-dns-02. The question must name the year, the server or the service.

From batch 7:
- 0007/0023: client clock on edge-gateway-03, 2025-01-19 vs missed pod edge-gateway-01, 2025-03-16
- 0125/0016: internal-dns-01 not answering, VMs, 2026 vs cluster CoreDNS CPU, pods, 2025
- 0125/0025: no answer vs NXDOMAIN; kafka-02 vs orders-api; 2026 vs 2025
- 0125/0142: internal-dns-01 silent vs zone file on internal-dns-02
- 0142/0016: zone file on internal-dns-02 vs cluster CoreDNS CPU
- 0142/0025: internal-dns-02, inventory-service, 2026 vs internal-dns-01, orders-api, 2025
- 0162/0048: CPX-W429 retry storm, sev1, 2026-05-26 vs a 5-minute blip in 2025
- 0177/0048: CPX-W430 during a rollout restart vs CPX-W429 blip
- 0162/0177: CPX-W429 + ORD-5003 vs CPX-W430 only
- 0162/0197: processor slow, CPX-W429 vs node cordon, CPX-W430
- 0177/0197: 5-min rollout restart vs 42-min cordon
- 0156/0055: payments-api slow queries, 2026 vs PgBouncer export, 2025
- 0059/0151: missing index, INV-E220, 2025 vs pool 20→80, 53300, 2026
- 0070/0089: kafka-02 GC logs vs kafka-01 journal
- 0070/0149: kafka-02 GC logs vs kafka-03 journal
- 0070/0120: `/var` vs the data disk
- 0070/0101: both retell RB-008; kafka vs search-cluster
- 0120/0089, 0120/0149: kafka-03 data disk, 2026-01 vs `/var` journal rows
- 0078/0152: overcommit 2, MISCONF, 2025-09 vs a 2026-04 row
- 0084/0157/0174: `cdc_payments` slot, 2025-09 vs archiving failure, ENOSPC, 2026-05 vs
  `payments_db_02` slot, 2026-07
- 0087/0099: planned failover, auth-service-01, 2025-10-07 vs partitioned primary, auth-service-02,
  2025-11-12
- 0108/0094: VM lost, catalog-db-01 CPU, 2025-12-09 vs 2025-10-26 row
- 0128/0133: catalog-api-02 every 50 min vs notification-worker-01 Monday batch

From batch 8:
- 0081/0033/0038 and 0025/0142 (zone push NXDOMAIN): 0033 auth-service-01, internal-dns-02,
  2025-04-21; 0038 ledger-service-01, internal-dns-01, 2025-05-02; 0081 catalog-api-02,
  internal-dns-02, 2025-09-19; 0025 orders-api-02, internal-dns-01, 2025-03-19; 0142
  inventory-service, internal-dns-02, 2026-03-24
- 0068/0086/0061 and 0007/0023 (edge TLS): intermediate expired, all pods, sev1, 2025-08-07 vs
  intermediate missing, 2025-10-06 vs client clock on edge-gateway-01, 2025-07-17 vs client clock,
  edge-gateway-03, 2025-01-19 vs missed pod, 2025-03-16
- 0110/0004: processor-side CA rotation, 2025-12-19 vs our client certificate expired, 2025-01-14
- 0073/0031: auth-db certificate name mismatch, 2025-08-21 vs renewed but unloaded, 2025-04-08
- 0186/0195/0048/0162/0177/0197: CPX-W430 from both pods (traffic above 360), 2026-08-13 vs CPX-W429
  blip 2026-09-12 vs 2025 blip vs retry storm vs rollout restart vs cordon
- 0167/0137: crawler at 250/pod, 2026-06-13 vs campaign at 100/pod, 2026-03-10
- 0119/0060: edge-gateway-03 node pressure, 2026-01-16 vs the earlier edge latency row
- 0155/0152 and 0078/0123: redis-features maxmemory 2026-05-03 vs 2026-04-26; overcommit 2025-09;
  LOADING after a restart 2026-01-25
- 0095/0055/0156: payments PgBouncer long transactions with query_wait_timeout, 2025-10-27 vs refunds
  export, 2025-06-24 vs pgx slow queries, 2026
- 0077/0124: ledger Hikari slow queries, 2025-09-08 vs month-end reconciliation, 2026-01-31
- 0022/0015: processor latency, 2025-03-15 vs the earlier processor-latency row
- 0145/0107: search-cluster-02 segment merges vs 0107's row (same shape, different cause)

From batch 9:
- 0043/0086: intermediate missing on all three edge pods, sev1, 2025-05-15 vs sev3, 2025-10-06
- 0082/0007/0061: client clock, edge-gateway-03 2025-09-21 vs edge-gateway-03 2025-01-19 vs
  edge-gateway-01 2025-07-17. 0082/0007 share host and cause: questions must name the date.
- 0126/0073: auth-db name mismatch, sev2 2026-02-08 vs sev3 2025-08-21
- 0035/0156/0055/0095: payments pool, 2025-04-24 slow queries on -03/-04 vs 2026 slow queries vs
  PgBouncer export vs long transactions with query_wait_timeout
- 0083/0035: release regression filling pools on -01/-03, 2025-09-23 vs slow queries on -03/-04
- 0136/0077/0124: ledger Hikari slow queries, ledger-service-02 2026-03-08 vs -01 2025-09-08 vs
  month-end
- 0182/0134: orders-api pool, 2026-08-08 -02/-03 vs month-end order summaries 2026-02-28 -01/-03
- 0169/0184/0155/0152: redis-features maxmemory, 2026-06-17 vs 2026-08-11 vs 2026-05-03 vs 2026-04-26
- 0146/0014/0028: search-cluster pool rejections, campaign 2026-04-15 vs 2025-02-19 (-01/-03) vs
  2025-03-26 (-02/-03)
- 0187/0107: search-cluster GC pauses vs 0107's row
- 0159 and the six card-processor-proxy rate rows (0048, 0162, 0177, 0186, 0195, 0197): a 10-minute
  CPX-W429 blip on 2026-05-19, no PAYAPI-E1107
- 0179/0196: ndots search-list latency, notification-worker 2026-07-23 vs catalog-api 2026-09-15
- 0076 and the internal-dns rows: SERVFAIL (zone not loaded) on internal-dns-01, 2025-09-02
- 0054/0108/0094: planned redis-cache failover 2025-06-19 vs VM loss 2025-12-09 vs 0094's row
- 0165/0093: ledger-service-01 month-end serialization CPU 2026-06-01 vs retry spin 2025-10
- 0006/0133: notification-worker-01 template cache 2025-01-18 vs Monday batch 2026-02
- 0037/0166: orders-api CPU-bound event loop, -02/-03 2025-04-29 vs crypto on -01 2026-06-09
- 0069/0039: OOMKilled, orders-api-03 Node heap vs auth-service-01 goroutines

From the sidecar (`hard_negative_pair`, with its distinguishing token):
- 0016 RB-020/RB-022 (`replicas`); 0025, 0033, 0038, 0081 RB-020/RB-027 (`Type=simple`);
  0042 RB-012/RB-028 (`lock_timeout`); 0055 RB-010/RB-011 (`cl_waiting`); 0124 RB-001/RB-013
  (`hikaricp_connections_pending`); 0137 RB-014/RB-030 (`EDGE-RL01`); 0162 RB-014/RB-016
  (`CPX_MAX_ATTEMPTS`); 0168 RB-020/RB-021 (`idleTimeoutMillis`); 0177 RB-023/RB-029
  (`rollout restart`); 0178 RB-018/RB-026 (`sess:`); 0197 RB-016/RB-029 (`CPX-W430`)

## (b) Write-ups that retell a runbook's history (gold set lists both)

- Batch 5: 0185/RB-027, 0168/RB-021, 0055/RB-011, 0023/RB-025 (the batch 5 Jaccard root cause: "those
  runbooks tell the same events")
- Batch 6: 0016/RB-022, 0024/RB-002, 0031/RB-024, 0178/RB-026, 0197/RB-029 (same times as the
  runbook)
- Batch 7: 0004/RB-023, 0042/RB-028, 0059/RB-015, 0070/RB-008, 0078/RB-019, 0084/RB-007,
  0157/RB-007, 0174/RB-007, 0087/RB-018, 0093/RB-001, 0108/RB-017, 0120/RB-009, 0124/RB-013,
  0128/RB-005, 0133/RB-005, 0137/RB-030, 0142/RB-020, 0151/RB-012, 0162/RB-016
- Batch 7 twin list: 0101/RB-008 (with 0070, both retell it)
- Batches 8 and 9: none

## (c) Post-dated runbook labels (in `runbook_ids` as a relevance label, never named in the write-up)

- INC-0004 → RB-023
- INC-0042 → RB-028
- INC-0124 → RB-013
- INC-0137 → RB-030
- INC-0162 → RB-016
- INC-0168 → RB-021

Matches `postdated()` over the 117 written rows. Batches 8 and 9 have none.

## (d) Unanswerable resolutions (no question may ask what fixed them)

- Batch 7: 0060, 0107, 0125, 0128
- Batch 8: every row except 0103 and 0134: 0005, 0022, 0028, 0033, 0038, 0046, 0049, 0061, 0065,
  0068, 0073, 0074, 0077, 0081, 0086, 0091, 0095, 0110, 0119, 0123, 0145, 0155, 0167, 0186, 0195
- Batch 9: all 27: 0006, 0008, 0014, 0035, 0037, 0039, 0043, 0054, 0069, 0076, 0082, 0083, 0088,
  0112, 0117, 0126, 0136, 0146, 0159, 0165, 0166, 0169, 0179, 0182, 0184, 0187, 0196
- Batches 5 and 6: NOTES records none.

## (e) Short documents under 170 words (host, code or date lookup questions only)

Word counts from `incidents.jsonl`; all are briefs.
- In NOTES: 0107 (117), 0125 (117), 0133 (125) (batch 7); 0061 (147), 0065 (136), 0145 (108)
  (batch 8); 0014 (106), 0037 (117), 0117 (164) (batch 9)
- Under 170 words but not listed in NOTES: 0011 (135), 0015 (128), 0048 (110), 0152 (126)

## (f) Tokens kept out or paraphrased

- 0142: sidecar `missing_record` inventory-db-01.prod.internal is paraphrased ("the inventory database
  record"); no question may use the hostname.
- 0093: no "4.12" (anachronism); backoff appears only as an undated follow-up.
- 0042 (`lock_timeout`), 0137 (250 per pod): grace-window tokens, only as follow-ups.
- 0177: `CPX-E600` was replaced by `rollout restart` in the sidecar; the old token is in no document.
- 0172: the channel name `#risk-releases` was removed.
- 0152: the OOM text appears only as the catalog code.
- 0099: no "split brain"; no Sentinel log lines.
- 0025, 0033, 0038, 0081, 0142: the bad zone file is named on the row's one internal-dns host; the
  other server is never named.
- 0025, 0033, 0038, 0081: `Type=simple` appears only as RB-027's statement about the `kafka` unit.
- 0054: redis-cache's primary history is unstated; `SENTINEL FAILOVER <master name>` is cited as the
  documented way, not as what was typed.
- 0179, 0196: no resolver library, no 8-query figure, no retry counts.
- 0171: the provider status-page statement was removed (2026-10-11).

## (g) Narrative colour no question may rely on

- All 117: follow-ups, owner names, confirmations (such as a vendor's report) and reported-by details
  (batch 5 rule).
- 0025: the 15:26 page, the 15:31 check and "the diff of the 15:15 push showed the record removed".
- 0073 (team-dba), 0110 (team-payments): the linked runbook's owner named as the team that took the
  incident.
- 0128: who took it (catalog on-call).
- Batch 9 (all 27, ids in (d)): who took each incident, a runbook's owner team or "the on-call".

## (h) Counts per cause type and severity (117 written)

Cause type is the row's first `failure_types` entry; matches the scope-cut table.

| Cause type | Count | Incidents |
|---|---|---|
| api_latency | 12 | 0015 0022 0049 0060 0065 0074 0088 0107 0119 0166 0171 0187 |
| bad_deploy | 11 | 0042 0059 0083 0096 0112 0117 0151 0172 0178 0185 0194 |
| conn_pool_exhaustion | 12 | 0035 0046 0055 0077 0091 0095 0103 0124 0134 0136 0156 0182 |
| disk_full | 14 | 0001 0011 0070 0079 0084 0089 0101 0109 0120 0127 0149 0157 0174 0183 |
| dns_failure | 11 | 0016 0025 0033 0038 0076 0081 0125 0142 0168 0179 0196 |
| high_cpu | 11 | 0005 0008 0024 0026 0030 0037 0067 0093 0145 0165 0189 |
| memory_leak | 11 | 0006 0013 0034 0039 0063 0069 0080 0097 0128 0133 0163 |
| rate_limiting | 12 | 0014 0028 0048 0137 0146 0159 0162 0167 0177 0186 0195 0197 |
| redis_down | 11 | 0054 0078 0087 0094 0099 0108 0123 0152 0155 0169 0184 |
| tls_expired | 12 | 0004 0007 0023 0031 0043 0061 0068 0073 0082 0086 0110 0126 |

| Severity | Count | Incidents |
|---|---|---|
| sev1 | 9 | 0004 0035 0043 0068 0096 0110 0162 0186 0197 |
| sev2 | 33 | 0008 0013 0016 0025 0031 0046 0049 0055 0067 0069 0076 0077 0080 0081 0083 0084 0088 0093 0095 0103 0108 0124 0126 0151 0155 0156 0157 0169 0174 0178 0182 0185 0194 |
| sev3 | 51 | 0001 0005 0015 0022 0023 0024 0026 0028 0030 0033 0034 0037 0038 0039 0042 0059 0060 0063 0070 0073 0074 0078 0086 0089 0091 0094 0097 0099 0101 0109 0117 0119 0120 0125 0127 0128 0134 0136 0137 0142 0146 0152 0159 0165 0166 0168 0172 0177 0184 0189 0196 |
| sev4 | 24 | 0006 0007 0011 0014 0048 0054 0061 0065 0079 0082 0087 0107 0112 0123 0133 0145 0149 0163 0167 0171 0179 0183 0187 0195 |
