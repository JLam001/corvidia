#!/usr/bin/env bash
# Download the public clips used for the laptop evaluation and ingest them into clips/.
#   DanceTrack (Voxel51/DanceTrack, CC BY 4.0): the three smallest mp4s + FiftyOne annotations
#   MOT17-05  (Lekim89/MOT17 mirror; upstream MOTChallenge CC BY-NC-SA 3.0): 14 fps moving camera
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
mkdir -p "$ROOT/data/dancetrack" "$ROOT/clips"
"$PY" - <<PY
import json, urllib.request
from huggingface_hub import hf_hub_download, snapshot_download
root = "$ROOT"
files = json.load(urllib.request.urlopen("https://huggingface.co/api/datasets/Voxel51/DanceTrack/tree/main/data"))
smallest = sorted((f["size"], f["path"]) for f in files if f["path"].endswith(".mp4"))[:3]
for _, p in smallest + [(0, "frames.json"), (0, "samples.json")]:
    hf_hub_download("Voxel51/DanceTrack", p, repo_type="dataset", local_dir=root + "/data/dancetrack")
    print("dancetrack:", p)
snapshot_download("Lekim89/MOT17", repo_type="dataset", allow_patterns=["train/MOT17-05-FRCNN/*"], local_dir=root + "/data/mot17")
print("mot17: train/MOT17-05-FRCNN")
PY
CLIP=$(ls "$ROOT"/data/dancetrack/data/*.mp4 | head -1)
[[ -d "$ROOT/clips/dancetrack0008" ]] || "$PY" "$ROOT/tools/ingest.py" fiftyone "$ROOT/data/dancetrack/data/dancetrack0008.mp4" \
    --frames "$ROOT/data/dancetrack/frames.json" --samples "$ROOT/data/dancetrack/samples.json" --out "$ROOT/clips/dancetrack0008" \
    || "$PY" "$ROOT/tools/ingest.py" fiftyone "$CLIP" --frames "$ROOT/data/dancetrack/frames.json" \
         --samples "$ROOT/data/dancetrack/samples.json" --out "$ROOT/clips/$(basename "${CLIP%.mp4}")"
[[ -d "$ROOT/clips/mot17-05" ]] || "$PY" "$ROOT/tools/ingest.py" mot "$ROOT/data/mot17/train/MOT17-05-FRCNN" --out "$ROOT/clips/mot17-05"
ls "$ROOT/clips"
