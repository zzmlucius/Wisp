import contextlib
import csv
import io
import json
import os
from pathlib import Path
import random
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import hackbench_runner
from hackbench_runner import cli, cleanup, preflight
from hackbench_runner.results import FIELDS, parse_results, summarize
from compilebench.control import POLICIES
from compilebench.policy import PolicyError
from Schbench.tests import test_schtest as shared_doubles

FakeScheduler = shared_doubles.FakeScheduler
FakeBatch = shared_doubles.FakeBatch
HELP = "hackbench V 2.11\n--process --threads --pipe --groups --fds --loops --datasize"
WORKLOAD = {"mode": "process", "communication": "socket", "groups": 1, "fds": 2, "loops": 3, "datasize": 100}
TEXT = "Running in process mode with 1 groups using 4 file descriptors each (== 4 tasks)\nEach sender will pass 3 messages of 100 bytes\nTime: 0.123\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hacktest-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.program = self.root / "native"
        self.config = self.root / "hacktest.json"
        self.config.write_text(json.dumps({"program": str(self.program), "workload": WORKLOAD,
            "cpus": sorted(os.sched_getaffinity(0)), "results_dir": "records", "cooldown_s": 0,
            "sample_interval_s": 0.02, "timeout_s": 5,
            "schedulers": {name: {"command": [sys.executable], "ops": name[4:] + "_test"} for name in POLICIES[1:]}}))
        self.write_program()
        for module in [cli, cleanup]:
            p = patch.object(module, "PROJECT", self.root)
            p.start();self.addCleanup(p.stop)
        FakeBatch.fail = FakeBatch.fatal = FakeBatch.cancel = None
        FakeBatch.opens = 0

    def write_program(self, extra="", code=0, sleep=0.05, metric=True):
        self.program.write_text(f"#!{sys.executable}\nimport sys,os,time\nfrom pathlib import Path\n"
            f"if '--help' in sys.argv: print({HELP!r});sys.exit(0)\n"
            "def arg(k):return int(sys.argv[sys.argv.index(k)+1])\n"
            "g,f,l,s=arg('-g'),arg('-f'),arg('-l'),arg('-s')\n"
            "mode='threaded' if '-T' in sys.argv else 'process'\n"
            "print(f'Running in {mode} mode with {g} groups using {2*f} file descriptors each (== {2*g*f} tasks)',flush=True)\n"
            "print(f'Each sender will pass {l} messages of {s} bytes',flush=True)\n"
            f"{extra}\ntime.sleep({sleep})\n"
            + ("print('Time: 0.123')\n" if metric else "") + f"sys.exit({code})\n")
        self.program.chmod(0o755)

    def change(self, **values):
        data = json.loads(self.config.read_text());data.update(values)
        self.config.write_text(json.dumps(data))

    def batch(self):
        return next((self.root / "records").iterdir())

    def rows(self):
        with (self.batch() / "summary.csv").open() as stream:return list(csv.DictReader(stream))

    def run_fake(self, rounds=None, observer=None):
        with patch.object(cli, "Scheduler", return_value=observer or FakeScheduler()), patch.object(cli, "BatchScheduler", FakeBatch):
            return cli.experiment(cli.load_config(self.config), rounds=rounds, seed=42)

    def isolated_cli(self):
        original = Path(cli.__file__).resolve().parents[1]
        project = self.root / "isolated/Hackbench"
        project.mkdir(parents=True)
        for name in ['Compile','Schbench']:(project.parent / name).symlink_to(original.parent / name, target_is_directory=True)
        shutil.copytree(original / 'hackbench_runner', project / 'hackbench_runner',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(original/'hacktest',project/'hacktest')
        cfg=json.loads(self.config.read_text());cfg['results_dir']=str(self.root/'records')
        (project/'hacktest.json').write_text(json.dumps(cfg))
        return project/'hacktest'


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'hackbench.log';self.log.write_text(TEXT)

    def test_time_is_native_seconds_and_zero_is_not_invented(self):
        self.assertEqual(parse_results(self.log,WORKLOAD)[0]['hackbench_s'],0.123)
        self.log.write_text(TEXT.replace('0.123','0.000'))
        self.assertEqual(parse_results(self.log,WORKLOAD)[0]['hackbench_s'],0)

    def test_missing_duplicate_negative_and_nonfinite_time_fail(self):
        for text in [TEXT.replace('Time:','Other:'), TEXT+'Time: 0.2\n',TEXT.replace('0.123','nan'),TEXT.replace('0.123','inf'),TEXT.replace('0.123','-1')]:
            self.log.write_text(text)
            with self.subTest(text=text),self.assertRaises(ValueError):parse_results(self.log,WORKLOAD)

    def test_worker_failure_even_with_time_and_zero_exit_fails(self):
        for error in ['SENDER: write (error: Broken pipe)','SERVER: read (error: Resource temporarily unavailable)',
                      'Signal 15 caught, longjmp\'ing out!', 'No measurements available', '2 children started.  Expected 4', 'pthread_create failed()']:
            self.log.write_text(TEXT+error+'\n')
            with self.subTest(error=error),self.assertRaises(ValueError):parse_results(self.log,WORKLOAD)

    def test_fixed_work_echo_must_match(self):
        for text in [TEXT.replace('3 messages','4 messages'),TEXT.replace('1 groups','2 groups'),TEXT.replace('4 tasks','3 tasks'),TEXT.replace('process mode','threaded mode')]:
            self.log.write_text(text)
            with self.assertRaises(ValueError):parse_results(self.log,WORKLOAD)

    def test_real_rt_tests_2_11_success_and_signalled_output(self):
        fixtures=Path(__file__).parent/'fixtures'
        workload={**WORKLOAD,'groups':4,'fds':20,'loops':10000}
        value,_=parse_results(fixtures/'rt-tests-2.11-success.log',workload)
        self.assertEqual(value['hackbench_s'],3.302)
        with self.assertRaisesRegex(ValueError,'通信失败'):
            parse_results(fixtures/'rt-tests-2.11-signalled.log',WORKLOAD)


class ConfigTests(Fixture):
    def test_overrides_are_frozen_without_modifying_input(self):
        raw=self.config.read_bytes();cfg=cli.load_config(self.config,groups=4,loops=10,output=self.root/'custom')
        self.assertEqual(cfg['workload']['groups'],4);self.assertEqual(cfg['workload']['loops'],10)
        self.assertEqual(cfg['results_dir'],str(self.root/'custom'));self.assertEqual(raw,self.config.read_bytes())

    def test_all_mode_commands_omit_realtime(self):
        for mode in ['process','thread']:
            for transport in ['socket','pipe']:
                self.change(workload={**WORKLOAD,'mode':mode,'communication':transport})
                cmd=cli.native_command(cli.load_config(self.config))
                self.assertIn('-P' if mode=='process' else '-T',cmd)
                self.assertEqual('-p' in cmd,transport=='pipe')
                self.assertNotIn('-F',cmd);self.assertNotIn('--fifo',cmd)

    def test_invalid_arguments_and_overflow_rejected(self):
        for workload in [{**WORKLOAD,'groups':0},{**WORKLOAD,'loops':True},{**WORKLOAD,'mode':'fifo'},
                         {**WORKLOAD,'fifo':True},{**WORKLOAD,'communication':'inet'},
                         {**WORKLOAD,'groups':2**31-1},{**WORKLOAD,'loops':2**31-1,'fds':20}]:
            self.change(workload=workload)
            with self.subTest(workload=workload),self.assertRaises(ValueError):cli.load_config(self.config)

    def test_invalid_timeouts_and_cpu_rejected(self):
        original=self.config.read_bytes()
        for values in [{'timeout_s':0},{'timeout_s':float('inf')},{'sample_interval_s':0},{'cpus':'1023'}]:
            self.change(**values)
            with self.assertRaises(ValueError):cli.load_config(self.config)
            self.config.write_bytes(original)

    def test_other_workload_output_paths_rejected(self):
        for path in [self.root,self.root/'tests',self.root.parent/'Compile/bench/results',self.root.parent/'Schbench/bench/results']:
            with self.subTest(path=path),self.assertRaises(ValueError):cli.load_config(self.config,output=path)

    def test_effective_config_can_be_reused(self):
        c=cli.load_config(self.config);copy=self.root/'copy.json';copy.write_text(json.dumps(cli.effective(c)))
        self.assertEqual(cli.effective(c),cli.effective(cli.load_config(copy)))


class LimitTests(unittest.TestCase):
    def config(self,mode='process'):
        return {'workload':{**WORKLOAD,'groups':2,'fds':10,'mode':mode},'cgroup_parent':None}

    def limits(self,cfg,nofile=1000,nproc=100000,user=1,pidmax='max',current='0'):
        def getlimit(key):return (nofile,nofile) if key==resource.RLIMIT_NOFILE else (nproc,nproc)
        def reader(path):
            p=str(path)
            if p=='/proc/self/cgroup':return '0::/test'
            if p.endswith('pids.max'):return pidmax if '/test/' in p else 'max'
            if p.endswith('pids.current'):return current
            return None
        with patch.object(preflight.resource,'getrlimit',getlimit),patch.object(preflight,'user_tasks',return_value=user),patch.object(preflight,'read',reader):
            return preflight.check_limits(cfg)

    def test_process_and_thread_fd_requirements_differ(self):
        self.assertEqual(self.limits(self.config(),nofile=40)['fd_conservative_bound_per_process'],28)
        with self.assertRaisesRegex(ValueError,'文件描述符'):self.limits(self.config('thread'),nofile=40)

    def test_user_task_limit_is_checked(self):
        with self.assertRaisesRegex(ValueError,'进程/线程'):self.limits(self.config(),nproc=50,user=10)

    def test_cgroup_ancestor_limit_is_checked(self):
        with self.assertRaisesRegex(ValueError,'pids'):self.limits(self.config(),pidmax='80',current='60')

    def test_missing_current_count_for_finite_limit_rejected(self):
        with self.assertRaisesRegex(ValueError,'pids.current'):self.limits(self.config(),pidmax='80',current=None)


class RunTests(Fixture):
    def test_single_is_silent_and_schema_is_exact(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):self.assertEqual(self.run_fake(),0)
        self.assertEqual(output.getvalue(),'');row,=self.rows()
        self.assertEqual(list(row),FIELDS);self.assertEqual(row['run'],'1');self.assertEqual(row['valid'],'True')
        self.assertNotEqual(float(row['wall_s']),float(row['hackbench_s']))
        self.assertTrue((self.batch()/'hacktest.json').exists())

    def test_three_random_rounds_and_one_authorization(self):
        self.assertEqual(self.run_fake(rounds=3),0)
        m=json.loads((self.batch()/'metadata.json').read_text());rng=random.Random(42);orders=[]
        for _ in range(3):
            order=list(POLICIES);rng.shuffle(order);orders.append(order)
        self.assertEqual(m['orders'],orders);self.assertEqual(FakeBatch.opens,1)
        self.assertEqual([r['policy'] for r in self.rows()],sum(orders,[]))
        self.assertEqual(m['final_scheduler']['state'],'disabled')

    def test_cli_overrides_and_two_entry_modes(self):
        for args,count in [([],1),(['1'],4)]:
            dest=self.root/f'output{count}'
            with patch.object(cli,'Scheduler',FakeScheduler),patch.object(cli,'BatchScheduler',FakeBatch):
                self.assertEqual(cli.main([*args,'--groups','2','--loops','7','--output',str(dest)]),0)
            batch=next(dest.iterdir())
            self.assertEqual(json.loads((batch/'hacktest.json').read_text())['workload']['loops'],7)
            with (batch/'summary.csv').open() as f:self.assertEqual(len(list(csv.DictReader(f))),count)

    def test_unknown_external_scheduler_is_preserved(self):
        s=FakeScheduler();s.state.update(state='enabled',ops='unknown_test',switch_all='1');original=s.snapshot()
        self.assertEqual(self.run_fake(observer=s),0);self.assertEqual(s.snapshot(),original)
        self.assertTrue((self.batch()/'run-001/current/attempt.json').exists());self.assertEqual(FakeBatch.opens,0)

    def test_multi_rejects_external_scheduler_before_sudo(self):
        s=FakeScheduler();s.state.update(state='enabled',ops='rusty_test',switch_all='1')
        with self.assertRaises(PolicyError):self.run_fake(rounds=1,observer=s)
        self.assertEqual(FakeBatch.opens,0)

    def test_loading_failure_continues_after_recovery(self):
        FakeBatch.fail='scx_rusty';self.assertEqual(self.run_fake(rounds=1),1)
        self.assertEqual(len(self.rows()),4)
        row=next(r for r in self.rows() if r['policy']=='scx_rusty');self.assertEqual(row['wall_s'],'')
        self.assertEqual(row['valid'],'False')

    def test_controller_loss_terminates_remaining_schedule(self):
        FakeBatch.fatal='scx_bpfland';self.assertEqual(self.run_fake(rounds=2),1)
        self.assertEqual(len(self.rows()),1)

    def test_native_error_even_with_exit_zero_is_invalid(self):
        self.write_program(extra="print('SENDER: write (error: Broken pipe)')")
        self.assertEqual(self.run_fake(),1);self.assertIn('通信失败',self.rows()[0]['error'])

    def test_native_nonzero_and_missing_time_are_invalid(self):
        self.write_program(code=7,metric=False)
        self.assertEqual(self.run_fake(),1);row,=self.rows()
        self.assertEqual(row['exit_code'],'7');self.assertEqual(row['hackbench_s'],'')

    def test_timeout_records_failure_and_continues_without_reducing_work(self):
        self.change(timeout_s=0.03);self.write_program(sleep=3)
        self.assertEqual(self.run_fake(rounds=1),1);self.assertEqual(len(self.rows()),4)
        self.assertTrue(all('超时' in r['error'] and r['valid']=='False' for r in self.rows()))
        a=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        self.assertTrue(a['timeout']['expired']);self.assertEqual(a['command'][a['command'].index('-l')+1],'3')

    def test_configuration_drift_is_invalid(self):
        self.write_program(extra=f"Path({str(self.config)!r}).write_text('{{}}')")
        self.assertEqual(self.run_fake(),1);self.assertIn('固定输入',self.rows()[0]['error'])

    def test_binary_drift_before_attempt_prevents_execution(self):
        cfg=cli.load_config(self.config);original=cli.save_inputs
        def changed(*args):
            result=original(*args)
            self.program.write_text(self.program.read_text()+'\n# modified\n')
            return result
        with patch.object(cli,'Scheduler',FakeScheduler),patch.object(cli,'save_inputs',changed):
            self.assertEqual(cli.experiment(cfg),1)
        self.assertEqual(self.rows()[0]['wall_s'],'')

    def test_bad_rounds_and_cli_overrides_fail_before_authorization(self):
        for args in [['0'],['-1'],['1.2'],['--groups','0'],['--loops','-1']]:
            with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(cli.main(args),2)
        self.assertEqual(FakeBatch.opens,0)

    def test_timeout_kills_term_resistant_descendants_and_preserves_unrelated_process(self):
        self.change(timeout_s=0.12)
        pidfile=self.root/'timeout-child.pid'
        self.write_program(extra="import subprocess\n"+
            f"p=subprocess.Popen([{sys.executable!r},'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])\n"
            f"Path({str(pidfile)!r}).write_text(str(p.pid))",sleep=30)
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        try:
            self.assertEqual(self.run_fake(),1);self.assertIn('超时',self.rows()[0]['error'])
            self.assertIsNone(unrelated.poll());self.assertTrue(pidfile.exists())
            stat=Path(f'/proc/{int(pidfile.read_text())}/stat')
            if stat.exists():self.assertIn(stat.read_text().rsplit(')',1)[-1].split()[0],{'Z','X'})
        finally:
            unrelated.terminate();unrelated.wait()

    def test_interrupt_preserves_row_without_unattempted_rows(self):
        FakeBatch.cancel='scx_bpfland';self.assertEqual(self.run_fake(rounds=3),130)
        self.assertEqual(len(self.rows()),1)

    def test_cpu_affinity_applied_before_exec(self):
        cpu=min(os.sched_getaffinity(0));self.change(cpus=[cpu])
        self.write_program(extra=f'assert os.sched_getaffinity(0)=={{{cpu}}}')
        self.assertEqual(self.run_fake(),0)

    def test_real_cli_is_silent_and_preserves_default(self):
        if Path('/sys/kernel/sched_ext/state').read_text().strip()!='disabled':self.skipTest('requires default')
        p=subprocess.run([sys.executable,str(self.isolated_cli())],capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr.decode());self.assertEqual(p.stdout,b'');self.assertEqual(p.stderr,b'')
        self.assertEqual(Path('/sys/kernel/sched_ext/state').read_text().strip(),'disabled')

    def test_real_cli_interrupt_cleans_owned_workers_only(self):
        if Path('/sys/kernel/sched_ext/state').read_text().strip()!='disabled':self.skipTest('requires default')
        pidfile=self.root/'pid'
        self.write_program(extra="import subprocess\n"+f"p=subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(30)'])\nPath({str(pidfile)!r}).write_text(str(p.pid))",sleep=30)
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        p=subprocess.Popen([sys.executable,str(self.isolated_cli())],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not pidfile.exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(0.02)
            self.assertTrue(pidfile.exists());p.send_signal(signal.SIGINT);out,err=p.communicate(timeout=10)
            self.assertEqual(p.returncode,130,err.decode());self.assertEqual(out,b'');self.assertIsNone(unrelated.poll())
            stat=Path(f'/proc/{int(pidfile.read_text())}/stat')
            if stat.exists():self.assertIn(stat.read_text().rsplit(')',1)[-1].split()[0],{'Z','X'})
        finally:
            if p.poll() is None:p.kill();p.communicate()
            unrelated.terminate();unrelated.wait()


class AnalysisTests(unittest.TestCase):
    def test_pairing_skips_invalid_and_zero_baselines(self):
        rows=[{'policy':'default','run':1,'valid':True,'hackbench_s':2},
              {'policy':'scx_lavd','run':1,'valid':True,'hackbench_s':1.5},
              {'policy':'default','run':2,'valid':True,'hackbench_s':0},
              {'policy':'scx_lavd','run':2,'valid':True,'hackbench_s':0.1},
              {'policy':'scx_lavd','run':3,'valid':False,'hackbench_s':0.01}]
        data=summarize(rows)['policies']['scx_lavd']
        self.assertEqual(data['attempted'],3);self.assertEqual(data['valid'],2)
        self.assertEqual(data['paired'],[{'run':1,'improvement_pct':25}])


class CleanTests(Fixture):
    def record(self,path,workload='hackbench'):
        path.mkdir(parents=True);(path/'metadata.json').write_text(json.dumps({'workload':workload}));return path

    def test_preview_and_selected_cleanup_protect_other_workloads(self):
        own=self.record(self.root/'records/one');other=self.record(self.root/'records/foreign','schbench')
        custom=self.record(self.root/'custom/two');cleanup.register_root(custom.parent)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.main(['-c',str(self.config),'one','--dry-run']),0);self.assertTrue(own.exists())
            self.assertEqual(cleanup.main(['-c',str(self.config),'one','two']),0)
        self.assertFalse(own.exists());self.assertFalse(custom.exists());self.assertTrue(other.exists())
        self.assertTrue(self.program.exists());self.assertTrue(self.config.exists())

    def test_shared_lock_blocks_cleanup(self):
        lock=cli.acquire_lock()
        try:
            with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(cleanup.main(['-c',str(self.config)]),2)
        finally:os.close(lock)

    def test_unknown_batch_prevents_partial_delete(self):
        own=self.record(self.root/'records/one')
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(cleanup.main(['-c',str(self.config),'one','missing']),2)
        self.assertTrue(own.exists())

    def test_symlink_roots_and_unsafe_paths_rejected(self):
        dest=self.root/'dest';dest.mkdir();link=self.root/'link';link.symlink_to(dest,target_is_directory=True)
        for name in ['.',str(self.root.parent/'Schbench'),'link']:
            self.change(results_dir=name)
            with self.assertRaises(ValueError):cleanup.plan(self.config,[])


if __name__=='__main__':unittest.main()
