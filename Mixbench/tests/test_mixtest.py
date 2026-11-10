import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import mixbench_runner
from mixbench_runner import cli, cleanup, configuration, results
from mixbench_runner import observation
from compilebench_runner.control import POLICIES
from compilebench_runner.policy import PolicyError, Scheduler as RealScheduler
from compilebench_runner.system import digest, write_json

sys.path.insert(0, str(Path(mixbench_runner.__file__).resolve().parents[2]))
from Schbench.tests.test_schtest import FakeBatch, FakeScheduler

FRONT_HELP = "schbench -m -t -r -F -n -s -w -z -i -R -L -j --json"
BACK_HELP = "--threads --time --events --rate --warmup-time --report-interval"
BACK_LOG = "CPU speed:\n    events per second: 123.45\nGeneral statistics:\n    total time: 1.0001s\n    total number of events: 123\n"


def front_log(runtime=1, rps=1000, request=250):
    return (f"Wakeup Latencies percentiles (usec) runtime {runtime} (s) (100 total samples)\n"
        "* 99.0th: 7 (99 samples)\n"
        f"Request Latencies percentiles (usec) runtime {runtime} (s) (100 total samples)\n"
        f"* 99.0th: {request} (99 samples)\naverage rps: {rps}\n")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mixtest-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.front, self.back = self.root / "front", self.root / "back"
        self.provenance = self.root / "provenance.json"
        self.config = self.root / "mixtest.json"
        for module in (cli, configuration, cleanup):
            p = patch.object(module, "PROJECT", self.root)
            p.start();self.addCleanup(p.stop)
        # Worker and frozen source files must still come from the staged package.
        (self.root / "mixbench_runner").symlink_to(Path(mixbench_runner.__file__).resolve().parent, target_is_directory=True)
        self.write_front()
        self.write_back()
        self.config.write_text(json.dumps({"front": {"program": str(self.front), "provenance": str(self.provenance),
            "message_threads": 1, "worker_threads": 2}, "back": {"program": str(self.back), "threads": 2},
            "runtime_s": 1, "cpus": sorted(os.sched_getaffinity(0)), "rps": 1000, "latency_limit_us": 300,
            "cooldown_s": 0, "sample_interval_s": 0.02, "timeout_s": 3, "results_dir": "records",
            "schedulers": {p: {"command": [sys.executable], "ops": p[4:] + "_test"} for p in POLICIES[1:]}}))
        FakeBatch.fail = FakeBatch.fatal = FakeBatch.cancel = None
        FakeBatch.opens = 0

    def script(self, path, text):
        path.write_text(f"#!{sys.executable}\n" + text)
        path.chmod(0o755)

    def write_front(self, extra="", sleep=1):
        self.script(self.front, "import sys,time,json,os\nfrom pathlib import Path\n"
            f"if '--help' in sys.argv:print({FRONT_HELP!r});sys.exit(0)\n"
            f"{extra}\ntime.sleep({sleep})\n"
            "runtime=int(sys.argv[sys.argv.index('-r')+1])\n"
            f"print({front_log()!r}.replace('runtime 1 ',f'runtime {{runtime}} '),flush=True)\n"
            "if '-j' in sys.argv:Path(sys.argv[sys.argv.index('-j')+1]).write_text(json.dumps({'normal':{},'int':{'runtime':runtime,'request_latency_pct99.0':250,'wakeup_latency_pct99.0':7}}))\n")
        write_json(self.provenance, {"commit": configuration.FRONT_COMMIT, "binary_sha256": digest(self.front)})

    def write_back(self, extra="", sleep=1):
        self.script(self.back, "import sys,time,os\nfrom pathlib import Path\n"
            "if '--version' in sys.argv:print('sysbench 1.0.20');sys.exit(0)\n"
            f"if '--help' in sys.argv:print({BACK_HELP!r});sys.exit(0)\n"
            "if 'help' in sys.argv:print('--cpu-max-prime');sys.exit(0)\n"
            f"{extra}\ntime.sleep({sleep})\nprint({BACK_LOG!r},flush=True)\n")

    def change(self, **values):
        cfg = json.loads(self.config.read_text());cfg.update(values);self.config.write_text(json.dumps(cfg))

    def cfg(self, case="mixed", **kwargs):
        return configuration.load_config(self.config, case=case, **kwargs)

    def batch(self):
        return next((self.root / "records").iterdir())

    def rows(self):
        with (self.batch() / "summary.csv").open() as f:return list(csv.DictReader(f))

    def run_fake(self, case="mixed", rounds=None, measured=False, observer=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "Scheduler", return_value=observer or (RealScheduler() if measured else FakeScheduler())))
            stack.enter_context(patch.object(cli, "BatchScheduler", FakeBatch))
            if not measured:
                stack.enter_context(patch.object(cli, "measure", self.measured_stub))
            return cli.experiment(self.cfg(case), rounds=rounds, seed=42)

    def measured_stub(self, command, directory, environment, log, samples, *args, **kwargs):
        spec = json.loads((directory / 'launch.json').read_text())
        children = {}
        for side in spec['commands']:
            (directory / f'{side}.log').write_text(front_log() if side == 'front' else BACK_LOG)
            children[side] = {'pid': 123, 'exec_monotonic_s': 10., 'exit_monotonic_s': 11.001,
                              'exit_code': 0, 'reaped': True}
        if 'front' in children:
            write_json(directory / 'front-native.json', {'normal': {}, 'int': {'runtime':1,'request_latency_pct99.0':250,'wakeup_latency_pct99.0':7}})
        write_json(directory / 'execution.json', {'children':children,'temp_start_c':40})
        with samples.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=cli.SAMPLE_FIELDS);w.writeheader()
            for t,temp in [(0,99),(0.6,45),(1.6,99)]:w.writerow({'relative_s':t,'temp_c':temp})
        return {'valid':True,'exit_code':0,'wall_s':1.5,'cpu_time_s':2.,'peak_memory_bytes':123,
            'temp_start_c':40,'temp_peak_sampled_c':99,'error':''}, {'started_monotonic_s':9.5,'missing_metrics':{}}, False

    def isolated_cli(self):
        original=Path(mixbench_runner.__file__).resolve().parents[1]
        project=self.root/'isolated/Mixbench';project.mkdir(parents=True)
        for name in ['Compile','Schbench']:(project.parent/name).symlink_to(original.parent/name,target_is_directory=True)
        shutil.copytree(original/'mixbench_runner',project/'mixbench_runner',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(original/'mixtest',project/'mixtest')
        cfg=json.loads(self.config.read_text());cfg['results_dir']=str(self.root/'records')
        (project/'mixtest.json').write_text(json.dumps(cfg))
        return project/'mixtest'


class ArgumentTests(Fixture):
    def test_observation_option_reaches_configuration(self):
        cfg = self.cfg()
        with patch.object(cli.os, "getuid", return_value=1000), \
             patch.object(cli, "acquire_lock", return_value=None), \
             patch.object(cli, "load_config", return_value=cfg) as load, \
             patch.object(cli, "experiment", return_value=0):
            self.assertEqual(cli.main(["3", "-500", "--observe-wakeup"]), 0)
            self.assertTrue(load.call_args.kwargs["observe_wakeup"])
            self.assertEqual(load.call_args.args[2], 500)

    def test_rps_spellings_and_rounds(self):
        for argv in [["1", "-1000"], ["1", "-n", "1000"], ["1", "-n1000"],
                     ["1", "--rps", "1000"], ["-1000", "1"]]:
            with self.subTest(argv=argv):
                args = cli.parse_arguments(argv)
                self.assertEqual((args.rounds, args.rps), ("1", 1000))
        args = cli.parse_arguments(["-750"])
        self.assertEqual((args.rounds, args.rps), (None, 750))

    def test_shorthand_preserves_other_option_values_and_separator(self):
        args = cli.parse_arguments(["3", "-1200", "--case", "front", "--bg-threads", "7", "--output", "custom"])
        self.assertEqual((args.rounds, args.rps, args.case, args.bg_threads, args.output),
                         ("3", 1200, "front", 7, Path("custom")))
        args = cli.parse_arguments(["1", "--rps", "-1000", "--bg-threads", "-7", "--output", "-900"])
        self.assertEqual((args.rps, args.bg_threads, args.output), (-1000, -7, Path("-900")))
        args = cli.parse_arguments(["--", "-1000"])
        self.assertEqual((args.rounds, args.rps), ("-1000", None))

    def test_main_passes_rps_override_without_changing_input(self):
        raw = self.config.read_bytes()
        for argv, rounds, rps in [(["1", "-1200"], 1, 1200), (["-n", "750"], None, 750)]:
            with self.subTest(argv=argv), patch.object(cli.os, "getuid", return_value=1000), \
                 patch.object(cli, "acquire_lock", return_value=None), \
                 patch.object(cli, "experiment", return_value=0) as experiment:
                self.assertEqual(cli.main(argv), 0)
                cfg = experiment.call_args.args[0]
                self.assertEqual(cfg["rps"], rps)
                self.assertEqual(experiment.call_args.kwargs["rounds"], rounds)
                cmd = configuration.commands(cfg, self.root)["front"]
                self.assertEqual(cmd[cmd.index("-R") + 1], str(rps))
                self.assertEqual(self.config.read_bytes(), raw)

    def test_invalid_rps_does_not_start_experiment(self):
        for argv in [["1", "-0"], ["1", "-n", "-1000"]]:
            with self.subTest(argv=argv), patch.object(cli.os, "getuid", return_value=1000), \
                 patch.object(cli, "acquire_lock", return_value=None), \
                 patch.object(cli, "experiment") as experiment, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(argv), 2)
                experiment.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.parse_arguments(["1", "-n"])
        self.assertEqual(error.exception.code, 2)


class ConfigurationTests(Fixture):
    def test_observation_back_rejected_and_saved_flag_not_silently_enabled(self):
        with self.assertRaisesRegex(ValueError, "需要前台"):
            self.cfg("back", observe_wakeup=True)
        self.change(wakeup_observation={"enabled": True})
        self.assertNotIn("wakeup_observation", self.cfg())

    def test_shipped_scenario_generates_requested_native_workloads(self):
        project = Path(mixbench_runner.__file__).resolve().parents[1]
        for filename in ['mixtest.json', 'mixtest.example.json']:
            settings = json.loads((project / filename).read_text())
            self.assertEqual(settings['cpus'], '4-13')
            settings['front'].update(program=str(self.front), provenance=str(self.provenance))
            settings['back']['program'] = str(self.back)
            settings['cpus'] = sorted(os.sched_getaffinity(0))
            settings['results_dir'] = str(self.root / 'records')
            self.config.write_text(json.dumps(settings))
            cfg = self.cfg(); cmd = configuration.commands(cfg, self.root)
            self.assertEqual((cfg['rps'], cfg['rps_tolerance'], cfg['latency_limit_us']), (1000, .02, 30000))
            for flag, expected in [('-m','1'),('-t','14'),('-r','60'),('-F','256'),('-n','5'),('-s','100'),('-R','1000'),('-w','0'),('-z','0')]:
                self.assertEqual(cmd['front'][cmd['front'].index(flag)+1], expected, filename)
            for value in ['--threads=14','--cpu-max-prime=20000','--time=60','--events=0','--rate=0','--report-interval=1']:
                self.assertIn(value, cmd['back'], filename)

    def test_missing_scenario_keys_use_new_defaults(self):
        settings=json.loads(self.config.read_text())
        for key in ['runtime_s','rps','rps_tolerance','latency_limit_us','timeout_s']:
            settings.pop(key,None)
        self.config.write_text(json.dumps(settings));cfg=self.cfg()
        self.assertEqual((cfg['runtime_s'],cfg['rps'],cfg['rps_tolerance'],cfg['latency_limit_us']), (60,1000,.02,30000))

    def test_native_cpu_without_warmup_option_remains_usable(self):
        self.back.write_text(self.back.read_text().replace('--warmup-time ',''))
        cfg=self.cfg();cmd=configuration.commands(cfg,self.root)['back']
        self.assertNotIn('--warmup-time=0',cmd)
        self.assertFalse(cfg['_versions']['back']['warmup']['option_supported'])
        self.assertEqual(cfg['_versions']['back']['warmup']['requested_s'],0)

    def test_overrides_frozen_without_modifying_input(self):
        raw=self.config.read_bytes();cfg=self.cfg(rps=1200,bg_threads=7,output=self.root/'custom')
        self.assertEqual((cfg['rps'],cfg['back']['threads']),(1200,7))
        self.assertEqual(cfg['results_dir'],str(self.root/'custom'));self.assertEqual(raw,self.config.read_bytes())

    def test_required_rps_and_invalid_values(self):
        raw=self.config.read_bytes()
        for values in [{'rps':None},{'rps':True},{'latency_limit_us':None},{'runtime_s':0},{'timeout_s':1},{'cpus':'1023'},
            {'rps_tolerance':1},{'latency_limit_us':-1},{'synchronization':{'min_overlap_ratio':1.1}},
            {'back':{'program':str(self.back),'threads':0}}, {'front':{'warmup_s':10}}]:
            self.change(**values)
            with self.subTest(values=values),self.assertRaises((ValueError,KeyError)):self.cfg()
            self.config.write_bytes(raw)

    def test_front_does_not_require_sysbench(self):
        self.change(back={'program':'no-such-sysbench'})
        cfg=self.cfg('front');self.assertEqual(list(configuration.commands(cfg,self.root)),['front'])

    def test_front_pilot_without_rps_or_latency_is_best_effort(self):
        self.change(rps=None,latency_limit_us=None,back={'program':'no-such-sysbench'})
        cfg=self.cfg('front');cmd=configuration.commands(cfg,self.root)['front']
        self.assertNotIn('-R',cmd);self.assertEqual(cfg['front_mode'],'best_effort')

    def test_back_does_not_require_front_or_rps(self):
        self.change(front={'program':'/missing/schbench','provenance':'/missing/provenance'},rps=None)
        cfg=self.cfg('back');self.assertEqual(list(configuration.commands(cfg,self.root)),['back'])

    def test_unsupported_timer_version_rejected(self):
        self.back.write_text(self.back.read_text().replace('1.0.20','2.0.0'))
        with self.assertRaisesRegex(ValueError,'计时口径'):self.cfg()

    def test_missing_dependency_explained(self):
        self.change(back={'program':'/missing/sysbench'})
        with self.assertRaisesRegex(ValueError,'sysbench'):self.cfg()

    def test_commands_share_duration_and_no_extra_warmup_or_realtime(self):
        cmd=configuration.commands(self.cfg(),self.root)
        self.assertIn('--time=1',cmd['back']);self.assertIn('--warmup-time=0',cmd['back'])
        self.assertIn('--events=0',cmd['back']);self.assertIn('--rate=0',cmd['back'])
        self.assertEqual(cmd['front'][cmd['front'].index('-R')+1],'1000')
        self.assertEqual(cmd['front'][cmd['front'].index('-w')+1],'0')
        self.assertEqual(cmd['front'][cmd['front'].index('-z')+1],'0')

    def test_output_cannot_overlap_other_workloads(self):
        for path in [self.root,self.root/'tests',self.root/'native',self.root.parent/'Compile/bench/results',self.root.parent/'Hackbench/bench/results']:
            with self.subTest(path=path),self.assertRaises(ValueError):self.cfg(output=path)

    def test_effective_config_roundtrip(self):
        cfg=self.cfg();p=self.root/'copy.json';write_json(p,cli.effective(cfg))
        self.assertEqual(cli.effective(cfg),cli.effective(configuration.load_config(p)))


class ResultTests(Fixture):
    def test_first_version_slo_boundaries(self):
        self.change(rps=1000,rps_tolerance=.02,latency_limit_us=10000)
        cfg=self.cfg()
        for rps,p99,met in [(980,10000,True),(1020,10000,True),(1000,9999,True),
                            (979.99,10000,False),(1020.01,10000,False),(1000,10000.01,False)]:
            row={'avg_rps':rps,'request_p99_us':p99,'wakeup_p99_us':7,'valid':True}
            with self.subTest(rps=rps,p99=p99):
                self.assertEqual(results.assess(row,cfg)['met'],met)
                self.assertTrue(row['valid'])

    def test_back_uses_native_final_rate_and_keeps_zero(self):
        p=self.root/'back.log';p.write_text(BACK_LOG)
        v=results.parse_back(p);self.assertEqual(v,{'events_s':123.45,'native_duration_s':1.0001,'total_events':123})
        p.write_text(BACK_LOG.replace('123.45','0.00').replace('events: 123','events: 0'))
        self.assertEqual(results.parse_back(p)['events_s'],0)

    def test_bad_native_metrics_and_failures_rejected(self):
        p=self.root/'back.log'
        for text in [BACK_LOG+BACK_LOG,BACK_LOG.replace('123.45','nan'),BACK_LOG.replace('1.0001s','0.0000s'),
            BACK_LOG+'FATAL: failed\n',BACK_LOG.replace('events per second','interval eps')]:
            p.write_text(text)
            with self.subTest(text=text),self.assertRaises(ValueError):results.parse_back(p)

    def test_missing_latency_limit_keeps_met_unknown(self):
        cfg=self.cfg();cfg['latency_limit_us']=None
        row={'avg_rps':1000,'request_p99_us':250,'wakeup_p99_us':7}
        self.assertIsNone(results.assess(row,cfg)['met'])

    def test_rps_failure_is_separate_from_validity(self):
        cfg=self.cfg();row={'avg_rps':100,'request_p99_us':250,'wakeup_p99_us':7,'valid':True}
        details={};results.finalize_validity(row,details,cfg)
        self.assertFalse(results.assess(row,cfg)['met']);self.assertTrue(row['valid'])
        self.assertTrue(details['technical_valid']);self.assertEqual(details['invalid_reasons'],[])

    def test_request_p99_validity_boundary_and_missing_values(self):
        cfg=self.cfg();cfg['latency_limit_us']=30000
        for value,met in [(29999,True),(30000,True),(30000.001,False),
                          (None,False),(float('nan'),False),(float('inf'),False),(-1,False)]:
            with self.subTest(value=value):
                row={'valid':True,'request_p99_us':value};details={}
                results.finalize_validity(row,details,cfg)
                self.assertEqual(row['valid'],met)
                self.assertTrue(details['technical_valid'])
                self.assertEqual(details['latency_validity']['met'],met)
                if not met:
                    self.assertEqual(details['invalid_reasons'],
                        ['p99_exceeded' if value is not None and value>=0 and value<float('inf') else 'p99_unavailable'])

    def test_technical_error_is_preserved_with_or_without_p99_failure(self):
        cfg=self.cfg()
        for p99,reason in [(250,[]),(500,['p99_exceeded']),(None,['p99_unavailable'])]:
            with self.subTest(p99=p99):
                row={'valid':False,'error':'调度器提前退出','request_p99_us':p99};details={}
                results.finalize_validity(row,details,cfg)
                self.assertFalse(row['valid']);self.assertFalse(details['technical_valid'])
                self.assertEqual(details['invalid_reasons'],['technical_error']+reason)
                self.assertIn('测试执行/核验错误: 调度器提前退出',row['error'])
                self.assertEqual('请求 p99 超限' in row['error'],p99==500)

    def test_back_and_unconfigured_front_pilot_have_no_p99_gate(self):
        for case in ['back','front']:
            cfg=self.cfg(case)
            if case=='front':cfg['latency_limit_us']=None
            row={'valid':True,'request_p99_us':None};details={}
            results.finalize_validity(row,details,cfg)
            self.assertTrue(row['valid']);self.assertIsNone(details['latency_validity']['met'])

    def test_sync_rejects_skew_low_overlap_and_unaccounted_time(self):
        evidence={'children':{side:{'exec_monotonic_s':1.,'exit_monotonic_s':2.01,'exit_code':0,'reaped':True} for side in ['front','back']}}
        cfg=self.cfg();self.assertEqual(results.synchronization(evidence,cfg,{'native_duration_s':1})[1],[])
        evidence['children']['back'].update(exec_monotonic_s=1.2,exit_monotonic_s=2.21)
        issues=results.synchronization(evidence,cfg,{'native_duration_s':1})[1]
        self.assertTrue(any('错位' in s for s in issues));self.assertTrue(any('重叠' in s for s in issues))
        evidence['children']['back'].update(exec_monotonic_s=1.,exit_monotonic_s=4.)
        self.assertTrue(any('时长不符' in s for s in results.synchronization(evidence,cfg,{'native_duration_s':1})[1]))

    def timing_evidence(self, front_excess=1.2, back_excess=.02):
        return {'children': {'front': {'exec_monotonic_s':10.,'exit_monotonic_s':70.+front_excess,
                    'exit_code':0,'reaped':True},
                'back': {'exec_monotonic_s':10.,'exit_monotonic_s':70.+back_excess,
                    'exit_code':0,'reaped':True}}}

    def test_front_known_polling_is_reported_separately_from_configured_runtime(self):
        cfg=self.cfg();cfg['runtime_s']=60
        info,errors=results.synchronization(self.timing_evidence(),cfg,{'native_duration_s':60.01})
        self.assertEqual(errors,[])
        front=info['native_duration_checks']['front']
        self.assertEqual(front['duration_reference'],'configured_runtime')
        self.assertNotIn('native_duration_s',front)
        self.assertEqual(front['configured_duration_s'],60)
        self.assertEqual(front['runtime_polling_allowance_s'],1)
        self.assertEqual(front['max_process_excess_s'],1.5)
        self.assertFalse(info['native_absolute_boundaries_observed'])
        self.assertTrue(info['warnings'])

    def test_front_beyond_polling_budget_stays_invalid(self):
        cfg=self.cfg();cfg['runtime_s']=60
        for excess,invalid in [(1.5,False),(1.501,True),(1.706,True),(-.1,True)]:
            with self.subTest(excess=excess):
                info,errors=results.synchronization(self.timing_evidence(front_excess=excess),cfg,{'native_duration_s':60.01})
                self.assertEqual(any('front 进程超出' in error for error in errors),invalid)

    def test_back_native_time_and_front_failures_keep_their_checks(self):
        cfg=self.cfg();cfg['runtime_s']=60
        evidence=self.timing_evidence(back_excess=.7)
        info,errors=results.synchronization(evidence,cfg,{'native_duration_s':60.01})
        self.assertTrue(any('back 进程时间' in error for error in errors))
        evidence=self.timing_evidence();evidence['children']['front']['exit_code']=1
        self.assertTrue(any('未正常自然退出' in error for error in results.synchronization(evidence,cfg,{'native_duration_s':60.01})[1]))

    def test_paired_improvement_requires_both_valid_and_met(self):
        rows=[{'policy':p,'run':1,'valid':True,'back_events_s':v} for p,v in [('default',100),('scx_lavd',150)]]
        a=[{'met':True},{'met':True}]
        data=results.summarize(rows,a,'mixed')['policies']['scx_lavd']
        self.assertEqual(data['paired'][0]['improvement_pct'],50)
        a[0]['met']=None;self.assertEqual(results.summarize(rows,a,'mixed')['policies']['scx_lavd']['paired'],[])
        a[0]['met']=True;rows[0]['back_events_s']=0
        self.assertEqual(results.summarize(rows,a,'mixed')['policies']['scx_lavd']['paired'],[])

    def test_matching_baseline_ratio_and_mismatched_baseline_is_empty(self):
        self.assertEqual(self.run_fake('back'),0);base=self.batch()
        self.change(baselines={'back':str(base),'front':None})
        cfg=self.cfg();snapshot=json.loads((base/'snapshot.json').read_text())
        row={'valid':True,'policy':'default','back_events_s':246.9,'request_p99_us':250}
        ratio=results.baseline_ratios(cfg,snapshot,row)
        self.assertEqual(ratio['back_throughput_retention']['ratio'],2)
        cfg['back']['threads']+=1
        self.assertIsNone(results.baseline_ratios(cfg,snapshot,row)['back_throughput_retention']['ratio'])
        self.assertIsNone(ratio['front_latency_amplification']['ratio'])


class RunTests(Fixture):
    def test_complete_observation_updates_csv_attempt_and_analysis_consistently(self):
        cfg=self.cfg();cfg['wakeup_observation']={'enabled':True}
        observed={'status':'complete','capture_complete':True,
                  'groups':{'running':{'count':98,'p99_us':2},'stopping':{'count':2,'p99_us':7}}}
        with patch.object(self,'cfg',return_value=cfg), patch.object(cli,'summarize_trace',return_value=observed):
            self.assertEqual(self.run_fake(rounds=1),0)
        self.assertTrue(all(row['wakeup_p99_us']=='2' and row['valid']=='True' for row in self.rows()))
        metadata=json.loads((self.batch()/'metadata.json').read_text())
        self.assertEqual(metadata['wakeup_metric']['scope'],'running_only')
        for policy,data in metadata['analysis']['policies'].items():
            self.assertEqual(data['metrics_valid']['wakeup_p99_us']['median'],2)
        for path in self.batch().glob('run-*/*/attempt.json'):
            detail=json.loads(path.read_text())
            self.assertEqual(detail['result']['wakeup_p99_us'],2)
            self.assertEqual(detail['wakeup_filter']['native_wakeup_p99_us'],7)

    def test_missing_observation_leaves_filtered_metric_empty_and_still_cleans_scheduler(self):
        cfg = self.cfg();cfg["wakeup_observation"] = {"enabled": True}
        with patch.object(self, "cfg", return_value=cfg):
            self.assertEqual(self.run_fake(rounds=1), 1)
        self.assertTrue(all(row["valid"] == "False" and row["wakeup_p99_us"] == "" for row in self.rows()))
        for path in self.batch().glob("run-*/*/attempt.json"):
            detail = json.loads(path.read_text())
            self.assertEqual(detail["wakeup_observation"]["status"], "unavailable")
            self.assertEqual(detail["wakeup_filter"]["native_wakeup_p99_us"], 7)
        self.assertEqual(json.loads((self.batch()/"metadata.json").read_text())["final_scheduler"]["state"], "disabled")

    def test_observation_write_error_does_not_block_scheduler_cleanup(self):
        cfg = self.cfg();cfg["wakeup_observation"] = {"enabled": True}
        with patch.object(self, "cfg", return_value=cfg), \
             patch.object(cli, "summarize_trace", side_effect=OSError("disk unavailable")):
            self.assertEqual(self.run_fake(rounds=1), 1)
        self.assertEqual(json.loads((self.batch()/"metadata.json").read_text())["final_scheduler"]["state"], "disabled")

    def test_observation_environment_is_front_only_and_stale_values_removed(self):
        front_marker, back_marker = self.root/"front-env", self.root/"back-env"
        self.write_front(extra=f"Path({str(front_marker)!r}).write_text(os.environ.get('WISP_WAKEUP_TRACE_DIR','absent'))")
        self.write_back(extra=f"Path({str(back_marker)!r}).write_text(os.environ.get('WISP_WAKEUP_TRACE_DIR','absent'))")
        cfg=self.cfg();cfg["wakeup_observation"]={"enabled":True}
        with patch.dict(os.environ, {"WISP_WAKEUP_TRACE_DIR":"stale", "WISP_WAKEUP_CAPACITY":"1"}):
            self.assertEqual(cli.experiment(cfg),1)
        attempt=next(self.batch().glob("run-*/*"))
        self.assertEqual(front_marker.read_text(),str(attempt))
        self.assertEqual(back_marker.read_text(),"absent")
        self.assertEqual(json.loads((attempt/"launch.json").read_text())["front_environment"]["WISP_WAKEUP_CAPACITY"],"4096")

    def test_single_silent_schema_and_current_policy_preserved(self):
        observer=FakeScheduler();before=observer.snapshot();out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):self.assertEqual(self.run_fake(observer=observer),0)
        self.assertEqual(out.getvalue(),'');self.assertEqual(observer.snapshot(),before)
        row,=self.rows();self.assertEqual(list(row),results.FIELDS);self.assertEqual(row['valid'],'True')
        self.assertEqual(row['temp_peak_sampled_c'],'45.0')
        attempt=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        self.assertEqual(attempt['exit_codes'],{'front':0,'back':0});self.assertTrue(attempt['constraints']['met'])
        metadata=json.loads((self.batch()/'metadata.json').read_text())
        snapshot=json.loads((self.batch()/'snapshot.json').read_text())
        self.assertEqual(metadata['timing_validation']['revision'],'bounded_approximate_v2')
        self.assertEqual(snapshot['timing_validation'],metadata['timing_validation'])

    def test_p99_failure_is_invalid_but_technically_valid_and_kept(self):
        self.change(latency_limit_us=100)
        self.assertEqual(self.run_fake(),1)
        row,=self.rows();self.assertEqual(row['valid'],'False');self.assertEqual(row['exit_code'],'0')
        self.assertIn('请求 p99 超限',row['error']);self.assertNotIn('测试执行/核验错误',row['error'])
        detail=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        self.assertFalse(detail['constraints']['met'])
        self.assertTrue(detail['technical_valid']);self.assertEqual(detail['invalid_reasons'],['p99_exceeded'])
        self.assertFalse(detail['latency_validity']['met'])

    def test_latency_failure_does_not_stop_remaining_rounds(self):
        self.change(latency_limit_us=100)
        self.assertEqual(self.run_fake(rounds=3),1)
        rows=self.rows();self.assertEqual(len(rows),12);self.assertEqual(FakeBatch.opens,1)
        self.assertTrue(all(row['valid']=='False' and '请求 p99 超限' in row['error'] for row in rows))
        metadata=json.loads((self.batch()/'metadata.json').read_text())
        self.assertEqual(metadata['final_scheduler']['state'],'disabled')
        self.assertEqual(metadata['validity_policy']['revision'],'request-p99-validity-v1')

    def test_front_pilot_without_targets_can_run(self):
        self.change(rps=None,latency_limit_us=None)
        self.assertEqual(self.run_fake('front'),0)
        detail=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        self.assertIsNone(detail['constraints']['met'])

    def test_three_rounds_have_twelve_rows_and_one_authorization(self):
        self.assertEqual(self.run_fake(rounds=3),0)
        rows=self.rows();self.assertEqual(len(rows),12);self.assertEqual(FakeBatch.opens,1)
        m=json.loads((self.batch()/'metadata.json').read_text())
        self.assertEqual([r['policy'] for r in rows],[p for order in m['orders'] for p in order])
        self.assertTrue(all(set(order)==set(POLICIES) for order in m['orders']))
        self.assertEqual(m['final_scheduler']['state'],'disabled')

    def test_single_cases_leave_inapplicable_columns_empty(self):
        for case,empty in [('front','back_events_s'),('back','avg_rps')]:
            with self.subTest(case=case):
                self.assertEqual(self.run_fake(case),0);row,=self.rows();self.assertEqual(row[empty],'')
                attempt=next(self.batch().glob('run-*/*'))
                self.assertFalse((attempt/('back.log' if case=='front' else 'front.log')).exists())
                shutil.rmtree(self.root/'records')

    def test_external_scheduler_rejected_before_authorization_in_batch(self):
        observer=FakeScheduler();observer.state.update(state='enabled',ops='external',switch_all='1')
        with self.assertRaisesRegex(PolicyError,'外部 scx'):self.run_fake(rounds=1,observer=observer)
        self.assertEqual(FakeBatch.opens,0)

    def test_load_failure_kept_and_next_policies_attempted(self):
        FakeBatch.fail='scx_rusty';self.assertEqual(self.run_fake(rounds=1),1)
        rows=self.rows();self.assertEqual(len(rows),4)
        self.assertEqual(next(r for r in rows if r['policy']=='scx_rusty')['valid'],'False')

    def test_controller_loss_stops_without_fabricating_rows(self):
        FakeBatch.fatal='scx_rusty';self.assertEqual(self.run_fake(rounds=3),1)
        self.assertLess(len(self.rows()),12)

    def test_input_change_invalidates_and_is_not_retried(self):
        old=self.measured_stub
        def modified(*args,**kwargs):
            outcome=old(*args,**kwargs);self.back.write_text(self.back.read_text()+'\n# changed\n');return outcome
        self.measured_stub=modified
        self.assertEqual(self.run_fake(),1);self.assertIn('固定输入',self.rows()[0]['error'])

    def test_real_worker_runs_both_and_captures_shared_scope(self):
        self.assertEqual(self.run_fake(measured=True),0)
        detail=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        sync=detail['synchronization'];self.assertGreater(sync['process_overlap_ratio'],0.99)
        self.assertEqual(set(detail['execution']['children']),{'front','back'})
        self.assertTrue(all(c['affinity']==sorted(os.sched_getaffinity(0)) and c['nice']==0 for c in detail['execution']['children'].values()))

    def test_one_side_failure_stops_other_promptly(self):
        marker=self.root/'back.pid'
        self.write_back(extra=f"Path({str(marker)!r}).write_text(str(os.getpid()))",sleep=60)
        self.write_front(extra="time.sleep(0.1);sys.exit(7)")
        begin=time.monotonic();self.assertEqual(self.run_fake(measured=True),1)
        self.assertLess(time.monotonic()-begin,4)
        row,=self.rows();self.assertEqual(row['valid'],'False');self.assertNotEqual(row['exit_code'],'0')
        if marker.exists():
            with self.assertRaises(ProcessLookupError):os.kill(int(marker.read_text()),0)

    def test_timeout_marks_invalid_and_kills_both(self):
        self.write_front(sleep=60);self.write_back(sleep=60)
        cfg=self.cfg();cfg['timeout_s']=0.2
        self.assertEqual(cli.experiment(cfg),1)
        row,=self.rows();self.assertEqual(row['valid'],'False');self.assertIn('超时',row['error'])
        detail=json.loads((self.batch()/'run-001/default/attempt.json').read_text())
        for child in detail['execution'].get('children',{}).values():
            with self.assertRaises(ProcessLookupError):os.kill(child['pid'],0)

    def test_actual_cli_current_default_is_silent(self):
        entry=self.isolated_cli();r=subprocess.run([sys.executable,str(entry)],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout+r.stderr,'')
        self.assertEqual(self.rows()[0]['valid'],'True')

    def test_actual_cli_interrupt_cleans_both_and_unrelated_survives(self):
        self.write_front(sleep=60);self.write_back(sleep=60)
        entry=self.isolated_cli()
        unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        process=subprocess.Popen([sys.executable,str(entry)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5;evidence=None
            while time.monotonic()<deadline:
                paths=list((self.root/'records').glob('*/run-*/*/execution.json'))
                if paths:
                    data=json.loads(paths[0].read_text())
                    if data.get('children') and all(c.get('exec_monotonic_s') for c in data['children'].values()):
                        evidence=data;break
                time.sleep(.02)
            self.assertIsNotNone(evidence)
            process.send_signal(signal.SIGINT);process.communicate(timeout=8)
            self.assertEqual(process.returncode,130);self.assertIsNone(unrelated.poll())
            for child in evidence['children'].values():
                with self.assertRaises(ProcessLookupError):os.kill(child['pid'],0)
            self.assertEqual(self.rows()[0]['valid'],'False')
        finally:
            if process.poll() is None:process.kill();process.communicate()
            unrelated.terminate();unrelated.wait()


class ObservationTests(Fixture):
    def write_trace(self, dropped=0):
        attempt=self.root/'trace';attempt.mkdir()
        rows=[{'worker_id':0,'sequence':1,'resume_realtime_us':1000100,'origin_realtime_us':1000000,
               'latency_us':100,'stopping':0,'last_post_kind':'request'},
              {'worker_id':0,'sequence':2,'resume_realtime_us':2000000,'origin_realtime_us':1150000,
               'latency_us':850000,'stopping':1,'last_post_kind':'shutdown'}]
        with (attempt/'front-wakeup.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        write_json(attempt/'front-wakeup.meta.json',{'schema_version':1,'capture_complete':not dropped,
            'recorded_samples':2,'native_wakeup_samples':2+dropped,'dropped_samples':dropped})
        return attempt

    def test_summary_keeps_running_and_stopping_samples_separate(self):
        attempt=self.write_trace();original=(attempt/'front-wakeup.csv').read_bytes()
        summary=observation.summarize_trace(attempt)
        self.assertEqual(summary['status'],'complete')
        self.assertEqual(summary['groups']['all']['p99_us'],850000)
        self.assertEqual(summary['groups']['running']['p99_us'],100)
        self.assertEqual(summary['groups']['stopping']['count'],1)
        self.assertEqual(summary['by_last_post']['shutdown']['over_100ms'],1)
        self.assertEqual(summary['slowest'][0]['last_post_kind'],'shutdown')
        self.assertEqual((attempt/'front-wakeup.csv').read_bytes(),original)
        with (attempt/'front-wakeup-running.csv').open() as stream:filtered=list(csv.DictReader(stream))
        self.assertEqual(len(filtered),1);self.assertEqual(filtered[0]['stopping'],'0')
        row={'wakeup_p99_us':850000,'avg_rps':1000,'request_p99_us':250,'valid':True};details={}
        self.assertIsNone(observation.apply_running_wakeup(row,details,summary))
        self.assertEqual(row['wakeup_p99_us'],100)
        self.assertEqual(row['avg_rps'],1000);self.assertEqual(row['request_p99_us'],250)
        self.assertTrue(row['valid'])
        self.assertEqual(details['wakeup_filter']['native_wakeup_p99_us'],850000)
        self.assertEqual(details['wakeup_filter']['excluded_samples'],1)

    def test_no_stopping_samples_means_nothing_is_discarded(self):
        attempt=self.write_trace()
        text=(attempt/'front-wakeup.csv').read_text().replace('850000,1,shutdown','850000,0,request')
        (attempt/'front-wakeup.csv').write_text(text)
        summary=observation.summarize_trace(attempt)
        row={'wakeup_p99_us':850000};details={}
        self.assertIsNone(observation.apply_running_wakeup(row,details,summary))
        self.assertEqual(row['wakeup_p99_us'],850000)
        self.assertEqual(details['wakeup_filter']['excluded_samples'],0)

    def test_no_running_samples_leaves_metric_empty(self):
        attempt=self.write_trace()
        text=(attempt/'front-wakeup.csv').read_text().replace('100,0,request','100,1,shutdown')
        (attempt/'front-wakeup.csv').write_text(text)
        summary=observation.summarize_trace(attempt)
        row={'wakeup_p99_us':850000};details={}
        self.assertIn('没有 stopping=0',observation.apply_running_wakeup(row,details,summary))
        self.assertIsNone(row['wakeup_p99_us'])

    def test_overflow_does_not_claim_complete_percentiles(self):
        summary=observation.summarize_trace(self.write_trace(dropped=1))
        self.assertEqual(summary['status'],'incomplete')
        self.assertFalse(summary['capture_complete'])
        self.assertEqual(summary['capture']['dropped_samples'],1)
        row={'wakeup_p99_us':850000};details={}
        self.assertIn('不完整',observation.apply_running_wakeup(row,details,summary))
        self.assertIsNone(row['wakeup_p99_us'])
        self.assertEqual(details['wakeup_filter']['native_wakeup_p99_us'],850000)

    def test_missing_or_corrupted_trace_is_explicit(self):
        attempt=self.root/'missing';attempt.mkdir()
        self.assertEqual(observation.summarize_trace(attempt)['status'],'unavailable')
        attempt=self.write_trace()
        text=(attempt/'front-wakeup.csv').read_text().replace('850000','850001')
        (attempt/'front-wakeup.csv').write_text(text)
        self.assertEqual(observation.summarize_trace(attempt)['status'],'unavailable')


@unittest.skipUnless(shutil.which('gcc'), 'native observation verification requires gcc')
class NativeObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='wakeup-native-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root=Path(cls.temp.name)
        project=Path(mixbench_runner.__file__).resolve().parents[1]
        cls.project=cls.root/'Mixbench'
        (cls.project/'native').mkdir(parents=True)
        shutil.copy2(project/'native/wakeup_observer.h',cls.project/'native/wakeup_observer.h')
        (cls.root/'Schbench').symlink_to(project.parent/'Schbench',target_is_directory=True)
        source=cls.root/'Schbench/bench/src'/('schbench-'+observation.COMMIT)/'schbench.c'
        if not source.exists():raise unittest.SkipTest('pinned native source is unavailable')
        cls.binary,cls.provenance,cls.identity,_=observation.ensure_observer(cls.project)

    def run_native(self, capacity):
        attempt=self.root/('run-'+str(capacity));attempt.mkdir()
        environment=os.environ.copy();environment.update(WISP_WAKEUP_TRACE_DIR=str(attempt),WISP_WAKEUP_CAPACITY=str(capacity))
        command=[self.binary,'-m','2','-t','2','-r','2','-F','16','-n','1','-s','100',
                 '-R','100','-w','0','-z','0','-i','3','-j',str(attempt/'front-native.json')]
        with (attempt/'front.log').open('w') as log:
            process=subprocess.run(command,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=10)
        self.assertEqual(process.returncode,0,(attempt/'front.log').read_text())
        metrics,_=results.parse_front(attempt/'front.log',attempt/'front-native.json',2)
        self.assertGreater(metrics['avg_rps'],0)
        return attempt,observation.summarize_trace(attempt)

    def test_native_events_match_original_count_and_have_shutdown_markers(self):
        attempt,summary=self.run_native(4096)
        self.assertEqual(summary['status'],'complete')
        self.assertEqual(summary['capture']['allocated_event_bytes'],4*4096*16)
        self.assertGreater(summary['groups']['running']['count'],0)
        self.assertGreater(summary['groups']['stopping']['count'],0)
        self.assertGreater(summary['by_last_post']['shutdown']['count'],0)
        self.assertEqual(summary['groups']['all']['count'],summary['capture']['native_wakeup_samples'])
        with (attempt/'front-wakeup.csv').open() as stream:rows=list(csv.DictReader(stream))
        self.assertEqual({int(row['worker_id']) for row in rows},{0,1,2,3})
        self.assertEqual(observation.ensure_observer(self.project)[2],self.identity)

    def test_native_capacity_exhaustion_keeps_original_histogram(self):
        _,summary=self.run_native(1)
        self.assertEqual(summary['status'],'incomplete')
        self.assertGreater(summary['capture']['dropped_samples'],0)
        self.assertEqual(summary['capture']['recorded_samples']+summary['capture']['dropped_samples'],
                         summary['capture']['native_wakeup_samples'])


class CleanupTests(Fixture):
    def test_observation_source_directory_is_protected(self):
        self.change(results_dir="native")
        with self.assertRaisesRegex(ValueError,"保留目录"):
            cleanup.roots(self.config)

    def test_clean_only_marked_mix_records_and_custom_roots(self):
        self.assertEqual(self.run_fake(),0);batch=self.batch()
        other=batch.parent/'foreign';other.mkdir();write_json(other/'metadata.json',{'workload':'hackbench'})
        self.assertEqual(cleanup.plan(self.config,[]),[batch])
        out=io.StringIO()
        with contextlib.redirect_stdout(out):self.assertEqual(cleanup.main(['--dry-run','--config',str(self.config)]),0)
        self.assertTrue(batch.exists())
        with contextlib.redirect_stdout(out):self.assertEqual(cleanup.main(['--config',str(self.config),batch.name]),0)
        self.assertFalse(batch.exists());self.assertTrue(other.exists());self.assertTrue(self.front.exists());self.assertTrue(self.config.exists())

    def test_symlink_and_path_traversal_are_not_followed(self):
        records=self.root/'records';records.mkdir();(records/'linked').symlink_to(self.root,target_is_directory=True)
        self.assertEqual(cleanup.plan(self.config,[]),[])
        with self.assertRaises(ValueError):cleanup.plan(self.config,['../front'])


if __name__=='__main__':unittest.main()
