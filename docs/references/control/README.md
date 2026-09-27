# User-provided control references

Copied from Downloads on September 27, 2026:

- `HANDOFF-2026-09-27.md`: original `HANDOFF.md`, describing the perception project.
  It describes commits and uncommitted Jetson work beyond this checkout. This is
  a reference snapshot, not proof that those files or measurements exist locally.
- `user-wiring-1.png`: `ChatGPT Image Sep 27, 2026, 01_17_20 AM.png`.
- `user-wiring-2.png`: `ChatGPT Image Sep 27, 2026, 01_17_36 AM.png`.
- `user-wiring-3.png`: `ChatGPT Image Sep 27, 2026, 01_17_44 AM.png`.

The images are annotated/generated references, not an electrical inspection.
Their arrows and pin labels conflict in places. In particular, Feather D11/D12
do not support hardware PWM. Use the verified manufacturer pin assignments and
the correction table in [MOTOR_CONTROL.md](../../../firmware/MOTOR_CONTROL.md).

User clarifications:

- ESC identified as Flywoo GOKU G45M AM32; see [ESC-G45M-AM32.md](ESC-G45M-AM32.md).

- Propellers removed; user subsequently confirmed all four motors mounted or clamped.
- Initial wiring: ESC V disconnected; optional C and T connected. T was
  subsequently disconnected for the DShot tests and remains disconnected in
  the current stand demo.
- ESC channels 1/3 are planned at the rear; 1/2 on the right, facing forward
  from behind the camera. Thus FL=4, FR=2, RL=3, RR=1.
- User confirmed moving ESC 3 from Feather 11 to 6, and ESC 4 from 12 to 5.
  Electrical continuity has not been independently measured.

The user separately requested a low-speed motor test in the conversation.
The live MAVLink check is recorded in `bench-check-2026-09-27.json`; that
read-only check does not verify physical ESC response or measured motor RPM.
