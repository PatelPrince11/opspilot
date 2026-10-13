# Label spot-check (40 of the 160 drafted questions)

Seeded random sample (seed 20261003), stratified by difficulty: exact-token 20, multi-doc 12, paraphrase 8. For each, open the gold docs and check that every key fact is stated there, that the gold set is complete, and that the tag fits. Mark ok / wrong with a note.

## Q001 (exact-token)

**Question:** search-api-01 slow from about 01:00 on 8 December 2025, which search-cluster node was stuck in GC?

**Gold:** INC-0107, RB-003

**Key facts:**
- search-cluster-03 had long garbage-collection pauses
- search latency was back to normal by 01:45

**Check:** [ ] ok  [ ] wrong:

## Q003 (exact-token)

**Question:** card-processor-proxy-01 and -02 both slow talking to the processor on 20 February 2025, what window?

**Gold:** INC-0015

**Key facts:**
- 10:05 to 10:45 UTC
- both proxy instances saw the same slowness

**Check:** [ ] ok  [ ] wrong:

## Q005 (exact-token)

**Question:** INV-E220 on both inventory-service pods on 4 June 2025, what were the sessions on inventory-db-01 doing?

**Gold:** INC-0049, RB-015

**Key facts:**
- sessions on inventory-db-01 were waiting on row locks in the reservations table

**Check:** [ ] ok  [ ] wrong:

## Q008 (paraphrase)

**Question:** edge 504s across multiple routes simultaneously from 05:50 on 16 July 2025, no single upstream looks bad, where does the triage point?

**Gold:** INC-0060, RB-014

**Key facts:**
- the problem sat on the node under edge-gateway-03
- latency up on every route at once is a shared cause such as the node pool

**Check:** [ ] ok  [ ] wrong:

## Q010 (exact-token)

**Question:** catalog-api-02 504s on 30 July 2025, which database host and how long?

**Gold:** INC-0065

**Key facts:**
- catalog-db-01
- 06:50 to 09:45 UTC, 175 minutes

**Check:** [ ] ok  [ ] wrong:

## Q012 (exact-token)

**Question:** emails running about an hour late on 29 June 2026 with NTF-E710 from both notification-worker pods, what slowed down?

**Gold:** INC-0171

**Key facts:**
- the email provider's API slowed from about 0.33 s to about 1.6 s per send
- throughput fell below the arrival rate of about 40 messages a second

**Check:** [ ] ok  [ ] wrong:

## Q014 (multi-doc)

**Question:** two inventory slowdowns with INV-E220, June 2025 and October 2025, how do I tell lock waits from table bloat on inventory-db-01?

**Gold:** INC-0049, INC-0088, RB-015

**Key facts:**
- June 2025 was sessions waiting on row locks in the reservations table
- October 2025 was table bloat on the reservations table
- low CPU with many waiting sessions points to locks rather than expensive queries

**Check:** [ ] ok  [ ] wrong:

## Q026 (exact-token)

**Question:** AUTH-E401X jumped at 14:08 on 21 July 2026 when the first new auth-service pod went Ready, then again after the rollback, why two waves?

**Gold:** INC-0178, RB-026

**Key facts:**
- the release changed how session values under sess: in redis-sessions were encoded
- neither build could decode values written by the other, so the rollback stranded sessions made during the release

**Check:** [ ] ok  [ ] wrong:

## Q028 (exact-token)

**Question:** payments-api getting 503 from ledger-service-01 and ledger-service-02 from 13:15 on 11 September 2026, what changed right before?

**Gold:** INC-0194

**Key facts:**
- a new ledger-service release started serving at 13:15 and the 503s began with it
- pods being Ready does not show a ledger call from payments-api succeeds

**Check:** [ ] ok  [ ] wrong:

## Q030 (multi-doc)

**Question:** catalog migrations have waited on locks before, May 2025 and December 2025, what protects us now?

**Gold:** INC-0042, INC-0112, RB-028

**Key facts:**
- in May 2025 the migration waited behind the weekly export and 504s followed
- the migration Job runs with a 5 second lock_timeout so it gives up quickly

**Check:** [ ] ok  [ ] wrong:

## Q032 (exact-token)

**Question:** did we lose ledger entries when LEDGER-E455 showed up during the kafka broker outage on 13 August 2026?

**Gold:** INC-0185, RB-027

**Key facts:**
- ledger-service retry queue peaked at about 1,200 entries and was empty by 10:28
- with one broker left acks=all writes were rejected because two in-sync copies were impossible

**Check:** [ ] ok  [ ] wrong:

## Q036 (exact-token)

**Question:** PAYAPI-E1042 on payments-api-03 and payments-api-04 on 24 April 2025, pools at 40 of 40, what was behind it?

**Gold:** INC-0035, RB-010

**Key facts:**
- slow queries on payments-db-01 kept the pgx pools on payments-api-03 and payments-api-04 checked out
- requests gave up at the 500 ms acquire deadline

**Check:** [ ] ok  [ ] wrong:

## Q048 (multi-doc)

**Question:** orders-api pool exhaustion twice, last day of February 2026 and 8 August 2026, compare the causes

**Gold:** INC-0134, INC-0182

**Key facts:**
- February was month-end order-summary requests holding connections about 30 s on orders-api-01 and -03
- August was a slow query on orders-db on orders-api-02 and -03

**Check:** [ ] ok  [ ] wrong:

## Q053 (paraphrase)

**Question:** catalog edge 504 arrived before the pool timeout in the logs on 15 October 2025, why that order?

**Gold:** INC-0091, RB-028

**Key facts:**
- the pool waits 30 seconds, longer than the edge's 15-second route timeout
- the pool timeout appeared in the service log about 15 seconds after the edge had already answered

**Check:** [ ] ok  [ ] wrong:

## Q060 (paraphrase)

**Question:** payments primary went down in May 2026 with out-of-space and every session got dropped with 57P02, payments-api lost its connections. what filled the volume?

**Gold:** INC-0157, RB-007

**Key facts:**
- WAL archiving to object storage was failing
- segments piled up in pg_wal until the volume was full
- sessions cut with SQLSTATE 57P02

**Check:** [ ] ok  [ ] wrong:

## Q062 (multi-doc)

**Question:** which hosts had /var filled by an application log rather than the journal? every case, kafka-02 and the rest

**Gold:** INC-0070, INC-0101, RB-008

**Key facts:**
- kafka-02: broker GC logs in /var/log/kafka
- search-cluster-02: one OpenSearch log under /var/log/opensearch/ grew about 1 GB a day after a rebuild dropped its logrotate file
- check the logrotate file and force a rotation; OpenSearch needed copytruncate

**Check:** [ ] ok  [ ] wrong:

## Q065 (exact-token)

**Question:** health probe on ledger-db-02 failed creating a scratch file, ENOSPC, 9 Feb 2026. database hasn't grown, so why full?

**Gold:** INC-0127, RB-008

**Key facts:**
- /var/log/journal filled the shared root disk
- no size directive in journald.conf
- journalctl --vacuum-size=500M then SystemMaxUse=500M

**Check:** [ ] ok  [ ] wrong:

## Q067 (exact-token)

**Question:** search-cluster-01 got a /var over 90% ticket on 9 August 2026 and the opensearch log folder was fine. what had grown?

**Gold:** INC-0183, RB-008

**Key facts:**
- the systemd journal had no size limit
- opensearch logrotate entry was present and the live files tiny
- root disk is 40 GB with about 11 GB normally used

**Check:** [ ] ok  [ ] wrong:

## Q068 (multi-doc)

**Question:** our VMs keep running out of root disk from the journal: kafka-01 in october 2025, kafka-03 in april 2026 and others. same story each time?

**Gold:** INC-0001, INC-0011, INC-0079, INC-0089, INC-0109, INC-0127, INC-0149, INC-0183, RB-008

**Key facts:**
- each host lacked a journald size cap so /var/log/journal grew on the root disk
- vacuum with journalctl --vacuum-size=500M
- persist with SystemMaxUse=500M in /etc/systemd/journald.conf

**Check:** [ ] ok  [ ] wrong:

## Q072 (multi-doc)

**Question:** lookups timing out in orders-api, auth-service and catalog-api all at once, VMs resolve fine. what's wrong and how did we deal with it in Feb 2025?

**Gold:** INC-0016, RB-022

**Key facts:**
- the kube-system coredns deployment ran 2 replicas both at their CPU ceiling
- scaled coredns from 2 to 4 replicas
- internal-dns answered normally when asked from a VM

**Check:** [ ] ok  [ ] wrong:

## Q077 (exact-token)

**Question:** catalog-api-02 intermittently failing to reach its database from 20:25 on 19 September 2025, NXDOMAIN on the db name only. what changed right then?

**Gold:** INC-0081, RB-020

**Key facts:**
- a zone-file push left internal-dns-02 serving a file without the catalog database record
- the push tool still finished with 0 failed hosts
- cluster resolver caches replies at most 30 seconds

**Check:** [ ] ok  [ ] wrong:

## Q082 (multi-doc)

**Question:** how do i prove checkout slowness is the pod resolver and not the database, from inside an orders-api pod?

**Gold:** RB-021, INC-0168

**Key facts:**
- time about 200 dns.lookup calls from the pod
- a couple of lookups take roughly 5000 ms while most take a few ms

**Check:** [ ] ok  [ ] wrong:

## Q087 (paraphrase)

**Question:** orders-api, auth-service and catalog-api all timing out on lookups, should i just bounce the coredns deployment?

**Gold:** RB-022, INC-0016

**Key facts:**
- restarting overloaded pods removes capacity
- scale coredns replicas up instead
- internal-dns can be ruled out with dig from a VM

**Check:** [ ] ok  [ ] wrong:

## Q091 (exact-token)

**Question:** edge returning 504 on payment routes from 18:10 on 23 January 2025, payments-api-01 and -02 look CPU throttled. what's the picture?

**Gold:** INC-0008, RB-002

**Key facts:**
- CPU throttling held requests past the gateway's 15 second route timeout
- throttling above about 25% of periods is significant

**Check:** [ ] ok  [ ] wrong:

## Q092 (exact-token)

**Question:** PAYAPI-W0310 at about 40 a second on payments-api-03 on 18 March 2025 and fraud-scorer-01 only 45% busy on average. what gives?

**Gold:** INC-0024, RB-002

**Key facts:**
- 30% of fraud-scorer-01 periods were CFS throttled
- 16 gunicorn workers because WEB_CONCURRENCY was unset
- raised the CPU limit and left the request unchanged

**Check:** [ ] ok  [ ] wrong:

## Q112 (multi-doc)

**Question:** orders-api-03 kept exiting 137 all night on 9 August 2025, how do I be sure it was memory and not a missed shutdown deadline?

**Gold:** INC-0069, RB-004, RB-005

**Key facts:**
- A shutdown-deadline kill also exits 137 but with another reason, so read the reason
- Reason here was OOMKilled
- Heap flag standard is about 75% of the container limit

**Check:** [ ] ok  [ ] wrong:

## Q114 (exact-token)

**Question:** fraud-scorer-02 tops kubectl top well above its sibling on 22 July 2025 and no restarts, what should I compare first?

**Gold:** INC-0063, RB-006

**Key facts:**
- Count FRD-W118 per pod: fraud-scorer-02 had several times as many
- redis-cli --latency on redis-features-01 showed regular spikes
- Each bump in warnings was followed by a step up in working set

**Check:** [ ] ok  [ ] wrong:

## Q116 (paraphrase)

**Question:** scorer pod OOMKilled twice one evening in November 2025 on a 16-core node, why did it get that close to the limit?

**Gold:** INC-0097

**Key facts:**
- WEB_CONCURRENCY unset gives one worker per visible core, 16
- 16 workers at about 150 MiB is 2,400 MiB inside a 3,072 MiB limit
- That leaves 672 MiB for the feature cache

**Check:** [ ] ok  [ ] wrong:

## Q118 (multi-doc)

**Question:** scorer memory is creeping again and redis-features latency spikes are back, has this happened before and what is the usual stopgap?

**Gold:** INC-0013, INC-0063, INC-0080, INC-0097, RB-006

**Key facts:**
- Feature cache in the scorer grows without bound when redis-features is slow
- Restart is a stopgap; it only holds once the store is answering normally
- Workers recycle every 5000 requests (--max-requests)

**Check:** [ ] ok  [ ] wrong:

## Q122 (exact-token)

**Question:** both proxies logged CPX-W429 for ten minutes on Tuesday 19 May 2026, were our own retries pushing it?

**Gold:** INC-0159

**Key facts:**
- Each payment kept its usual three attempts, so retries were not adding load
- Processor limit is 400 requests per second for our merchant account
- Lasted from 06:55 to 07:05

**Check:** [ ] ok  [ ] wrong:

## Q123 (paraphrase)

**Question:** five minutes of declined card payments at 00:30 on 12 September 2026, how do we know the processor pushed back and not our own proxy limiter?

**Gold:** INC-0195, RB-029

**Key facts:**
- CPX-W429 is logged when the processor answers with its rate-limit reply
- The proxy's own limiter rejections never leave the proxy
- Both pods logged it from the same minute

**Check:** [ ] ok  [ ] wrong:

## Q124 (multi-doc)

**Question:** checkout 504s on 26 May 2026 with the processor pushing back, how do I tell a retry storm from a plain slow dependency?

**Gold:** INC-0162, RB-016

**Key facts:**
- compare request rates per hop: during a storm the lower hops run at two or more times the edge rate
- flat ratios with everything slow means it is not a retry storm

**Check:** [ ] ok  [ ] wrong:

## Q129 (exact-token)

**Question:** search returning 429 with EDGE-RL01 on all three edge pods though search-api looks calm, 10 March 2026, what limit and what changed?

**Gold:** INC-0137, RB-030

**Key facts:**
- The /search limit was raised from 100 to 250 per pod
- Setting is read only at Envoy start so the edge pods needed a restart
- About 29% of searches were refused

**Check:** [ ] ok  [ ] wrong:

## Q130 (exact-token)

**Question:** crawler hammering /search on 13 June 2026 and EDGE-RL01 on every edge pod while search-api shows no errors, what was the per-pod limit then?

**Gold:** INC-0167, RB-030

**Key facts:**
- Limit was 250 requests per second per pod, 750 across the three
- Refused requests were stopped at the edge so search-api logged no errors

**Check:** [ ] ok  [ ] wrong:

## Q144 (multi-doc)

**Question:** how do I tell whether the feature store is refusing writes because snapshots are failing or because it is full, like 9 September 2025?

**Gold:** RB-019, INC-0078

**Key facts:**
- rdb_last_bgsave_status:err points to the MISCONF path
- used_memory at maxmemory with noeviction points to the OOM path
- Redis log shows fork: Cannot allocate memory when the commit limit is the cause

**Check:** [ ] ok  [ ] wrong:

## Q149 (exact-token)

**Question:** one customer's device rejects our edge cert on 19 January 2025 and edge-gateway-03 shows a broken-off handshake, what do you check?

**Gold:** INC-0007, RB-025

**Key facts:**
- The certificate edge-gateway-03 serves was in date until 2025-03-16 10:15
- One client failing while others are fine points at the client's clock
- No change at the edge

**Check:** [ ] ok  [ ] wrong:

## Q152 (paraphrase)

**Question:** partner integration failing on roughly one call in three on 16 March 2025 and one edge pod has near-zero request rate, how did one pod keep an old cert after renewal?

**Gold:** INC-0023, RB-025

**Key facts:**
- edge-gateway-01 presented a certificate with notAfter 2025-03-16 10:15 UTC
- Pods were hand-deleted and the original pod behind edge-gateway-01 was never restarted
- Envoy reads the certificate only at start; rollout restart fixed it

**Check:** [ ] ok  [ ] wrong:

## Q153 (paraphrase)

**Question:** some clients cannot open TLS to the edge on 15 May 2025 and s_client gives verify code 21 on all three pods, what does that mean?

**Gold:** INC-0043, RB-025

**Key facts:**
- Verify code 21 means the intermediate is missing from the served file
- All three pods served the same incomplete file with our certificate and no intermediate
- Clients without a cached intermediate could not complete the handshake

**Check:** [ ] ok  [ ] wrong:

## Q156 (exact-token)

**Question:** login 503s climbing slowly in April 2025, auth-db-01 looks healthy and a renewed cert is on disk but dials fail with x509 expired, why?

**Gold:** INC-0031, RB-024

**Key facts:**
- Postgres kept the previous certificate in memory; the presented serial differs from the file
- pg_reload_conf() on auth-db-01 and on replica auth-db-02, a reload not a restart
- Only fresh connections fail; pooled ones carry on

**Check:** [ ] ok  [ ] wrong:

## Q158 (multi-doc)

**Question:** auth-db cert file on disk looks renewed but I suspect postgres still serves the old one, as in April 2025, how do I compare and does the replica need it too?

**Gold:** RB-024, INC-0031

**Key facts:**
- Compare serial and enddate from openssl s_client -starttls postgres against openssl x509 on server.crt
- Run SELECT pg_reload_conf(); auth-db-02 needs the same check and reload

**Check:** [ ] ok  [ ] wrong:
