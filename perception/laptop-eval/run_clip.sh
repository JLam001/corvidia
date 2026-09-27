#!/usr/bin/env bash
# YOLO-alone arm of the step 5 evaluation on one ingested clip: corvidia-run (accuracy mode, stub
# confirmer, no depth) -> autolabel from gt.txt -> corvidia-encounter-eval score.
#   ./run_clip.sh <clip name> [--depiction] [extra corvidia-run args...]
# Sessions land in runs/<clip>/<session>/; labels accumulate in runs/labels.json.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$ROOT/.venv}"
CLIP="$1"; shift
DEPICTION=""
if [[ "${1:-}" == "--depiction" ]]; then DEPICTION="--depiction"; shift; fi
[[ -d "$ROOT/clips/$CLIP" ]] || { echo "no clip $ROOT/clips/$CLIP (run fetch_datasets.sh or tools/ingest.py)" >&2; exit 2; }
EVENTS="$ROOT/runs/$CLIP"
mkdir -p "$EVENTS"
LOG="$EVENTS/last_run.log"
"$VENV/bin/corvidia-run" --video "$ROOT/clips/$CLIP" --mode every --confirmer yes --no-depth \
    --config "$ROOT/laptop.toml" --model "$ROOT/models/yolo11n.pt" --events "$EVENTS" "$@" > "$LOG" 2>&1
grep -E "^(session|report)" "$LOG" || true
SESSION=$(sed -n 's/^session [^ ]* -> //p' "$LOG" | tail -1)
[[ -n "$SESSION" && -d "$SESSION" ]] || { echo "could not find the session directory in $LOG" >&2; exit 1; }
"$VENV/bin/python" - "$SESSION" "$ROOT/clips/$CLIP/clip.json" <<'PY'
import json, sys
rep = json.load(open(sys.argv[1] + "/run_report.json")); clip = json.load(open(sys.argv[2]))
faults = rep.get("faults") or rep.get("health", {}).get("faults") or {}
problems = []
if rep.get("frames_processed") != clip["frames"]:
    problems.append(f"frames_processed {rep.get('frames_processed')} != clip frames {clip['frames']} (interrupted run?)")
if faults:
    problems.append(f"faults {faults}")
if problems:
    sys.exit("NOT SCORING: " + "; ".join(problems))
print(f"run ok: {rep['frames_processed']} frames, {rep.get('events')} events, detector p50 {rep['detector_ms']['p50']} ms")
PY
"$VENV/bin/python" "$ROOT/tools/autolabel.py" --runs "$SESSION" --clips "$ROOT/clips" --labels "$ROOT/runs/labels.json" $DEPICTION
"$VENV/bin/corvidia-encounter-eval" score --runs "$SESSION" --labels "$ROOT/runs/labels.json" --out "$SESSION/encounter_score.json"
