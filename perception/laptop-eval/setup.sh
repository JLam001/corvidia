#!/usr/bin/env bash
# One-time laptop setup: venv with CPU PyTorch, Ultralytics, OpenCV, transformers, this package
# (editable), the YOLO11n weights and the Depth Anything V2 Metric Indoor Small checkpoint.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
python3 -m venv "$VENV" || true
if [[ ! -x "$VENV/bin/pip" ]]; then   # Debian/Ubuntu without python3-venv's ensurepip
    curl -sSfL https://bootstrap.pypa.io/get-pip.py -o "$ROOT/get-pip.py"
    "$VENV/bin/python" "$ROOT/get-pip.py" -q && rm -f "$ROOT/get-pip.py"
fi
PIP="$VENV/bin/pip"
$PIP install -q "numpy>=1.26,<2" pillow lap pytest "opencv-python-headless<5" huggingface_hub "scipy<1.14" "transformers>=4.45"
$PIP install -q --index-url https://download.pytorch.org/whl/cpu torch torchvision
$PIP install -q "ultralytics==8.3.*"
$PIP install -q --no-deps -e "$ROOT/.."     # corvidia_perception, editable
$PIP install -q "numpy>=1.26,<2"            # re-pin: some wheels above try to pull NumPy 2
mkdir -p "$ROOT/models"
[[ -f "$ROOT/models/yolo11n.pt" ]] || curl -sL -o "$ROOT/models/yolo11n.pt" \
    https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
"$VENV/bin/python" - <<PY
from huggingface_hub import snapshot_download
snapshot_download('depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf', local_dir='$ROOT/models/da2-metric-indoor-small-hf')
import numpy, cv2, torch, ultralytics, transformers, corvidia_perception
print('numpy', numpy.__version__, 'cv2', cv2.__version__, 'torch', torch.__version__, 'ultralytics', ultralytics.__version__, 'transformers', transformers.__version__)
PY
echo "setup done: $VENV"
