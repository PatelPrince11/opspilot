# Label audit (Gemini, pasted by hand)

Total questions: 160. Gemini lines: 160. Flagged: 2. Sampled OK: 10 (seed 20261003).

# Flagged by Gemini

## Q091 (exact-token): Gemini OK

**Question:** edge returning 504 on payment routes from 18:10 on 23 January 2025, payments-api-01 and -02 look CPU throttled. what's the picture?

**Gold:** INC-0008

**Key facts:**

1. CPU throttling held requests past the gateway's 15 second route timeout
2. throttling above about 25% of periods is significant

**Gemini:** missing_gold: RB-002 | unsupported_facts: none | reason: INC-0008 states CPU throttling on payments-api pods held requests past the edge's 15 second route timeout causing 504s and notes throttling above 25% of periods is significant.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q100 (exact-token): Gemini OK

**Question:** ledger-service-01 alone hot late 1 June 2026 with LEDGER-W210 in the log, where is the load coming from?

**Gold:** INC-0165

**Key facts:**

1. serializing the month-end reconciliation result on an http-nio request thread
2. LEDGER-W210 was a consequence of slow threads, not the cause
3. not the Kafka retry spin

**Gemini:** missing_gold: RB-001 | unsupported_facts: none | reason: INC-0165 states serializing the month-end reconciliation result ran on an http-nio request thread, that LEDGER-W210 was a consequence of threads competing for CPU and holding connections, and that the Kafka retry spin was not the cause.

**Prince:** AGREE/DISAGREE, note: AGREE

# Random sample of questions Gemini marked OK

## Q002 (exact-token): Gemini OK

**Question:** searches through search-api-02 crawled on 19 August 2026 and the hottest threads on search-cluster-01 were GC, any error codes to look for?

**Gold:** INC-0187, RB-003

**Key facts:**

1. GC pauses on search-cluster-01 held up the queries that search-api-02 sent to it
2. no error code was seen, only long response times

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0187 fully details the 19 August 2026 incident, stating GC held up queries and no error codes were seen.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q008 (paraphrase): Gemini OK

**Question:** edge 504s across multiple routes simultaneously from 05:50 on 16 July 2025, no single upstream looks bad, where does the triage point?

**Gold:** INC-0060, RB-014

**Key facts:**

1. the problem sat on the node under edge-gateway-03
2. latency up on every route at once is a shared cause such as the node pool

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0060 and RB-014 together explain that simultaneous edge 504s point to node pool resource pressure, specifically under edge-gateway-03 here.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q028 (exact-token): Gemini OK

**Question:** payments-api getting 503 from ledger-service-01 and ledger-service-02 from 13:15 on 11 September 2026, what changed right before?

**Gold:** INC-0194

**Key facts:**

1. a new ledger-service release started serving at 13:15 and the 503s began with it
2. pods being Ready does not show a ledger call from payments-api succeeds

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0194 confirms the 503 errors started with a new ledger-service release at 13:15, noting that pod readiness doesn't guarantee call success.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q037 (exact-token): Gemini OK

**Question:** auth-service-01 and auth-service-02 logins slowed then returned 503 on 22 May 2025, was auth-db out of connection slots?

**Gold:** INC-0046

**Key facts:**

1. auth-service pools total 2 x 30 = 60, well inside the database's limit
2. the slowness was inside auth-db, which kept the pool connections checked out

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0046 confirms auth-db had plenty of slots (200 against 60), but internal database slowness kept the pool connections tied up.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q074 (exact-token): Gemini OK

**Question:** how did they put back the record that vanished from internal-dns-01 in the March 2025 orders-api NXDOMAIN trouble?

**Gold:** INC-0025

**Key facts:**

1. record restored in the zone source with a higher SOA serial
2. corrected file pushed to internal-dns-01

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0025 documents that the fix was restoring the record with a higher SOA serial in the source and pushing the corrected file to internal-dns-01.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q087 (paraphrase): Gemini OK

**Question:** orders-api, auth-service and catalog-api all timing out on lookups, should i just bounce the coredns deployment?

**Gold:** RB-022, INC-0016

**Key facts:**

1. restarting overloaded pods removes capacity
2. scale coredns replicas up instead
3. internal-dns can be ruled out with dig from a VM

**Gemini:** missing_gold: none | unsupported_facts: none | reason: RB-022 states restarting overloaded pods removes capacity, advises scaling coredns replicas, and ruling out internal-dns using dig from a VM; INC-0016 states bouncing pods cuts capacity, coredns was scaled to 4 replicas, and internal-dns was checked with dig from a VM.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q102 (multi-doc): Gemini OK

**Question:** fraud-scorer keeps hitting late-score fallbacks with calm-looking cpu, how many times has that bitten us and what was always the lever?

**Gold:** INC-0024, INC-0030, INC-0189, RB-002

**Key facts:**

1. PAYAPI-W0310 on payments-api when scores arrive late
2. throttled periods above 25%
3. raising the CPU limit while leaving the request unchanged

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0024, INC-0030, INC-0189, and RB-002 state PAYAPI-W0310 logs when scores arrive late, throttled periods above 25% is the threshold, and the fix is raising the CPU limit while leaving the request unchanged.

**Prince:** AGREE/DISAGREE, note: AGREE

## Q130 (exact-token): Gemini OK

**Question:** crawler hammering /search on 13 June 2026 and EDGE-RL01 on every edge pod while search-api shows no errors, what was the per-pod limit then?

**Gold:** INC-0167, RB-030

**Key facts:**

1. Limit was 250 requests per second per pod, 750 across the three
2. Refused requests were stopped at the edge so search-api logged no errors

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0167 states limit was 250 requests per pod (750 across three), and refused requests stopped at the edge so search-api logged no errors

**Prince:** AGREE/DISAGREE, note: AGREE

## Q137 (exact-token): Gemini OK

**Question:** planned SENTINEL FAILOVER sessions on 7 October 2025 before patching, auth-service-01 saw READONLY, how many users were hurt?

**Gold:** INC-0087, RB-018

**Key facts:**

1. auth-service-01 logged READONLY for session writes for a few seconds
2. Few users had to sign in again, seen as AUTH-E401X on their next request
3. redis-sessions-02 became primary and redis-sessions-01 a replica

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0087 and RB-018 state auth-service-01 logged READONLY for a few seconds and a few users had to sign in again after hitting AUTH-E401X; INC-0087 states redis-sessions-02 became primary and redis-sessions-01 became a replica

**Prince:** AGREE/DISAGREE, note: AGREE

## Q159 (exact-token): Gemini OK

**Question:** both auth-service pods refused at the TLS handshake to auth-db-01 in February 2026 on the 8th, what did verify-full find wrong?

**Gold:** INC-0126

**Key facts:**

1. The primary database's certificate did not list the name the client connected to
2. Neither expiry nor an unloaded renewal applied
3. Ran 09:00 to 09:55

**Gemini:** missing_gold: none | unsupported_facts: none | reason: INC-0126 states the primary database's certificate did not list the name the client connected to, neither expiry nor an unloaded renewal applied, and it ran from 09:00 to 09:55

**Prince:** AGREE/DISAGREE, note: AGREE
