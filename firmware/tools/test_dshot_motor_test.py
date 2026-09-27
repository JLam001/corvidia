import unittest
import contextlib
import io
from unittest.mock import patch
from types import SimpleNamespace

import dshot_motor_test as tool
from pymavlink import mavutil


class DshotToolTests(unittest.TestCase):
    def test_v6_individual_and_all_four_keep_one_target_and_fixed_duration(self):
        for motor in (3, 'all'):
            with self.subTest(motor=motor):
                link = SimulatedLink(mode=tool.RAMP_MODE_ID)
                request = tool.plan(motor,25,30000,limit_percent=25,limit_duration_ms=30000)
                output = io.StringIO()
                with patch.object(mavutil,'mavlink_connection',return_value=link), \
                     patch.object(tool.time,'monotonic',side_effect=lambda:link.now), \
                     contextlib.redirect_stdout(output):
                    tool.execute('/fake',request,bench_ready=True,individual_stops_checked=True)
                test_command = tool.TEST_ALL_COMMAND if motor == 'all' else tool.TEST_COMMAND
                self.assertEqual(link.commands[0],(1,1,tool.ENABLE_COMMAND,0,1,7,25,30000,0,0,0))
                self.assertEqual(link.commands[1],(1,1,test_command,0,4 if motor=='all' else 3,25,30000,7,0,0,0))
                self.assertEqual(sum(c[2] in (tool.TEST_COMMAND,tool.TEST_ALL_COMMAND) for c in link.commands),1)
                self.assertIn('fixed 5000 ms startup ramp is included in the 30000 ms run',output.getvalue())
                self.assertFalse(link.heartbeat_after_stop)
                self.assertTrue(link.closed)

    def test_v6_short_run_does_not_wait_for_ramp_to_finish(self):
        link = SimulatedLink(mode=tool.RAMP_MODE_ID)
        output = io.StringIO()
        with patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now), \
             contextlib.redirect_stdout(output):
            tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
        self.assertIn('ends before the target is reached',output.getvalue())
        self.assertLess(link.command_times[2]-link.command_times[1],.3)
        self.assertEqual(link.commands[1][6],200)

    def test_v6_missing_wrong_or_stale_ramp_capability_cannot_enable(self):
        for invalid in ('missing',0,4999,5001,'stale'):
            with self.subTest(invalid=invalid):
                link = SimulatedLink(mode=tool.RAMP_MODE_ID)
                original = link.recv_match
                seen = [False]
                def invalid_capability(**kwargs):
                    msg = original(**kwargs)
                    if getattr(msg,'name',None) == 'DS_RAMPMS':
                        if invalid == 'missing' or (invalid == 'stale' and seen[0]):
                            msg.name = 'UNRELATED'
                        elif invalid != 'stale':
                            msg.value = invalid
                        seen[0] = True
                    return msg
                with patch.object(link,'recv_match',side_effect=invalid_capability), \
                     patch.object(mavutil,'mavlink_connection',return_value=link), \
                     patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
                    with self.assertRaisesRegex(RuntimeError,'Fresh v6 capability required: DS_RAMPMS'):
                        tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
                self.assertTrue(all(c[2]==tool.ENABLE_COMMAND and c[4:]==(0,)*7 for c in link.commands))

    def test_v6_identity_requires_v6_revision(self):
        link = SimulatedLink(mode=tool.RAMP_MODE_ID)
        original = link.recv_match
        def old_revision(**kwargs):
            msg = original(**kwargs)
            if getattr(msg,'name',None) == 'FW_REV': msg.value = 5
            return msg
        with patch.object(link,'recv_match',side_effect=old_revision), \
             patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            with self.assertRaisesRegex(RuntimeError,'FW_REV'):
                tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
        self.assertTrue(all(c[4:]==(0,)*7 for c in link.commands))

    def test_v6_stop_only_works_without_health_or_ramp_reports(self):
        link = FakeLink([message('HEARTBEAT',custom_mode=tool.RAMP_MODE_ID),
                         message('COMMAND_ACK',command=tool.ENABLE_COMMAND,result=0)])
        with patch.object(mavutil,'mavlink_connection',return_value=link):
            tool.execute('/fake',stop_only=True)
        self.assertTrue(link.commands)
        self.assertTrue(all(c==(1,1,tool.ENABLE_COMMAND,0,*([0]*7)) for c in link.commands))
        self.assertTrue(link.closed)

    def test_v6_lost_test_ack_does_not_retry_or_bypass_ramp(self):
        link = SimulatedLink(mode=tool.RAMP_MODE_ID,drop_test_ack=True)
        with patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            with self.assertRaises(TimeoutError):
                tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
        self.assertEqual(sum(c[2]==tool.TEST_COMMAND for c in link.commands),1)
        self.assertEqual(link.commands[-1],(1,1,tool.ENABLE_COMMAND,0,*([0]*7)))

    def test_v6_byte_names_preserve_capability_checks(self):
        link = SimulatedLink(mode=tool.RAMP_MODE_ID)
        original = link.recv_match
        def byte_names(**kwargs):
            msg = original(**kwargs)
            if getattr(msg,'name',None): msg.name = msg.name.encode().ljust(10,b'\0')
            return msg
        with patch.object(link,'recv_match',side_effect=byte_names), \
             patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
        self.assertEqual(sum(c[2]==tool.TEST_COMMAND for c in link.commands),1)

    def test_explicit_envelope_supports_full_range_and_one_minute(self):
        p=tool.plan('all',100,60000,limit_percent=100,limit_duration_ms=60000)
        self.assertEqual(p['dshot_value'],2047)
        for kwargs in [dict(limit_percent=101), dict(limit_percent=float('nan')),
                       dict(limit_duration_ms=60001), dict(limit_duration_ms=True)]:
            with self.assertRaises(ValueError):
                tool.plan(1,1,200,**kwargs)
        with self.assertRaises(ValueError):
            tool.plan(1,50,60000,limit_percent=40,limit_duration_ms=60000)

    def test_v5_encodes_envelope_and_only_one_long_test(self):
        link=SimulatedLink(mode=tool.MODE_ID)
        request=tool.plan('all',50,60000,limit_percent=50,limit_duration_ms=60000)
        with patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            tool.execute('/fake',request,bench_ready=True,individual_stops_checked=True)
        self.assertEqual(link.commands[0],(1,1,tool.ENABLE_COMMAND,0,1,7,50,60000,0,0,0))
        self.assertEqual(link.commands[1],(1,1,tool.TEST_ALL_COMMAND,0,4,50,60000,7,0,0,0))
        self.assertEqual(sum(c[2]==tool.TEST_ALL_COMMAND for c in link.commands),1)
        self.assertEqual(link.commands[-1][4:],(0,)*7)

    def test_extended_request_cannot_enable_legacy_firmware(self):
        link=SimulatedLink()
        request=tool.plan(1,10,1000,limit_percent=10,limit_duration_ms=1000)
        with patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            with self.assertRaises(RuntimeError):
                tool.execute('/fake',request,bench_ready=True)
        self.assertTrue(all(c[2]==tool.ENABLE_COMMAND and c[4:]==(0,)*7 for c in link.commands))

    def test_missing_v5_capability_cannot_enable(self):
        link=SimulatedLink(mode=tool.MODE_ID)
        original=link.recv_match
        def missing(**kwargs):
            msg=original(**kwargs)
            if getattr(msg,'name',None)=='DS_MAXMS': msg.value=500
            return msg
        with patch.object(link,'recv_match',side_effect=missing), \
             patch.object(mavutil,'mavlink_connection',return_value=link), \
             patch.object(tool.time,'monotonic',side_effect=lambda:link.now):
            with self.assertRaises(RuntimeError):
                tool.execute('/fake',tool.plan(1,1,200),bench_ready=True)
        self.assertTrue(all(c[4:]==(0,)*7 for c in link.commands))

    def test_bounds_and_units(self):
        p = tool.plan(4, 1, 200)
        self.assertEqual((p['dshot_value'], p['feather_pin']), (67, 5))
        self.assertEqual(tool.plan(1, 5, 500)['dshot_value'], 147)
        for args in [(0, 1, 20), (5, 1, 20), (1.5, 1, 20), (True, 1, 20),
                     (1, 0, 20), (1, 5.01, 20), (1, float('nan'), 20),
                     (1, float('inf'), 20), (1, True, 20),
                     (1, 1, 19), (1, 1, 501), (1, 1, 20.5)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                tool.plan(*args)

    def test_all_plan_requires_explicit_selection_and_keeps_caps(self):
        p = tool.plan('all', 1, 200)
        self.assertEqual(p['feather_pin'], [9, 10, 6, 5])
        self.assertEqual(p['dshot_value'], 67)
        for args in [('all', 5.01, 200), ('all', 1, 501), ('ALL', 1, 200)]:
            with self.assertRaises(ValueError):
                tool.plan(*args)

    def test_all_uses_its_own_command_and_one_token(self):
        link = SimulatedLink()
        with patch.object(mavutil, 'mavlink_connection', return_value=link), \
             patch.object(tool.time, 'monotonic', side_effect=lambda: link.now):
            tool.execute('/fake', tool.plan('all', 1, 200), bench_ready=True, individual_stops_checked=True)
        self.assertEqual(link.commands[1], (1, 1, tool.TEST_ALL_COMMAND, 0, 4, 1, 200, 7, 0, 0, 0))
        self.assertEqual(sum(c[2] == tool.TEST_ALL_COMMAND for c in link.commands), 1)
        self.assertFalse(any(c[2] == tool.TEST_COMMAND for c in link.commands))
        self.assertEqual(link.commands[-1][4:], (0,)*7)

    def test_old_firmware_without_all_capability_is_not_enabled(self):
        link = SimulatedLink(all_capable=False)
        with patch.object(mavutil, 'mavlink_connection', return_value=link), \
             patch.object(tool.time, 'monotonic', side_effect=lambda: link.now):
            with self.assertRaises(RuntimeError):
                tool.execute('/fake', tool.plan('all', 1, 200), bench_ready=True, individual_stops_checked=True)
        self.assertTrue(all(c[2] == tool.ENABLE_COMMAND and c[4:] == (0,)*7 for c in link.commands))

    def test_missing_readiness_prevents_opening_serial(self):
        with patch.object(mavutil, 'mavlink_connection') as connect:
            with self.assertRaises(RuntimeError):
                tool.execute('/not-a-device', tool.plan(1, 1, 200))
            connect.assert_not_called()

    def test_all_requires_individual_stop_observations_before_opening_serial(self):
        with patch.object(mavutil, 'mavlink_connection') as connect:
            with self.assertRaises(RuntimeError):
                tool.execute('/not-a-device', tool.plan('all', 1, 200), bench_ready=True)
            connect.assert_not_called()

    def test_nonzero_initial_status_prevents_enable(self):
        link = SimulatedLink()
        original = link.recv_match
        def active(**kwargs):
            msg = original(**kwargs)
            if getattr(msg, 'name', None) == 'M4_DS':
                msg.value = 67
            return msg
        with patch.object(link, 'recv_match', side_effect=active), \
             patch.object(mavutil, 'mavlink_connection', return_value=link), \
             patch.object(tool.time, 'monotonic', side_effect=lambda: link.now):
            with self.assertRaises(RuntimeError):
                tool.execute('/fake', tool.plan(1, 1, 200), bench_ready=True)
        self.assertTrue(all(c[2] == tool.ENABLE_COMMAND and c[4:] == (0,)*7 for c in link.commands))

    def test_wrong_image_rejected_without_commands(self):
        link = FakeLink([message('HEARTBEAT', custom_mode=0x42454E43)])
        with patch.object(mavutil, 'mavlink_connection', return_value=link):
            with self.assertRaises(RuntimeError):
                tool.execute('/fake', stop_only=True)
        self.assertEqual(link.commands, [])
        self.assertTrue(link.closed)

    def test_stop_only_never_enables_or_sends_throttle(self):
        link = FakeLink([message('HEARTBEAT', custom_mode=tool.MODE_ID),
                         message('COMMAND_ACK', command=tool.ENABLE_COMMAND, result=0)])
        with patch.object(mavutil, 'mavlink_connection', return_value=link):
            tool.execute('/fake', stop_only=True)
        self.assertTrue(link.commands)
        for command in link.commands:
            self.assertEqual(command, (1, 1, tool.ENABLE_COMMAND, 0, *([0]*7)))
        self.assertTrue(link.closed)

    def test_enabled_simulation_uses_token_percent_duration_and_final_stop(self):
        link = SimulatedLink()
        with patch.object(mavutil, 'mavlink_connection', return_value=link), \
             patch.object(tool.time, 'monotonic', side_effect=lambda: link.now):
            tool.execute('/fake', tool.plan(3, 1, 200), bench_ready=True)
        self.assertEqual(link.commands[:2], [
            (1, 1, tool.ENABLE_COMMAND, 0, 1, 7, 0, 0, 0, 0, 0),
            (1, 1, tool.TEST_COMMAND, 0, 3, 1, 200, 7, 0, 0, 0)])
        self.assertTrue(all(c[4:] == (0,)*7 for c in link.commands[2:]))
        self.assertTrue(link.closed)

    def test_lost_test_ack_is_not_retried_and_stop_is_sent(self):
        link = SimulatedLink(drop_test_ack=True)
        with patch.object(mavutil, 'mavlink_connection', return_value=link), \
             patch.object(tool.time, 'monotonic', side_effect=lambda: link.now):
            with self.assertRaises(TimeoutError):
                tool.execute('/fake', tool.plan(1, 1, 200), bench_ready=True)
        self.assertEqual(sum(c[2] == tool.TEST_COMMAND for c in link.commands), 1)
        self.assertEqual(link.commands[-1], (1, 1, tool.ENABLE_COMMAND, 0, *([0]*7)))
        self.assertTrue(link.closed)


def message(kind, **values):
    return SimpleNamespace(get_type=lambda: kind, get_srcSystem=lambda: 1,
                           get_srcComponent=lambda: 1, **values)


class FakeLink:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.commands = []
        self.closed = False
        self.mav = SimpleNamespace(command_long_send=lambda *args: self.commands.append(args))

    def recv_match(self, **kwargs):
        return next(self.messages, None)

    def close(self):
        self.closed = True


class SimulatedLink(FakeLink):
    def __init__(self, drop_test_ack=False, all_capable=True, mode=tool.LEGACY_MODE_ID):
        super().__init__([])
        self.now = 0
        self.index = 0
        self.pending = []
        self.drop_test_ack = drop_test_ack
        self.all_capable = all_capable
        self.mode = mode
        self.command_times = []
        self.stop_sent = False
        self.heartbeat_after_stop = False
        self.mav = SimpleNamespace(command_long_send=self.command, heartbeat_send=self.heartbeat)

    def heartbeat(self, *args):
        if self.stop_sent: self.heartbeat_after_stop = True

    def command(self, *args):
        self.commands.append(args)
        self.command_times.append(self.now)
        if args[2] == tool.ENABLE_COMMAND and args[4:] == (0,)*7: self.stop_sent = True
        if not (args[2] == tool.TEST_COMMAND and self.drop_test_ack):
            self.pending.append(message('COMMAND_ACK', command=args[2], result=0))

    def recv_match(self, **kwargs):
        self.now += .02
        if self.pending:
            return self.pending.pop(0)
        values = [('DS_ALLOW', 1), ('DS_OUTPUT', 1), ('DS_FAULT', 0), ('IMU_OK', 1),
                  ('DS_TOKEN', 7), ('DS_ALL', int(self.all_capable)),
                  ('DS_ACTIVE', 0), ('M1_DS', 0), ('M2_DS', 0), ('M3_DS', 0), ('M4_DS', 0),
                  ('FW_REV',6 if self.mode==tool.RAMP_MODE_ID else 5),
                  ('DS_MAXP',100), ('DS_MAXMS',60000), ('FLT_READY',0)]
        if self.mode == tool.RAMP_MODE_ID: values.append(('DS_RAMPMS',5000))
        self.index += 1
        if self.index % (len(values)+1) == 1:
            return message('HEARTBEAT', custom_mode=self.mode)
        name, value = values[(self.index-2) % (len(values)+1)]
        return message('NAMED_VALUE_INT', name=name, value=value)


if __name__ == '__main__':
    unittest.main()
