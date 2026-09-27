import contextlib
import io
import sys
import types
import unittest
import re
from pathlib import Path
from unittest.mock import patch

import motor_test
import motion_request


class Message:
    def __init__(self, kind, **kwargs):
        self.kind = kind
        self.__dict__.update(kwargs)
    def get_srcSystem(self): return 1
    def get_srcComponent(self): return 1


class FakeLink:
    def __init__(self, mode=motor_test.MODE_ID, ready=True, drop_motor_ack=False):
        self.mode, self.ready, self.drop_motor_ack = mode, ready, drop_motor_ack
        self.commands, self.pending = [], []
        self.closed = False
        self.mav = self
    def heartbeat_send(self, *args): pass
    def command_long_send(self, system, component, command, confirmation, *params):
        self.commands.append((command, params))
        if not (self.drop_motor_ack and command == motor_test.MOTOR_TEST_COMMAND):
            self.pending.append(Message("COMMAND_ACK", command=command, result=0))
    def recv_match(self, type=None, **kwargs):
        if type == "HEARTBEAT":
            return Message("HEARTBEAT", custom_mode=self.mode, system_status=3 if self.ready else 0)
        if self.pending:
            return self.pending.pop(0)
        return None
    def close(self): self.closed = True


class ToolTests(unittest.TestCase):
    def test_wire_command_ids_match_firmware_mavlink_headers(self):
        enums=(Path(__file__).resolve().parents[1]/"include/mavlink/common/common.h").read_text()
        for name, actual in [("MAV_CMD_USER_1",motor_test.ENABLE_COMMAND),
                             ("MAV_CMD_DO_MOTOR_TEST",motor_test.MOTOR_TEST_COMMAND)]:
            self.assertEqual(int(re.search(r"\b"+name+r"=(\d+)",enums).group(1)),actual)
    def test_motor_mapping(self):
        self.assertEqual([motor_test.plan(i, 1050, 1)["feather_pin"] for i in range(1, 5)], [9, 10, 6, 5])
    def test_motor_invalid_inputs(self):
        for args in [(0,1050,1),(5,1050,1),(True,1050,1),(1,1101,1),(1,1050,3),(1,1050,float("nan"))]:
            with self.subTest(args=args), self.assertRaises(ValueError): motor_test.plan(*args)
    def test_motion_preview(self):
        r=motion_request.preview(dict(action="tilt_forward", amount=5, duration_ms=500, sequence=1))
        self.assertFalse(r["execution_available"])
        self.assertEqual(r["attitude_demand"]["pitch_deg"],-5)
    def test_llm_cannot_request_arm_throttle_or_extra_fields(self):
        base=dict(action="level", amount=0, duration_ms=500, sequence=1)
        for extra in [dict(action="arm"),dict(action="hover"),dict(throttle=0.5),dict(duration_ms=True),dict(amount=float("inf")),dict(amount=10**1000),dict(sequence=-1)]:
            with self.subTest(extra=str(extra)[:80]), self.assertRaises(ValueError):
                motion_request.validate(base | extra)
    def run_fake(self, link, stop_only=False):
        mavutil=types.SimpleNamespace(
            mavlink_connection=lambda *a,**kw:link,
            mavlink=types.SimpleNamespace(MAV_TYPE_GCS=6,MAV_AUTOPILOT_INVALID=8,
                                         MAV_RESULT_ACCEPTED=0,MAV_STATE_UNINIT=0))
        clock=[0.0]
        def now(): clock[0]+=0.025; return clock[0]
        with patch.dict(sys.modules,{"pymavlink":types.SimpleNamespace(mavutil=mavutil)}), \
             patch.object(motor_test, "SPIN_TESTS_ENABLED", not stop_only), \
             patch.object(motor_test.time,"monotonic",now), contextlib.redirect_stdout(io.StringIO()):
            motor_test.execute("FAKE",motor_test.plan(1,1050,0.2),stop_only)
    def test_execute_single_test_then_stop(self):
        link=FakeLink(); self.run_fake(link)
        self.assertEqual(sum(c==209 for c,p in link.commands),1)
        self.assertEqual(link.commands[-1],(motor_test.ENABLE_COMMAND,(0,)*7))
        self.assertTrue(link.closed)
    def test_hardware_failure_blocks_tests_before_opening_serial(self):
        with self.assertRaisesRegex(RuntimeError, "disabled after the motor-4 stop failure"):
            motor_test.execute("MUST_NOT_OPEN", motor_test.plan(1,1020,.5))
    def test_wrong_firmware_sends_no_commands(self):
        link=FakeLink(mode=0)
        with self.assertRaises(RuntimeError): self.run_fake(link)
        self.assertEqual(link.commands,[])
        self.assertTrue(link.closed)
    def test_disabled_output_never_enables(self):
        link=FakeLink(ready=False)
        with self.assertRaises(RuntimeError): self.run_fake(link)
        self.assertTrue(all(c==motor_test.ENABLE_COMMAND and p[0]==0 for c,p in link.commands))
    def test_missing_ack_stops_without_retry(self):
        link=FakeLink(drop_motor_ack=True)
        with self.assertRaises(TimeoutError): self.run_fake(link)
        self.assertEqual(sum(c==209 for c,p in link.commands),1)
        self.assertEqual(link.commands[-1][1],(0,)*7)
    def test_stop_only_does_not_enable_or_start(self):
        link=FakeLink(); self.run_fake(link,True)
        self.assertTrue(all(c==motor_test.ENABLE_COMMAND and p==(0,)*7 for c,p in link.commands))


if __name__ == "__main__": unittest.main()
