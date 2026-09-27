import math
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pymavlink.dialects.v20 import common as mav
from imu_viz import Telemetry, detect_port


def decoded(msg, system=1, component=1, v1=False):
    encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
    return mav.MAVLink(None).parse_char(msg.pack(encoder, force_mavlink1=v1))


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.state = Telemetry()
        self.state.connected = True

    def attitude(self, angle=math.pi/2):
        return mav.MAVLink_attitude_message(1234, angle, 0, 0, 0, math.pi, 0)

    def test_real_wire_packet_v1_and_v2_radians(self):
        for v1 in (False, True):
            self.state.consume(decoded(self.attitude(), v1=v1), 10)
            s = self.state.snapshot(10.1)
            self.assertAlmostEqual(s['attitude']['roll'], 90, places=4)
            self.assertAlmostEqual(s['attitude']['gy'], 180, places=4)
            self.assertTrue(s['fresh'])
            self.assertEqual(s['attitude']['boot_ms'], 1234)

    def test_stale_and_disconnected(self):
        self.state.consume(decoded(self.attitude()), 10)
        self.assertFalse(self.state.snapshot(10.6)['fresh'])
        self.state.connected = False
        self.assertFalse(self.state.snapshot(10.1)['fresh'])

    def test_wrong_source_and_target_do_not_become_measurements(self):
        self.state.consume(decoded(self.attitude(), system=2), 10)
        self.state.consume(decoded(mav.MAVLink_attitude_target_message(1, 0, [1,0,0,0], 0,0,0,.3)), 10)
        self.assertIsNone(self.state.snapshot(10)['attitude'])

    def test_nan_rejected_and_does_not_refresh(self):
        self.state.consume(decoded(self.attitude(float('nan'))), 10)
        self.assertIsNone(self.state.snapshot(10)['attitude'])
        self.assertEqual(self.state.invalid, 1)

    def test_reconnect_clears_old_measurements(self):
        self.state.consume(decoded(self.attitude()), 10)
        self.state.reset()
        self.assertIsNone(self.state.snapshot(11)['attitude'])
        self.assertEqual(self.state.snapshot(11)['history'], [])

    def test_heartbeat_mode_and_health_report_age(self):
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x42454E43,3,3)), 10)
        self.assertTrue(self.state.snapshot(10.2)['heartbeat']['bench'])
        self.assertFalse(self.state.snapshot(10.2)['heartbeat']['armed'])
        self.assertAlmostEqual(self.state.snapshot(10.2)['heartbeat_age'], .2)

    def test_autodetect_never_falls_back_to_unrelated_device(self):
        with patch('imu_viz.list_ports.comports', return_value=[SimpleNamespace(vid=None,pid=None,device='bluetooth')]):
            self.assertIsNone(detect_port())
        board=SimpleNamespace(vid=0x0483,pid=0x5740,device='stm32')
        with patch('imu_viz.list_ports.comports', return_value=[board,board]):
            self.assertRaises(RuntimeError, detect_port)

    def test_dshot_health_and_input_values_are_separate_from_pwm_and_rpm(self):
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x4453494D,3,3)), 10)
        for name, value in [('IMU_OK', 1), ('M4_DS', 67), ('DS_ALLOW', 0), ('DS_TOKEN', 12)]:
            self.state.consume(decoded(mav.MAVLink_named_value_int_message(123, name.encode(), value)), 10)
        s = self.state.snapshot(10.2)
        self.assertTrue(s['heartbeat']['dshot_imu'])
        self.assertFalse(s['heartbeat']['bench'])
        self.assertEqual(s['named_values']['M4_DS']['value'], 67)
        self.assertAlmostEqual(s['named_values']['IMU_OK']['age'], .2)
        self.assertNotIn('SERVO_OUTPUT_RAW', s['reports'])

    def test_v5_reports_capabilities_separately_from_authorized_limits(self):
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x44534935,3,3)),10)
        for name,value in [('FW_REV',5),('DS_MAXP',100),('DS_MAXMS',60000),
                           ('DS_CAP_BP',500),('DS_CAP_MS',500),('FLT_READY',0),('AXES_OK',0)]:
            self.state.consume(decoded(mav.MAVLink_named_value_int_message(123,name.encode(),value)),10)
        s=self.state.snapshot(10.2)
        self.assertTrue(s['heartbeat']['dshot_imu'])
        self.assertEqual(s['named_values']['DS_MAXMS']['value'],60000)
        self.assertEqual(s['named_values']['DS_CAP_MS']['value'],500)
        self.assertEqual(s['named_values']['FLT_READY']['value'],0)

    def test_v6_identity_and_ramp_report_are_read_only_telemetry(self):
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x44534936,3,3)),10)
        for name,value in [('FW_REV',6),('DS_RAMPMS',5000),('DS_CAP_BP',2500),('M1_DS',297)]:
            self.state.consume(decoded(mav.MAVLink_named_value_int_message(123,name.encode(),value)),10)
        s=self.state.snapshot(10.2)
        self.assertTrue(s['heartbeat']['dshot'])
        self.assertTrue(s['heartbeat']['dshot_imu'])
        self.assertTrue(s['heartbeat']['dshot_ramp'])
        self.assertFalse(s['heartbeat']['armed'])
        self.assertEqual(s['named_values']['DS_RAMPMS']['value'],5000)
        self.assertAlmostEqual(s['named_values']['DS_RAMPMS']['age'],.2)
        self.assertEqual(s['named_values']['M1_DS']['value'],297)
        self.assertEqual(s['named_values']['DS_CAP_BP']['value'],2500)

    def test_v5_does_not_claim_ramp_and_reconnect_clears_old_v6_capability(self):
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x44534936,3,3)),10)
        self.state.consume(decoded(mav.MAVLink_named_value_int_message(123,b'DS_RAMPMS',5000)),10)
        self.state.reset()
        self.state.consume(decoded(mav.MAVLink_heartbeat_message(2,8,0,0x44534935,3,3)),11)
        s=self.state.snapshot(11.2)
        self.assertTrue(s['heartbeat']['dshot_imu'])
        self.assertFalse(s['heartbeat']['dshot_ramp'])
        self.assertNotIn('DS_RAMPMS',s['named_values'])


if __name__ == '__main__':
    unittest.main()
