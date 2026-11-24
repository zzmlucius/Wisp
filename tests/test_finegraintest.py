import copy
import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import Finegraintest as fine


class FinegrainTests(unittest.TestCase):
    def test_private_configs_cover_the_entire_matrix(self):
        base = {
            'case': 'mixed', 'front': {'worker_threads': 14, 'locking': True},
            'back': {'threads': 14, 'cpu_max_prime': 20000}, 'runtime_s': 60,
            'rps': 1000, 'cooldown_s': 30, 'cpus': [4, 5], 'latency_limit_us': 30000,
        }
        original = copy.deepcopy(base)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def load(path):
                return copy.deepcopy(base) if path == fine.SETTINGS else json.loads(path.read_text())
            with patch.object(fine, 'load_config', side_effect=load), \
                 patch.object(fine.collector, 'prepare', return_value={'binary': '/bin/false', 'files': [str(fine.BPF_OBJECT)]}), \
                 patch.object(fine, 'scheduler_specs', return_value={}), \
                 patch.object(fine.queue, 'commands', return_value={}):
                jobs = fine.prepare_jobs(root, {'AC/online': '1'})

            self.assertEqual(base, original)
            self.assertEqual(fine.RPS_VALUES, (250, 500, 750, 1000, 1250, 1500, 1750, 2000))
            self.assertEqual(len({j['name'] for j in jobs}), len(jobs))
            self.assertEqual(len(jobs) * len(fine.POLICIES) * 3, 864)
            self.assertEqual([(j['config']['front']['worker_threads'],
                               j['config']['back']['threads'], j['config']['rps']) for j in jobs],
                             list(product(fine.THREADS, fine.THREADS, fine.RPS_VALUES)))
            for job in jobs:
                cfg = job['config']
                self.assertEqual(cfg['runtime_s'], 30)
                self.assertEqual(cfg['cooldown_s'], 30)
                self.assertEqual(cfg['back']['cpu_max_prime'], 20000)
                self.assertEqual(cfg['latency_limit_us'], 30000)
                self.assertEqual(cfg['_collector']['prefix'], fine.PREFIX)
                self.assertTrue(cfg['_collector_required'])
                self.assertTrue(cfg['_collector']['authorized'])
                self.assertFalse(cfg['collector']['runqueue_verified'])
                self.assertEqual(cfg['results_dir'], str(root/'results'))
                self.assertTrue(Path(job['config_file']).is_file())


if __name__ == '__main__':
    unittest.main()
