import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
REAL = HERE
sys.path.insert(0, str(REAL / 'Compile'))
sys.path.insert(0, str(REAL / 'Schbench'))
sys.path.insert(0, str(REAL / 'Mixbench'))
loader = importlib.machinery.SourceFileLoader('nighttest', str(HERE / 'nighttest'))
spec = importlib.util.spec_from_loader(loader.name, loader)
night = importlib.util.module_from_spec(spec)
loader.exec_module(night)
spec = importlib.util.spec_from_file_location('compilebench_runner.night_control', HERE / 'Compile/compilebench_runner/control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class NightTests(unittest.TestCase):
    def test_jobs_only_include_four_mixed_batches(self):
        from compilebench_runner import cli as compile_cli
        from mixbench_runner import cli as mix_cli, configuration
        with patch.object(configuration, 'load_config', side_effect=lambda path, **kw: {'rps': kw['rps']}) as load, \
             patch.object(compile_cli, 'scheduler_specs'), \
             patch.object(compile_cli, 'load_config', side_effect=AssertionError('must not load compile config')):
            jobs = night.load_jobs()
        self.assertEqual(len(jobs), 4)
        self.assertEqual([job['command'] for job in jobs],
                         [['./Mixbench/mixtest', '3', f'-{r}'] for r in (500, 1000, 1500, 2000)])
        self.assertTrue(all(job['experiment'] is mix_cli.experiment for job in jobs))
        self.assertEqual(load.call_count, 4)

    def metadata(self):
        return {'attempts': [str(i) for i in range(12)], 'fatal_error': None,
                'status': 'completed', 'final_scheduler': {'state': 'disabled'},
                'scheduler_controller': {'status': 'closed', 'exit_code': 0}}

    def test_only_initial_authentication_can_prompt(self):
        with io.StringIO() as log, patch.object(night.subprocess, 'run') as run:
            with night.Authorization(log) as authorization:
                authorization.check()
            calls = [c.args[0] for c in run.call_args_list]
            self.assertEqual(calls, [['sudo', '-v'], ['sudo', '-n', '-v'], ['sudo', '-n', '-v']])
            for call in run.call_args_list[1:]:
                self.assertEqual(call.kwargs['stdin'], night.subprocess.DEVNULL)

    def test_authorization_failure_stops_without_prompt(self):
        with io.StringIO() as log:
            authorization = night.Authorization(log)
            authorization.failure = 'expired'
            with patch.object(night.subprocess, 'run') as run:
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
        self.assertTrue(night.can_continue(0, m))
        m['status'] = 'failed'
        self.assertTrue(night.can_continue(1, m))
        for key, value in [('fatal_error', 'controller lost'), ('attempts', []),
                           ('final_scheduler', {'state': 'enabled'}),
                           ('scheduler_controller', {'status': 'closed', 'exit_code': 1})]:
            changed = self.metadata();changed[key] = value
            with self.subTest(key=key):self.assertFalse(night.can_continue(1, changed))

    def queue(self, first_fatal=False):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root = Path(temp.name);directory=root/'queue';directory.mkdir()
        jobs = [{'name': name, 'command': command, 'config': {'results_dir':str(root/name)}}
                for name, command in [(f'mix-rps-{r}', ['./Mixbench/mixtest','3',f'-{r}'])
                                      for r in (500,1000,1500,2000)]]
        calls=[]
        def run_batch(job):
            calls.append(job['command']);p=Path(job['config']['results_dir'])/'batch';p.mkdir(parents=True)
            m=self.metadata();m['status']='failed'
            if first_fatal:m['fatal_error']='supervisor failed'
            (p/'metadata.json').write_text(json.dumps(m))
            return 1
        class Session:
            def check(self):pass
        with patch.object(night, 'run_batch', side_effect=run_batch), \
             patch.object(night, 'batch_log', side_effect=lambda path: contextlib.nullcontext()), \
             contextlib.redirect_stderr(io.StringIO()):
            code=night.run_queue(jobs,directory,Session())
        return code,calls,json.loads((directory/'status.json').read_text())

    def test_all_four_batches_run_in_order_even_when_results_invalid(self):
        code,calls,status=self.queue()
        self.assertEqual(code,1);self.assertEqual(status['status'],'completed_with_invalid_results')
        self.assertEqual(calls, [['./Mixbench/mixtest','3',f'-{r}'] for r in (500,1000,1500,2000)])
        self.assertTrue(all(len(j['result_directories'])==1 and j['exit_code']==1 for j in status['jobs']))

    def test_fatal_error_stops_remaining_jobs_and_preserves_pending(self):
        code,calls,status=self.queue(first_fatal=True)
        self.assertEqual(code,2);self.assertEqual(len(calls),1)
        self.assertEqual(status['jobs'][0]['status'],'failed')
        self.assertTrue(all(j['status']=='pending' for j in status['jobs'][1:]))


if __name__ == '__main__':
    unittest.main()
