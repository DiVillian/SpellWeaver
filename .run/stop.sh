#!/bin/bash
# Stop the spellweaver dev server using its recorded pid.
D="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$D/server.pid" ]; then
  PID=$(cat "$D/server.pid")
  if kill "$PID" 2>/dev/null; then echo "stopped $PID"; else echo "not running"; fi
  rm -f "$D/server.pid"
else
  echo "no pid file"
fi
