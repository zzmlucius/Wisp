import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compilebench_runner import cleanup
from compilebench_runner.cli import acquire_lock


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.patch = patch.object(cleanup, "PROJECT", self.project)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = self.project / "compiletest.json"
        self.cfg = {"source": "bench/src/linux", "kernel_config": "bench/config/kernel.config",
                    "build_root": "bench/build", "results_dir": "bench/results"}
        self.config.write_text(json.dumps(self.cfg))
        self.kept = []
        for relative in ("bench/src/linux/Makefile", "bench/config/kernel.config",
                         "bench/config/kernel.json", "bench/build/batch/vmlinux"):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserve me")
            self.kept.append(path)
        self.records = []
        for relative in ("bench/results/batch/run-001/default/build.log",
                         "bench/validation/schedulers-check/diagnostics.json", "results/old/summary.csv"):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test record")
            self.records.append(path)

    def run_clean(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cleanup.main(["-c", str(self.config), *arguments])

    def test_delete_records_preserves_inputs_outputs_and_symlink_targets(self):
        (self.project / "bench/results/link-to-source").symlink_to(self.kept[0].parent, target_is_directory=True)
        self.assertEqual(self.run_clean(), 0)
        self.assertTrue(all(not path.exists() for path in self.records))
        self.assertTrue(all(path.read_text() == "preserve me" for path in self.kept))
        self.assertEqual(json.loads(self.config.read_text()), self.cfg)
        self.assertTrue((self.project / "bench/results").is_dir())

    def test_dry_run_preserves_every_record(self):
        self.assertEqual(self.run_clean("--dry-run"), 0)
        self.assertTrue(all(path.exists() for path in self.records))

    def test_overlap_rejected_before_deletion(self):
        for unsafe in (".", "bench", "bench/build", "bench/src/linux", "bench/config"):
            with self.subTest(results_dir=unsafe):
                self.config.write_text(json.dumps({**self.cfg, "results_dir": unsafe}))
                self.assertEqual(self.run_clean(), 2)
                self.assertTrue(all(path.exists() for path in self.records + self.kept))

    def test_symlink_root_rejected_without_touching_destination(self):
        linked = self.project / "linked-results"
        linked.symlink_to(self.project / "bench/results", target_is_directory=True)
        self.config.write_text(json.dumps({**self.cfg, "results_dir": str(linked)}))
        self.assertEqual(self.run_clean(), 2)
        self.assertTrue(all(path.exists() for path in self.records))

    def test_active_compiletest_lock_prevents_deletion(self):
        lock = acquire_lock()
        try:
            self.assertEqual(self.run_clean(), 2)
            self.assertTrue(all(path.exists() for path in self.records))
        finally:
            os.close(lock)

    def artifact(self, name):
        path = self.project / "bench/config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated configuration")
        return path

    def test_selected_batch_removes_only_its_configs_and_records(self):
        own = [self.artifact(name) for name in ("batch.config", "kernel-batch.config", "config-batch.json", "kernel-batch.log")]
        other = self.artifact("kernel-batch-other.config")
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertTrue(all(not path.exists() for path in own))
        self.assertFalse(self.records[0].exists())
        self.assertTrue(all(path.exists() for path in self.records[1:] + self.kept + [other]))

    def test_generated_configuration_directory_is_removed_with_its_batch(self):
        artifact = self.artifact("batch/config/options.config")
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertFalse(artifact.parent.parent.exists())
        self.assertTrue(all(path.exists() for path in self.kept))

    def test_metadata_registered_configuration_is_removed(self):
        generated = self.artifact("generated-options.config")
        metadata = self.project / "bench/results/batch/metadata.json"
        metadata.write_text(json.dumps({"generated_configs": [str(generated)]}))
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertFalse(generated.exists())

    def test_relative_metadata_path_and_filename_match_are_deleted_once(self):
        generated = self.artifact("kernel-batch.config")
        metadata = self.project / "bench/results/batch/metadata.json"
        metadata.write_text(json.dumps({"generated_configs": ["../../config/kernel-batch.config"]}))
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertFalse(generated.exists())

    def test_configuration_still_referenced_by_another_batch_is_preserved(self):
        generated = self.artifact("shared-generated.config")
        for name in ("batch", "retained"):
            directory = self.project / "bench/results" / name
            directory.mkdir(exist_ok=True)
            (directory / "metadata.json").write_text(json.dumps({"generated_configs": [str(generated)]}))
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertTrue(generated.exists())
        self.assertTrue((self.project / "bench/results/retained/metadata.json").exists())

    def test_all_cleanup_removes_orphan_timestamp_configs(self):
        generated = self.artifact("kernel-20261103T132723.853222Z-206509.config")
        unrelated = self.artifact("manual.config")
        self.assertEqual(self.run_clean(), 0)
        self.assertFalse(generated.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(all(path.exists() for path in self.kept))

    def test_dry_run_preserves_generated_configs_and_records(self):
        generated = self.artifact("kernel-batch.config")
        self.assertEqual(self.run_clean("batch", "--dry-run"), 0)
        self.assertTrue(generated.exists())
        self.assertTrue(all(path.exists() for path in self.records))

    def test_registered_path_outside_config_directory_is_rejected_before_deletion(self):
        outside = self.project / "outside.config"
        outside.write_text("must remain")
        (self.project / "bench/results/batch/metadata.json").write_text(json.dumps({"generated_configs": [str(outside)]}))
        self.assertEqual(self.run_clean("batch"), 2)
        self.assertTrue(outside.exists())
        self.assertTrue(all(path.exists() for path in self.records + self.kept))

    def test_shared_frozen_config_cannot_be_deleted_by_metadata(self):
        metadata = self.project / "bench/results/batch/metadata.json"
        metadata.write_text(json.dumps({"generated_configs": [str(self.project / "bench/config/kernel.config"),
                                                              str(self.project / "bench/config/kernel.json")]}))
        self.assertEqual(self.run_clean("batch"), 0)
        self.assertTrue(all(path.exists() for path in self.kept))

    def test_unknown_or_path_batch_names_fail_without_removing_records(self):
        for name in ("missing", "..", "../config", "/tmp", "batch/../batch"):
            with self.subTest(name=name):
                self.assertEqual(self.run_clean(name), 2)
                self.assertTrue(all(path.exists() for path in self.records + self.kept))


if __name__ == "__main__":
    unittest.main()
