"""Evaluate the Cosmos confirmer on a labeled crop set and check its failure handling.

    uv run corvidia-cosmos-eval --dataset ~/corvidia-data/eval/starter --out report.json
    uv run corvidia-cosmos-eval --checks          # cancellation, overflow, unavailable

Images go through the same encoding as live events (BGR crop -> JPEG, longest
side capped by `confirm.max_image_side`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from .config import PipelineConfig, load_config
from .confirmer import normalize
from .cosmos import LlamaCppBackend, LlamaCppConfig
from .evidence import encode_jpeg
from .records import ConfirmRequest

# Expected answers per label; "partial" accepts anything and is reported separately.
EXPECTED = {"person": {"confirmed"}, "not_person": {"rejected"}, "depiction": {"rejected"}}


def pct(values, qs=(50, 95, 99)) -> dict:
    if not values:
        return {f"p{q}": None for q in qs}
    return {f"p{q}": round(float(np.percentile(values, q)), 1) for q in qs}


def run_one(backend: LlamaCppBackend, jpeg: bytes, prompt: str, request_id: str,
            deadline_s: float) -> tuple[str, str, dict, float]:
    t = time.monotonic()
    fut = backend.submit(ConfirmRequest(request_id, jpeg, prompt))
    try:
        fut.result(timeout=deadline_s)
    except Exception:  # noqa: BLE001 - normalize reports the failure
        pass
    elapsed = (time.monotonic() - t) * 1000
    if not fut.done():
        backend.cancel(request_id)
        return "unknown", "timeout", {}, elapsed
    result, reason, details = normalize(fut)
    return result.value, reason, details, elapsed


def evaluate(backend: LlamaCppBackend, cfg: PipelineConfig, dataset: Path, limit: int | None,
             deadline_s: float) -> dict:
    records = [json.loads(line) for line in (dataset / "labels.jsonl").read_text().splitlines()]
    records = [r for r in records if r["label"] != "unlabeled"][:limit]
    rows = []
    for i, r in enumerate(records):
        rgb = np.asarray(Image.open(dataset / r["file"]).convert("RGB"))
        jpeg = encode_jpeg(rgb, cfg.confirm.jpeg_quality, "rgb", cfg.confirm.max_image_side)
        result, reason, details, ms = run_one(backend, jpeg, cfg.confirm.prompt, f"eval-{i}",
                                              deadline_s)
        usage = details.get("usage") or {}
        timings = details.get("timings") or {}
        rows.append({**r, "result": result, "reason": reason, "latency_ms": round(ms, 1),
                     "prompt_tokens": usage.get("prompt_tokens"),
                     "completion_tokens": usage.get("completion_tokens"),
                     "prompt_ms": timings.get("prompt_ms"), "predicted_ms": timings.get("predicted_ms"),
                     "raw_output": details.get("raw_output")})
        print(f"{i + 1}/{len(records)} {r['label']:10s} -> {result:9s} {ms:7.0f} ms  {r['file']}",
              flush=True)

    by_label: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_label[row["label"]][row["result"]] += 1
    scored = [row for row in rows if row["label"] in EXPECTED]
    correct = sum(row["result"] in EXPECTED[row["label"]] for row in scored)
    positives = [row for row in scored if row["label"] == "person"]
    negatives = [row for row in scored if row["label"] != "person"]
    confirmed = [row for row in scored if row["result"] == "confirmed"]
    true_pos = sum(row["label"] == "person" for row in confirmed)
    return {
        "dataset": str(dataset),
        "backend": backend.info.__dict__,
        "prompt": cfg.confirm.prompt,
        "max_image_side": cfg.confirm.max_image_side,
        "n": len(rows),
        "confusion": {k: dict(v) for k, v in sorted(by_label.items())},
        "accuracy_on_scored": round(correct / len(scored), 3) if scored else None,
        "person_recall": round(true_pos / len(positives), 3) if positives else None,
        "confirm_precision": round(true_pos / len(confirmed), 3) if confirmed else None,
        "false_confirm_rate_on_negatives": (
            round(sum(r["result"] == "confirmed" for r in negatives) / len(negatives), 3)
            if negatives else None),
        "unknown_rate": round(sum(r["result"] == "unknown" for r in rows) / len(rows), 3) if rows else None,
        "reasons": dict(Counter(r["reason"] for r in rows)),
        "latency_ms": pct([r["latency_ms"] for r in rows]),
        "prompt_tokens": pct([r["prompt_tokens"] for r in rows if r["prompt_tokens"]]),
        "prompt_eval_ms": pct([r["prompt_ms"] for r in rows if r["prompt_ms"]]),
        "rows": rows,
    }


def checks(backend: LlamaCppBackend, cfg: PipelineConfig) -> dict:
    """Failure-path checks against the live server."""
    out: dict[str, object] = {}

    # 1. Cancellation: a long free-text generation, cancelled mid-flight.
    image = np.random.default_rng(0).integers(0, 255, (320, 240, 3), dtype=np.uint8)
    jpeg = encode_jpeg(image, 90, "rgb")
    req = ConfirmRequest("check-cancel", jpeg, "Describe this image in exhaustive detail.")
    payload = backend.build_payload(req)
    payload.pop("response_format")
    payload["max_tokens"] = 512
    original = backend.build_payload
    backend.build_payload = lambda _r: payload  # type: ignore[method-assign]
    try:
        fut = backend.submit(req)
        time.sleep(1.0)
        busy_before = backend.is_idle() is False
        t = time.monotonic()
        acked = backend.cancel(req.request_id)
        out["cancel"] = {"busy_before_cancel": busy_before, "acknowledged": acked,
                         "ack_ms": round((time.monotonic() - t) * 1000, 1),
                         "idle_after": backend.is_idle()}
        try:
            fut.result(timeout=15)
        except Exception as e:  # noqa: BLE001
            out["cancel"]["future"] = type(e).__name__  # type: ignore[index]
    finally:
        backend.build_payload = original  # type: ignore[method-assign]

    # 2. Context overflow: an image far beyond the configured context.
    big = np.random.default_rng(1).integers(0, 255, (2160, 3840, 3), dtype=np.uint8)
    result, reason, details, ms = run_one(backend, encode_jpeg(big, 90, "rgb"), cfg.confirm.prompt,
                                          "check-overflow", 60.0)
    out["oversized_image"] = {"result": result, "reason": reason, "ms": round(ms, 1),
                              "prompt_tokens": (details.get("usage") or {}).get("prompt_tokens"),
                              "error": details.get("error")}

    # 3. Unavailable server.
    dead = LlamaCppBackend(LlamaCppConfig(url="http://127.0.0.1:9"))
    result, reason, _, _ = run_one(dead, jpeg, cfg.confirm.prompt, "check-dead", 5.0)
    out["unavailable"] = {"result": result, "reason": reason}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path)
    ap.add_argument("--checks", action="store_true")
    ap.add_argument("--url", default=LlamaCppConfig.url)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--max-image-side", type=int)
    ap.add_argument("--prompt", help="override confirm.prompt for this evaluation")
    ap.add_argument("--deadline", type=float, default=10.0,
                    help="per-request limit for evaluation (not the deployed deadline)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    cfg = load_config(args.config) if args.config else PipelineConfig()
    if args.max_image_side is not None:
        cfg = dataclasses.replace(cfg, confirm=dataclasses.replace(
            cfg.confirm, max_image_side=args.max_image_side))
    if args.prompt:
        cfg = dataclasses.replace(cfg, confirm=dataclasses.replace(cfg.confirm, prompt=args.prompt))
    backend = LlamaCppBackend(LlamaCppConfig(url=args.url))
    if not backend.healthy():
        raise SystemExit(f"llama-server not healthy at {args.url}")
    report: dict = {"backend": backend.info.__dict__}
    if args.dataset:
        report["eval"] = evaluate(backend, cfg, args.dataset, args.limit, args.deadline)
    if args.checks:
        report["checks"] = checks(backend, cfg)
    summary = {k: v for k, v in report.get("eval", {}).items() if k != "rows"}
    print(json.dumps({"eval": summary, "checks": report.get("checks")}, indent=2, default=str))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
