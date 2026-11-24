import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from Tests.Finegraintest import queue


class FinegrainQueueTests(unittest.TestCase):
    RPS = (250, 500, 750, 1000)

    def queue(self, fatal=False, cancelled=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'logs').mkdir()
            results = root / 'results'
            jobs = [{'name': f'14-14-{r}-1', 'command': ['mixtest', '3', f'-{r}'],
                     'config_file': f'{r}.json', 'config': {'rps': r, 'results_dir': str(results)}}
                    for r in self.RPS]
            calls = []
            def experiment(cfg, interrupts, rounds, on_round_complete):
                calls.append((cfg['rps'], rounds))
                batch = results / str(cfg['rps'])
                batch.mkdir(parents=True)
                metadata = {'status': 'failed', 'attempts': list(range(12)),
                    'fatal_error': 'controller failed' if fatal else None,
                    'final_scheduler': {'state': 'disabled'},
                    'scheduler_controller': {'status': 'closed', 'exit_code': 0}}
                (batch / 'metadata.json').write_text(json.dumps(metadata))
                return 130 if cancelled else 1
            auth, guard, interrupts = Mock(), Mock(info={'status': 'held'}), Mock()
            with patch.object(queue.mixed, 'experiment', side_effect=experiment), \
                 patch.object(queue, 'verify_queue') as verify, \
                 patch.object(queue, 'batch_log', side_effect=lambda p: contextlib.nullcontext()), \
                 contextlib.redirect_stderr(io.StringIO()):
                code = queue.run_queue(jobs, root, auth, guard, {'batch_rest_s': 0}, interrupts)
                checks = verify.call_count
            return code, calls, json.loads((root / 'status.json').read_text()), auth.check.call_count, checks

    def test_p99_invalid_does_not_stop_four_batches_or_reauthenticate(self):
        code, calls, status, auth, checks = self.queue()
        self.assertEqual(code, 1)
        self.assertEqual(calls, [(r, 3) for r in self.RPS])
        self.assertEqual(status['status'], 'completed_with_invalid_results')
        self.assertEqual((auth, checks), (4, 4))
        self.assertTrue(all(j['result_directories'] and j['exit_code'] == 1 for j in status['jobs']))

    def test_controller_failure_stops_pending_jobs(self):
        code, calls, status, _, _ = self.queue(fatal=True)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(j['status'] == 'pending' for j in status['jobs'][1:]))

    def test_cancel_keeps_results_and_stops_queue(self):
        code, calls, status, _, _ = self.queue(cancelled=True)
        self.assertEqual(code, 130)
        self.assertEqual(status['status'], 'interrupted')
        self.assertEqual(len(calls), 1)
        self.assertTrue(status['jobs'][0]['result_directories'])

    def test_scheduler_preflight_always_closes_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = Mock()
            controller.info = {'status': 'closed', 'exit_code': 0}
            controller.select.side_effect = RuntimeError('cannot load')
            with patch.object(queue, 'scheduler_specs', return_value={}), \
                 patch.object(queue, 'BatchScheduler', return_value=controller):
                with self.assertRaisesRegex(RuntimeError, 'cannot load'):
                    queue.scheduler_preflight({'scheduler_timeout_s': 30, 'scheduler_settle_s': 5},
                        Path(temporary) / 'probe', Mock())
            controller.close.assert_called_once()

    def test_guard_failure_blocks_batch_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard = Mock()
            guard.check.side_effect = RuntimeError('screen lock lost')
            with self.assertRaisesRegex(RuntimeError, 'screen lock lost'):
                queue.verify_queue({}, Path(temporary), guard)

    def fake_gi(self, fail_system=False):
        calls = []
        class Variant:
            def __init__(self, signature, value):
                self.value = value
            def unpack(self):
                return self.value
        def session_call(*args):
            method = args[3]
            calls.append(method)
            return Variant('', (17,) if method == 'Inhibit' else (True,) if method == 'IsInhibited' else ())
        session = Mock(call_sync=Mock(side_effect=session_call))
        base_fd = os.open('/dev/null', os.O_RDONLY)
        self.addCleanup(os.close, base_fd)
        descriptors = Mock(get=Mock(side_effect=lambda index: os.dup(base_fd)))
        system = Mock()
        if fail_system:
            system.call_with_unix_fd_list_sync.side_effect = RuntimeError('logind denied')
        else:
            system.call_with_unix_fd_list_sync.return_value = (Variant('', (0,)), descriptors)
        gio = SimpleNamespace(BusType=SimpleNamespace(SESSION=1, SYSTEM=2),
            DBusCallFlags=SimpleNamespace(NONE=0), bus_get_sync=lambda kind, unused: session if kind == 1 else system)
        glib = SimpleNamespace(Variant=Variant, VariantType=SimpleNamespace(new=lambda s: s))
        modules = {'gi': Mock(), 'gi.repository': SimpleNamespace(Gio=gio, GLib=glib)}
        return modules, calls

    def test_guard_holds_fd_and_session_cookie_then_releases(self):
        modules, calls = self.fake_gi()
        with patch.dict(sys.modules, modules):
            with queue.DesktopGuard() as guard:
                descriptor = guard.fd
                os.fstat(descriptor)
                self.assertEqual(guard.info['status'], 'held')
                guard.check()
            self.assertEqual(guard.info['status'], 'released')
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(calls[0], 'Inhibit')
        self.assertEqual(calls[-1], 'Uninhibit')

    def test_partial_guard_failure_releases_gnome_cookie(self):
        modules, calls = self.fake_gi(fail_system=True)
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'logind denied'):
                with queue.DesktopGuard():
                    self.fail('must not start work')
        self.assertEqual(calls, ['Inhibit', 'Uninhibit'])


if __name__ == '__main__':
    unittest.main()
