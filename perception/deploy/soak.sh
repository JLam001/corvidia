#!/usr/bin/env bash
# Sustained-load run (spec step 4): camera + TensorRT YOLO + Cosmos, detached from
# the SSH session, with a timeseries.jsonl row every 10 s for drift analysis.
#
#   deploy/soak.sh start [minutes]   # default 60
#   deploy/soak.sh status
#   deploy/soak.sh stop              # graceful stop; writes run_report.json
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=$HOME/corvidia-data/soak.log
case "${1:-status}" in
  start)
    mins=${2:-60}
    mkdir -p "$(dirname "$LOG")"
    nohup "$HOME/.local/bin/uv" run corvidia-run --camera --confirmer cosmos --preview-port 8080 \
      --duration $((mins * 60)) >"$LOG" 2>&1 &
    echo "soak started for $mins min (pid $!); preview http://192.168.2.2:8080/ ; log $LOG"
    ;;
  status)
    pgrep -af "corvidia-run --camera" || echo "not running"
    s=$(ls -td "$HOME"/corvidia-data/events/*/ 2>/dev/null | head -1)
    [ -n "$s" ] && [ -f "$s/timeseries.jsonl" ] && tail -1 "$s/timeseries.jsonl"
    ;;
  stop)
    pkill -INT -f "corvidia-run --camera" && echo "stopping (report is written on exit)" || echo "not running"
    ;;
  *) echo "usage: $0 [start [minutes]|status|stop]"; exit 2 ;;
esac
