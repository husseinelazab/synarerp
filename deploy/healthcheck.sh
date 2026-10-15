#!/bin/sh
# ينتظر حتى يرد النظام وقاعدة البيانات (/healthz/) بحد أقصى عدد الثواني المعطى (افتراضي 120)
limit=${1:-120}
elapsed=0
while [ "$elapsed" -lt "$limit" ]; do
  if docker compose exec -T web python -c '
import os, urllib.request
host = os.environ.get("SYNAR_ALLOWED_HOSTS", "localhost").split(",")[0].strip().lstrip(".") or "localhost"
request = urllib.request.Request("http://127.0.0.1:8000/healthz/", headers={"Host": host, "X-Forwarded-Proto": "https"})
assert urllib.request.urlopen(request, timeout=5).read() == b"ok"
' >/dev/null 2>&1; then
    exit 0
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done
exit 1
