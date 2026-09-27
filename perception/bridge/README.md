# Corvidia -> autodrone bridge

`corvidia_bridge.py` is the process that lets the autodrone mission loop run on top of this
pipeline. `corvidia-run` owns the camera and the GPU models and publishes two UDP streams; the
bridge turns them, plus the evidence store, into the bus topics the framework's nodes consume.

| Bus topic | Source in Corvidia | Notes |
|---|---|---|
| `perc.freespace` | `corvidia-run --freespace-hz 10 --freespace-udp 127.0.0.1:5601` | FreeSpace fields forwarded unchanged, stamped at Corvidia's publish time |
| `perc.tracks` | `corvidia-run --tracks-udp 127.0.0.1:5602` | PersonTracks fields unchanged: normalized boxes, id, conf, age (processed frames seen), depth_m (torso median from the newest depth map) |
| `health.perception` | liveness of the tracks stream | stale after `stale_s` (1 s); free-space staleness only appears in the detail, because the mission guard judges free-space age itself |
| `health.camera` | frame ids advancing in the tracks stream; Corvidia `/health` faults `camera`, `source`, `memory_low` when `health_url` is set | |
| `capture.event` | each completed `event.json` in the newest session under `events_root` | paths: frame.jpg, crop.jpg, best.jpg (waits up to `best_grace_s` for the best shot); `verified` = yes / no / unknown from Cosmos, `unverified` with a stub; events completed before the bridge started are not replayed |
| `cam.frame` (optional) | Corvidia's MJPEG preview (`--preview-port 8080`, `preview_url = "http://127.0.0.1:8080/stream"`) | JPEG frames at `frame_hz`; they carry the drawn boxes, so they serve the CAPTURE burst and the context frame, not clean evidence |

## Running it on the Jetson

```sh
# Corvidia side (its own venv)
cd ~/corvidia/perception
uv run corvidia-run --camera --freespace-hz 10 --stub-delay 0 \
    --freespace-udp 127.0.0.1:5601 --tracks-udp 127.0.0.1:5602 --preview-port 8080

# autodrone side (its environment: pyzmq + the autodrone package)
cd ~/autodrone
python -m autodrone.launch --profile configs/host-jetson.toml --only broker,fc,control,mission
python ~/corvidia/perception/bridge/corvidia_bridge.py --profile configs/host-jetson.toml
```

Do not start the framework's `camera` or `perception` nodes: both would open the same Argus
sensor and run a second detector. The profile may carry a `[corvidia_bridge]` section (every key
optional; defaults shown in the module docstring): ports, `stale_s`, `events_root`, `session`,
`publish_capture`, `best_grace_s`, `health_url`, `preview_url` (the `/stream` endpoint), `frame_hz`. Set `depth_kind = "metric"` in the
profile's perception section if anything on the framework side still assumes the relative model;
the free-space thresholds (`d_stop` 3 m, `d_free` 6 m) are applied on the Corvidia side.

What the mission then sees, in pilot-assisted mode (autodrone `states.py` / `guards.py`):
entering SCOUT needs healthy FC, PERCEPTION and CAMERA; SCOUT and CENTER hold while PERCEPTION
stays healthy and `perc.freespace` stays fresh (older than `depth_stale_s` for `depth_stale_grace_s`
means STOP); `all_close` is not a state change but a controller backoff; a track with `age >= 3`
moves SCOUT to CENTER; CAPTURE needs CAMERA and reads `cam.frame` for its burst (or logs "no camera
frames" and falls to COOLDOWN after 3 s if `preview_url` is unset) while `capture.event` carries
Corvidia's own evidence. With `send_commands = false` nothing reaches the board.

## Tests

`test_bridge.py` covers everything that does not need the bus: UDP receive, datagram
conversion, liveness and heartbeat rules, event tailing across sessions, JPEG dimension parsing
and MJPEG splitting. It runs with the package suite (`uv run pytest -q`; `bridge` is in
`testpaths`). The node itself imports autodrone only when started.

The laptop harness can drive the whole chain without the Jetson:
`laptop-eval/tools/mimic_pipeline.py ... --freespace-udp 127.0.0.1:5601 --tracks-udp 127.0.0.1:5602`,
then the framework's broker and this bridge from the autodrone environment.
