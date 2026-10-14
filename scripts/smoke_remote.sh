#!/usr/bin/env bash
# Smoke test of a deployed OpsPilot: /health, then the demo investigation through Nginx.
#   OPSPILOT_API_KEY=... scripts/smoke_remote.sh <host>      (host: IP or domain, port 80)
# The demo question runs with the fixed demo as_of, so a repeat run is served from the
# server's LLM response cache (no spend). The key is read from the environment and sent on
# curl's stdin, so it never appears in argv (ps) or in this script's output.
set -euo pipefail

host="${1:?usage: OPSPILOT_API_KEY=... $0 <host>}"
: "${OPSPILOT_API_KEY:?set OPSPILOT_API_KEY (not printed)}"
base="http://${host}"
cd "$(dirname "$0")/.."

echo "1/3 GET ${base}/health"
curl -fsS --max-time 10 "${base}/health" | grep -q '"status":"ok"' || {
	echo "FAIL: /health"
	exit 1
}
echo "    ok"

question=$(sed -n 's/^question = "\(.*\)"$/\1/p' eval/corpus/demo_answer.toml)
body=$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1], "as_of": "2026-10-06T14:52:00Z"}))' "$question")
out=$(mktemp)
trap 'rm -f "$out"' EXIT

echo "2/3 POST ${base}/investigate (demo question; streams up to 300 s)"
start=$(date +%s)
code=$(printf 'X-API-Key: %s\n' "$OPSPILOT_API_KEY" |
	curl -sS -N --max-time 300 -o "$out" -w '%{http_code}' -H @- \
		-H 'Content-Type: application/json' --data "$body" "${base}/investigate")
echo "    HTTP ${code} in $(($(date +%s) - start)) s; events: $(sed -n 's/^event: //p' "$out" | sort | uniq -c | tr -s ' \n' ' ')"
[ "$code" = 200 ] || {
	echo "FAIL: HTTP ${code}: $(head -c 300 "$out")"
	exit 1
}
grep -q '^event: diagnosis' "$out" || {
	echo "FAIL: no diagnosis event; last error: $(grep -A1 '^event: error' "$out" | tail -1 | head -c 300)"
	exit 1
}

iid=$(sed -n 's/^data: .*"investigation_id": "\([^"]*\)".*/\1/p' "$out" | head -1)
echo "3/3 GET ${base}/investigations/${iid}"
curl -fsS --max-time 10 "${base}/investigations/${iid}" | python3 -c '
import json, sys
inv = json.load(sys.stdin)
calls = inv.get("llm_calls") or []
cached = sum(bool(c.get("cached")) for c in calls)
status, cost = inv.get("status"), inv.get("cost_usd")
print(f"    status {status}; llm calls {len(calls)} ({cached} cached); cost ${cost}")
sys.exit(status != "done")'
echo "smoke: OK"
