#!/usr/bin/env bash
# Run the Cosmos Reason 2 2B confirmer: llama.cpp llama-server in the Jetson AI Lab
# container, with pinned weights, bound to 127.0.0.1:8010.
#
# Sized for RAM headroom on the 8 GB Orin Nano (measured 2026-09-27):
#   Q4_K_M server ~2.9 GB (Q8_0 was 3.8 GB); full stack left >= 1.7 GB available.
#   -c 2048 fits a 448 px crop (<= ~300 tokens) with room to spare; one slot; no
#   RAM prompt cache (llama-server defaults to 4 slots, 8192 ctx, 8 GB cache).
#   Explicit 512/128 logical/physical token batches bound inference buffers;
#   defaults of 2048/512 left too little room for the app-only graphical session.
#   Camera + GUI + final appearance replays left about 2.4 GiB available locally.
#
# Usage: deploy/cosmos_server.sh [fetch|start|stop|status]
set -euo pipefail

IMAGE=ghcr.io/nvidia-ai-iot/llama_cpp@sha256:f7c67c102b08252e963f9e5f92c3a36554c8f69305eb7ea257c6cd12e24c3191
REPO=Kbenkhaled/Cosmos-Reason2-2B-GGUF   # community GGUF linked by Jetson AI Lab
REV=6efd567dd99d4cf9d54cc80ea38e878ae25700dc
MODEL=Cosmos-Reason2-2B-Q4_K_M.gguf
MODEL_SHA=3ed011641891e81fe654328071de251718832e7deafef579033485d33082e4fd
MMPROJ=mmproj-Cosmos-Reason2-2B-F16.gguf
MMPROJ_SHA=8d3284c340d6a9c9237d56f2fc42f2df50d3761f539f3f01be964cefb0b73916
DIR=$HOME/models/cosmos-reason2-2b

fetch() {
  mkdir -p "$DIR"
  for pair in "$MODEL:$MODEL_SHA" "$MMPROJ:$MMPROJ_SHA"; do
    f=${pair%%:*}; sha=${pair##*:}
    if [ ! -f "$DIR/$f" ] || ! echo "$sha  $DIR/$f" | sha256sum -c --quiet; then
      curl -fL --retry 3 -o "$DIR/$f.part" "https://huggingface.co/$REPO/resolve/$REV/$f"
      echo "$sha  $DIR/$f.part" | sha256sum -c --quiet
      mv "$DIR/$f.part" "$DIR/$f"
    fi
  done
  echo "weights OK in $DIR"
}

start() {
  fetch
  sudo docker rm -f cosmos >/dev/null 2>&1 || true
  sudo docker run -d --name cosmos --restart unless-stopped --runtime=nvidia --network host \
    -v "$DIR:/models:ro" "$IMAGE" \
    llama-server -m "/models/$MODEL" --mmproj "/models/$MMPROJ" \
      --host 127.0.0.1 --port 8010 -c 2048 -np 1 -cram 0 -fa on -b 512 -ub 128 \
      --reasoning-budget 0 --no-webui
  for _ in $(seq 90); do
    curl -sf -m 1 http://127.0.0.1:8010/health >/dev/null && { echo "cosmos healthy"; return; }
    sleep 2
  done
  echo "cosmos did not become healthy; see: sudo docker logs cosmos" >&2
  exit 1
}

case "${1:-start}" in
  fetch) fetch ;;
  start) start ;;
  stop) sudo docker rm -f cosmos ;;
  status) sudo docker ps --filter name=cosmos; curl -s -m 2 http://127.0.0.1:8010/health; echo ;;
  *) echo "usage: $0 [fetch|start|stop|status]"; exit 2 ;;
esac
