#!/usr/bin/env bash
# Export YOLO11n to an FP16 TensorRT engine for this Jetson (about 10 minutes).
# Needs the tooling extra: uv sync --extra detector
set -euo pipefail
cd "$(dirname "$0")/.."
MODELS=$HOME/models
mkdir -p "$MODELS"
[ -f "$MODELS/yolo11n.pt" ] || curl -sL -o "$MODELS/yolo11n.pt" \
  https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
YOLO_AUTOINSTALL=False uv run python -c "
from ultralytics import YOLO
YOLO('$MODELS/yolo11n.pt').export(format='engine', half=True, imgsz=640, device=0)
"
ls -la "$MODELS/yolo11n.engine"
