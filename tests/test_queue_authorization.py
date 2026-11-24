import io
import os
import tempfile
import unittest
from unittest.mock import patch

from Finegraintest import queue
from compilebench_runner import control


class QueueAuthorizationTests(unittest.TestCase):
    def metadata(self):
        return {'attempts': [str(i) for i in range(12)], 'fatal_error': None,
                'status': 'completed', 'final_scheduler': {'state': 'disabled'},
                'scheduler_controller': {'status': 'closed', 'exit_code': 0}}

    def test_only_initial_authentication_can_prompt(self):
        with io.StringIO() as log, patch.object(queue.subprocess, 'run') as run:
            with queue.Authorization(log) as authorization:
                authorization.check()
            calls = [c.args[0] for c in run.call_args_list]
            self.assertEqual(calls, [['sudo', '-v'], ['sudo', '-n', '-v'], ['sudo', '-n', '-v']])
            for call in run.call_args_list[1:]:
                self.assertEqual(call.kwargs['stdin'], queue.subprocess.DEVNULL)

    def test_authorization_failure_stops_without_prompt(self):
        with io.StringIO() as log:
            authorization = queue.Authorization(log)
            authorization.failure = 'expired'
            with patch.object(queue.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, '非交互刷新失败'):
                    authorization.check()
                run.assert_not_called()

    def test_controller_noninteractive_mode_and_original_mode(self):
        from compilebench_runner.interrupts import Interrupts
        specs = {name: {'command': ['/bin/true']} for name in control.POLICIES[1:]}
        for value, expected in [('1', ['sudo', '-n', '-v']), ('0', ['sudo', '-v'])]:
            with self.subTest(mode=value), tempfile.TemporaryDirectory() as tmp:
                scheduler = control.BatchScheduler(specs, tmp, Interrupts(), inhibit=False)
                with patch.dict(os.environ, {'WISP_SUDO_NONINTERACTIVE': value}), \
                     patch.object(scheduler, 'initial', return_value={'state': 'disabled'}), \
                     patch.object(control.subprocess, 'run') as run, \
                     patch.object(control.subprocess, 'Popen', side_effect=RuntimeError('spawn sentinel')):
                    with self.assertRaisesRegex(RuntimeError, 'spawn sentinel'):
                        scheduler.open()
                    self.assertEqual(run.call_args.args[0], expected)
                if scheduler.log:scheduler.log.close()

    def test_incomplete_and_fatal_batches_cannot_continue(self):
        m = self.metadata()
        self.assertTrue(queue.can_continue(0, m))
        m['status'] = 'failed'
        self.assertTrue(queue.can_continue(1, m))
        for key, value in [('fatal_error', 'controller lost'), ('attempts', []),
                           ('final_scheduler', {'state': 'enabled'}),
                           ('scheduler_controller', {'status': 'closed', 'exit_code': 1})]:
            changed = self.metadata();changed[key] = value
            with self.subTest(key=key):self.assertFalse(queue.can_continue(1, changed))

if __name__ == '__main__':
    unittest.main()
