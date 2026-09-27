import unittest
from pymavlink import mavutil
import mission_request as tool


class MissionRequestTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(nominal_collective=.5, motor_ceiling=1, duration_ms=60000)
        self.intent = dict(action='tilt_forward', amount=4, duration_ms=500)

    def test_full_authority_and_selectable_duration_encode_without_execution(self):
        p = tool.preview(self.config, self.intent, session_id=12, sequence=9)
        self.assertFalse(p['execution_available'])
        self.assertEqual(p['attitude_demand']['pitch_deg'], -4)
        messages = []
        for wire in p['packets']:
            decoder = mavutil.mavlink.MAVLink(None)
            messages.append(decoder.parse_char(bytes.fromhex(wire['hex'])))
        begin, move = messages
        self.assertEqual((begin.get_srcSystem(), begin.get_srcComponent()), (255, 190))
        self.assertEqual((begin.command, begin.param1, begin.param2, begin.param3, begin.param4),
                         (mavutil.mavlink.MAV_CMD_USER_5, 12, .5, 1, 60000))
        self.assertEqual((move.get_srcSystem(), move.get_srcComponent()), (42, 191))
        self.assertEqual((move.command, move.param1, move.param2, move.param3, move.param4, move.param5),
                         (mavutil.mavlink.MAV_CMD_USER_4, 1, 4, 500, 12, 9))

    def test_llm_cannot_set_power_arm_session_or_refresh(self):
        for change in [dict(throttle=.5), dict(motor_ceiling=1), dict(action='arm'),
                       dict(session_id=2), dict(sequence=4), dict(action='keepalive'),
                       dict(duration_ms=60000), dict(action='hover')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                tool.preview(self.config, self.intent | change)

    def test_no_unbounded_duration_nonfinite_or_no_correction_headroom(self):
        for change in [dict(duration_ms=0), dict(duration_ms=1800001), dict(duration_ms=True),
                       dict(motor_ceiling=1.1), dict(nominal_collective=1),
                       dict(nominal_collective=float('nan')), dict(nominal_collective=10**1000),
                       dict(motor_ceiling=True)]:
            with self.subTest(change=str(change)[:80]), self.assertRaises(ValueError):
                tool.preview(self.config | change, self.intent)

    def test_stop_is_a_supervisor_operation_not_motor_disarm(self):
        p = tool.preview(self.config, {'action': 'stop_mission'})
        self.assertEqual(p['packets'], [])
        self.assertEqual(p['supervisor_operation'], 'end_session_and_request_flight_fallback')
        self.assertFalse(p['airborne_fallback_implemented'])

    def test_exact_wire_ids_and_strict_fields(self):
        for session, seq in [(0,1),(True,1),(16777216,1),(1,16777216),(1,-1)]:
            with self.assertRaises(ValueError):
                tool.preview(self.config, self.intent, session, seq)
        with self.assertRaises(ValueError):
            tool.preview(self.config | {'unlimited':True}, self.intent)
        with self.assertRaises(ValueError):
            tool.preview(self.config, {'action':'stop_mission', 'throttle':0})


if __name__ == '__main__':
    unittest.main()
