#!/usr/bin/env bash
# Walk through the API against a running server (default http://127.0.0.1:8000).
# Usage: scripts/demo.sh [base_url]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

show() {
  local title="$1" url="$2"
  printf '\n\033[1m%s\033[0m\n  GET %s\n' "$title" "$url"
  curl -s -w '\n  -> HTTP %{http_code} in %{time_total}s\n' "$url" | python3 -c '
import json, sys
raw = sys.stdin.read()
body, meta = raw.rsplit("\n", 2)[0], raw.rsplit("\n", 2)[1]
try:
    data = json.loads(body)
except ValueError:
    print(body); print(meta); sys.exit()
if "results" in data:
    print(f"  query={data[\"query\"]!r} total_matches={data[\"total_matches\"]} exact={data[\"exact\"]}")
    for w in data["results"]:
        print(f"  {w[\"object_end_date\"]:>5}  {w[\"title\"][:60]!s:<60} {w[\"artist\"] or \"\"}")
else:
    print("  " + json.dumps(data))
print(meta)
'
}

show "Health"                              "$BASE/health"
show "Default query (bread), 5 results"    "$BASE/api/works?limit=5"
show "Same again: served from cache"       "$BASE/api/works?limit=5"
show "Custom term and limit"               "$BASE/api/works?q=cheese&limit=3"
show "No matches"                          "$BASE/api/works?q=xyzzyqwertyzz&limit=5"
show "Validation: limit is required"       "$BASE/api/works"
show "Validation: limit out of bounds"     "$BASE/api/works?limit=50"
show "Validation: non-ASCII query"         "$BASE/api/works?q=br%C3%ABad&limit=5"
show "Validation: blank query"             "$BASE/api/works?q=%20&limit=5"
printf '\nInteractive docs: %s/docs\n' "$BASE"
