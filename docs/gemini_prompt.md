# Prompt for Gemini (paste everything below the line)

---

You are an on-call engineer at a company whose services are listed in the table below. For each of
the 40 rows, write one question you would type into an incident-investigation assistant while
looking at that incident. You have not read any documentation about these incidents.

## Writing rules

- Write as the on-call engineer: short, symptom-first, the way you'd type into a chat at 2am.
- Don't read the incident or runbook documents first. Work only from the row: imagine what you'd see.
- Never put the answer in the question (no root cause, no fix).
- Twins: when a row says "has a twin", include its date (e.g. "2025-09-21" or "September 2025"),
  so only this incident fits.
- "no fix question": don't ask what fixed or resolved it; ask what's going on, where to look, what
  changed, or whether it happened before.
- Mix: about 24 paraphrase, 8 exact-token, 8 multi-doc questions, as marked per row.
- "paraphrase: no code, no hostname": describe the situation in your own words. The question must
  contain no error code and no hostname (service names and dates are fine).
- "use this exact token": put that code or hostname in the question, exactly as written.
- "multi-doc: also RB-NNN": write a question whose answer needs both the incident and that runbook
  (e.g. what's happening plus where to look or what the runbook says to check).
- Stay inside the corpus: only the services, hosts and codes in the row. No other systems.
- Vary the openings: don't start every question the same way.
- One question per row, in row order.

Output format: exactly 40 lines and nothing else. Each line is the row number, a tab character, then
the question: `N<tab>question`. No header, no blank lines, no numbering other than N, no quotes, no
commentary before or after.

| # | Service | Symptom services | Date (UTC) | Failure type | Hosts | Codes | Notes |
|---|---|---|---|---|---|---|---|
| 1 | search-cluster | search-api | 2025-03-26 | rate_limiting | search-api-02, search-cluster-02, search-cluster-03 | SRCH-E502 | use this exact token: `SRCH-E502`; has a twin: include the date; no fix question |
| 2 | redis-features | fraud-scorer | 2026-05-03 | redis_down | fraud-scorer-01, fraud-scorer-02, redis-features-01 | OOM command not allowed, FRD-E140 | multi-doc: also RB-019; has a twin: include the date; no fix question |
| 3 | edge-gateway | - | 2025-01-19 | tls_expired | edge-gateway-03 | - | multi-doc: also RB-025; has a twin: include the date |
| 4 | ledger-service | - | 2025-04-21 | memory_leak | ledger-service-02 | OOMKilled | multi-doc: also RB-005 |
| 5 | fraud-scorer | payments-api | 2026-06-30 | bad_deploy, high_cpu | fraud-scorer-01, fraud-scorer-02 | PAYAPI-W0310 | multi-doc: also RB-002 |
| 6 | redis-features | fraud-scorer, payments-api | 2025-08-23 | api_latency | fraud-scorer-01, redis-features-01 | FRD-W118, PAYAPI-W0310 | use this exact token: `FRD-W118`; no fix question |
| 7 | payments-pgbouncer | payments-api | 2025-03-22 | high_cpu | payments-pgbouncer-01 | PAYAPI-E1042 | multi-doc: also RB-011 |
| 8 | orders-db | orders-api | 2025-11-22 | conn_pool_exhaustion | orders-db-01 | 53300, ORD-5003 | multi-doc: also RB-012 |
| 9 | internal-dns | notification-worker | 2025-09-02 | dns_failure | internal-dns-01, notification-worker-01 | SERVFAIL | multi-doc: also RB-020; has a twin: include the date; no fix question |
| 10 | search-cluster | - | 2025-11-18 | disk_full | search-cluster-02 | ENOSPC | multi-doc: also RB-008; has a twin: include the date |
| 11 | edge-gateway | - | 2026-03-10 | rate_limiting | edge-gateway-01, edge-gateway-02, edge-gateway-03 | 429, EDGE-RL01 | use this exact token: `EDGE-RL01`; has a twin: include the date |
| 12 | redis-features | fraud-scorer | 2026-06-17 | redis_down | fraud-scorer-01, fraud-scorer-02, redis-features-01 | OOM command not allowed, FRD-E140 | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 13 | edge-gateway | - | 2025-03-16 | tls_expired | edge-gateway-01 | - | paraphrase: no code, no hostname; has a twin: include the date |
| 14 | catalog-api | - | 2026-02-10 | memory_leak | catalog-api-02 | OOMKilled | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 15 | edge-gateway | - | 2026-01-09 | bad_deploy, api_latency | edge-gateway-01, edge-gateway-02, edge-gateway-03 | 504 | use this exact token: `edge-gateway-01`; no fix question; short doc: host, code or date lookup only |
| 16 | orders-api | edge-gateway | 2026-06-09 | api_latency | orders-api-01 | 504 | use this exact token: `orders-api-01`; has a twin: include the date; no fix question |
| 17 | payments-api | edge-gateway | 2025-01-23 | high_cpu, api_latency | payments-api-01, payments-api-02 | 504 | paraphrase: no code, no hostname; no fix question |
| 18 | payments-api | payments-db | 2026-05-06 | conn_pool_exhaustion, api_latency | payments-api-02, payments-api-03, payments-db-01 | PAYAPI-E1042, 503 | paraphrase: no code, no hostname; has a twin: include the date |
| 19 | internal-dns | kafka, redis-cache | 2026-02-03 | dns_failure | internal-dns-01, kafka-02 | - | use this exact token: `internal-dns-01`; has a twin: include the date; no fix question; short doc: host, code or date lookup only |
| 20 | orders-db | - | 2025-02-04 | disk_full | orders-db-01 | - | use this exact token: `orders-db-01`; has a twin: include the date; short doc: host, code or date lookup only |
| 21 | edge-gateway | search-api | 2026-06-13 | rate_limiting | edge-gateway-01, edge-gateway-02, edge-gateway-03 | 429, EDGE-RL01 | use this exact token: `EDGE-RL01`; has a twin: include the date; no fix question |
| 22 | redis-features | fraud-scorer | 2026-08-11 | redis_down | fraud-scorer-01, fraud-scorer-02, redis-features-01 | OOM command not allowed, FRD-E140 | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 23 | edge-gateway | - | 2025-08-07 | tls_expired | edge-gateway-01, edge-gateway-02, edge-gateway-03 | - | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 24 | notification-worker | - | 2026-02-23 | memory_leak | notification-worker-01 | OOMKilled | paraphrase: no code, no hostname; has a twin: include the date; short doc: host, code or date lookup only |
| 25 | catalog-api | catalog-db, edge-gateway | 2025-05-15 | bad_deploy, api_latency | catalog-api-01, catalog-api-02, catalog-db-01 | 504 | paraphrase: no code, no hostname |
| 26 | catalog-db | catalog-api, edge-gateway | 2025-07-30 | api_latency | catalog-api-02, catalog-db-01 | 504 | paraphrase: no code, no hostname; no fix question; short doc: host, code or date lookup only |
| 27 | orders-api | edge-gateway | 2025-04-29 | high_cpu, api_latency | orders-api-02, orders-api-03 | 504 | paraphrase: no code, no hostname; has a twin: include the date; no fix question; short doc: host, code or date lookup only |
| 28 | payments-api | payments-db | 2025-04-24 | conn_pool_exhaustion, api_latency | payments-api-03, payments-api-04, payments-db-01 | PAYAPI-E1042, 503 | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 29 | internal-dns | auth-service, catalog-api, orders-api | 2025-02-20 | dns_failure | auth-service-01, auth-service-02, catalog-api-01, catalog-api-02, orders-api-01, orders-api-02, orders-api-03 | EAI_AGAIN | paraphrase: no code, no hostname; has a twin: include the date |
| 30 | kafka | - | 2025-08-12 | disk_full | kafka-02 | ENOSPC | paraphrase: no code, no hostname; has a twin: include the date |
| 31 | card-processor-proxy | payments-api | 2026-07-15 | rate_limiting | card-processor-proxy-01 | CPX-W430 | paraphrase: no code, no hostname; has a twin: include the date |
| 32 | redis-cache | catalog-api | 2025-06-19 | redis_down | redis-cache-01, redis-cache-02 | CAT-E301 | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 33 | auth-db | auth-service | 2025-08-21 | tls_expired | auth-db-01, auth-service-01, auth-service-02 | 503 | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 34 | notification-worker | - | 2025-01-18 | memory_leak | notification-worker-01 | OOMKilled | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 35 | inventory-service | inventory-db, orders-api | 2025-07-09 | bad_deploy, api_latency | inventory-db-01, inventory-service-01, inventory-service-02, orders-api-01, orders-api-02, orders-api-03 | INV-E220, ORD-5003 | paraphrase: no code, no hostname; has a twin: include the date |
| 36 | inventory-service | inventory-db, orders-api | 2025-06-04 | api_latency | inventory-db-01, inventory-service-01, inventory-service-02 | INV-E220 | paraphrase: no code, no hostname; no fix question |
| 37 | search-api | search-cluster | 2025-01-15 | high_cpu, api_latency | search-api-01, search-cluster-02, search-cluster-03 | SRCH-E502 | paraphrase: no code, no hostname; no fix question |
| 38 | payments-api | payments-pgbouncer | 2025-06-24 | conn_pool_exhaustion, api_latency | payments-pgbouncer-01, payments-pgbouncer-02 | PAYAPI-E1042, 503 | paraphrase: no code, no hostname; has a twin: include the date |
| 39 | internal-dns | ledger-service | 2025-05-02 | dns_failure, bad_deploy | internal-dns-01, ledger-service-01 | NXDOMAIN | paraphrase: no code, no hostname; has a twin: include the date; no fix question |
| 40 | payments-db | - | 2025-09-25 | disk_full | payments-db-01 | - | paraphrase: no code, no hostname; has a twin: include the date |
