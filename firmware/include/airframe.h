#pragma once
#include "quad_mixer.h"

// User-confirmed mounted positions (2026-09-27), camera points forward:
//     4 (front-left)       2 (front-right)
//     3 (rear-left)        1 (rear-right)
// Electrical outputs: ESC1 -> D9, ESC2 -> D10, ESC3 -> D6, ESC4 -> D5.
// User now confirms unchanged positions and corrected rotation viewed from
// above: 1/4 CW, 2/3 CCW. Keep flight integration gated pending body-axis and
// controller correction checks; this helper does not authorize motor output.
inline QuadLayout plannedAirframe() {
    QuadLayout l;
    l.frontLeft = 3; l.frontRight = 1; l.rearLeft = 2; l.rearRight = 0;
    l.yawSign[0] = -1; l.yawSign[1] = 1;
    l.yawSign[2] = 1; l.yawSign[3] = -1;
    return l; // verified=false: mixQuadX still rejects this candidate layout
}
