"""Exercise account isolation, deployment state, and notification retries offline."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_monitoring as runner


TARGET = {
    "name": "workload-a-us-east-1",
    "account_id": "111122223333",
    "profile": "workload-a",
    "region": "us-east-1",
    "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:enterprise-disk-alerts",
}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = deepcopy(TARGET)
        self.config = {"monitoring_account_id": "123456789012", "targets": [self.target]}

    def load_config(self):
        path = self.root / "targets.yml"
        path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        return runner.load_config(path)

    def test_rejects_duplicate_account_region_and_path_traversal(self):
        duplicate = dict(self.target, name="another-name")
        self.config["targets"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "unique"):
            self.load_config()
        self.config["targets"] = [dict(self.target, name="../outside")]
        with self.assertRaisesRegex(ValueError, "Target names"):
            self.load_config()

    def test_rejects_wrong_region_or_account_in_topic(self):
        for topic in (
            "arn:aws:sns:us-west-2:123456789012:enterprise-disk-alerts",
            "arn:aws:sns:us-east-1:999999999999:enterprise-disk-alerts",
        ):
            self.target["sns_topic_arn"] = topic
            with self.assertRaisesRegex(ValueError, "central topic"):
                self.load_config()

    def test_account_mismatch_is_rejected_before_deployment(self):
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
        with self.assertRaisesRegex(ValueError, "expected 111122223333"):
            runner.check_identity(session, self.target["account_id"])

    def test_deployment_passes_one_account_region_and_preserves_first_enrollment(self):
        inventory_dir = self.root / "ansible/inventory"
        inventory_dir.mkdir(parents=True)
        (inventory_dir / "aws_ec2.yml").write_text("plugin: amazon.aws.aws_ec2\nregions: [us-west-2]\n")
        state = self.root / "state"
        observed = []

        def run(command, **kwargs):
            inventory = yaml.safe_load(Path(command[2]).read_text())
            variables = json.loads(Path(command[-1][1:]).read_text())
            observed.append((inventory, variables, kwargs["env"]))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(runner, "ROOT", self.root), patch.object(runner.subprocess, "run", side_effect=run):
            with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "example-parent-key", "AWS_SESSION_TOKEN": "example-token"}):
                runner.deploy_target(self.target, state)
                first = json.loads((state / "deployment.json").read_text())["first_started_at"]
                runner.deploy_target(self.target, state)
        self.assertEqual(json.loads((state / "deployment.json").read_text())["first_started_at"], first)
        inventory, variables, environment = observed[0]
        self.assertEqual(inventory["regions"], [self.target["region"]])
        self.assertEqual(inventory["profile"], self.target["profile"])
        self.assertEqual(variables["target_account_id"], self.target["account_id"])
        self.assertEqual(variables["ansible_aws_ssm_bucket_name"], "ansible-ssm-staging-111122223333-us-east-1")
        self.assertNotIn("AWS_ACCESS_KEY_ID", environment)
        self.assertNotIn("AWS_SESSION_TOKEN", environment)
        self.assertEqual(environment["AWS_PROFILE"], self.target["profile"])

    def test_failed_playbook_leaves_failed_deployment_state(self):
        inventory_dir = self.root / "ansible/inventory"
        inventory_dir.mkdir(parents=True)
        (inventory_dir / "aws_ec2.yml").write_text("plugin: amazon.aws.aws_ec2\n")
        state = self.root / "state"
        with patch.object(runner, "ROOT", self.root), patch.object(runner.subprocess, "run") as run:
            run.return_value.returncode = 2
            with self.assertRaisesRegex(RuntimeError, "status 2"):
                runner.deploy_target(self.target, state)
        record = json.loads((state / "deployment.json").read_text())
        self.assertEqual(record["status"], "failed")
        self.assertIsNotNone(record["finished_at"])

    def test_notifications_deduplicate_and_send_recovery(self):
        session = Mock()
        state = self.root / "state"
        healthy = {"status": "ok", "summary": "Healthy", "findings": []}
        gap = {"status": "gap", "summary": "Missing telemetry", "findings": ["i-example /data: no recent disk metric"]}
        runner.notify_changes(session, self.target, healthy, state)
        session.client.assert_not_called()
        runner.notify_changes(session, self.target, gap, state)
        runner.notify_changes(session, self.target, gap, state)
        self.assertEqual(session.client.return_value.publish.call_count, 1)
        runner.notify_changes(session, self.target, healthy, state)
        self.assertEqual(session.client.return_value.publish.call_count, 2)

    def test_failed_notification_is_retried(self):
        session = Mock()
        session.client.return_value.publish.side_effect = RuntimeError("SNS unavailable")
        state = self.root / "state"
        gap = {"status": "gap", "summary": "Missing telemetry", "findings": ["missing"]}
        with self.assertRaisesRegex(RuntimeError, "SNS unavailable"):
            runner.notify_changes(session, self.target, gap, state)
        self.assertFalse((state / "notification.json").exists())
        session.client.return_value.publish.side_effect = None
        runner.notify_changes(session, self.target, gap, state)
        self.assertEqual(session.client.return_value.publish.call_count, 2)


if __name__ == "__main__":
    unittest.main()
