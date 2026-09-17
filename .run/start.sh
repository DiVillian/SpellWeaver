#!/bin/bash
# Start the spellweaver dev server and record its pid.
D="$(cd "$(dirname "$0")" && pwd)"; R="$(dirname "$D")"
"$D/stop.sh" >/dev/null 2>&1
nohup python3 "$R/run.py" --port "${1:-8800}" > "$D/server.log" 2>&1 &
echo $! > "$D/server.pid"
for i in $(seq 1 40); do
  sleep 0.25
  grep -q "ready at" "$D/server.log" 2>/dev/null && break
done
cat "$D/server.log"
