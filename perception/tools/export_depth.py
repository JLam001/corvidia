"""Export a Depth Anything V2 metric checkpoint to ONNX with a fixed input size.

Runs in an environment with torch + transformers + onnx (e.g. the llama.cpp
conversion venv), then build the engine on the Jetson with trtexec:

    python tools/export_depth.py ~/models/depth/da2-metric-indoor-small-hf \
        ~/models/depth/da2-metric-indoor-small-294x518.onnx --height 294 --width 518
    /usr/src/tensorrt/bin/trtexec --onnx=...onnx --saveEngine=...engine --fp16

Input: float32 (1, 3, H, W), RGB, ImageNet-normalized. Output: depth in meters
(1, H, W). H and W must be multiples of 14 (the ViT patch size).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from transformers import AutoModelForDepthEstimation


class DepthOnly(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, pixel_values: torch.Tensor) -> torch.Tensor:
        return self.model(pixel_values=pixel_values).predicted_depth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint", type=Path)
    ap.add_argument("onnx", type=Path)
    ap.add_argument("--height", type=int, default=294)
    ap.add_argument("--width", type=int, default=518)
    args = ap.parse_args()
    if args.height % 14 or args.width % 14:
        raise SystemExit("height and width must be multiples of 14")
    config = json.loads((args.checkpoint / "config.json").read_text())
    model = AutoModelForDepthEstimation.from_pretrained(args.checkpoint).eval()
    dummy = torch.randn(1, 3, args.height, args.width)
    with torch.no_grad():
        ref = DepthOnly(model)(dummy)
        torch.onnx.export(DepthOnly(model), (dummy,), str(args.onnx), opset_version=17,
                          input_names=["pixel_values"], output_names=["depth"], dynamo=False)
    meta = {"checkpoint": str(args.checkpoint), "height": args.height, "width": args.width,
            "max_depth": config.get("max_depth"), "output_shape": list(ref.shape),
            "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]}
    args.onnx.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
