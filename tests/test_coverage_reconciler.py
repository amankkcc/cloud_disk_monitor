"""Exercise coverage failures with local files and mocked AWS clients."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coverage_reconciler import audit_target, disk_metric, recent_metrics


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.settings = {
            "cw_namespace": "CWAgent",
            "linux_disk_metric_name": "disk_used_percent",
            "windows_disk_metric_name": "disk_free_percent",
            "disk_warning_threshold": 80,
            "disk_critical_threshold": 90,
            "alarm_period_seconds": 60,
            "alarm_evaluation_periods": 3,
            "alarm_datapoints_to_alarm": 3,
            "coverage_max_age_minutes": 5,
            "coverage_inventory_max_age_minutes": 30,
            "enrollment_grace_minutes": 15,
        }
        self.target = {
            "name": "workload-east", "account_id": "111122223333",
            "profile": "workload", "region": "us-east-1",
            "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:disk-alerts",
        }
        self.deployment = {
            "first_started_at": (self.now - timedelta(days=1)).isoformat(),
            "started_at": (self.now - timedelta(minutes=3)).isoformat(),
            "finished_at": (self.now - timedelta(minutes=2)).isoformat(),
            "status": "success",
        }
        self.write_json("deployment.json", self.deployment)
        self.instance = {
            "InstanceId": "i-linux", "State": {"Name": "running"},
            "LaunchTime": self.now - timedelta(days=1),
        }
        self.ec2 = Mock()
        self.ssm = Mock()
        self.cloudwatch = Mock()
        self.session = Mock()
        self.session.client.side_effect = lambda name, **kwargs: {
            "ec2": self.ec2, "ssm": self.ssm, "cloudwatch": self.cloudwatch,
        }[name]
        self.ec2.get_paginator.return_value.paginate.return_value = [
            {"Reservations": [{"Instances": [self.instance]}]}
        ]
        self.ssm.get_paginator.return_value.paginate.return_value = [
            {"InstanceInformationList": [{"InstanceId": "i-linux", "PingStatus": "Online"}]}
        ]
        self.alarms = self.make_alarms("i-linux", "linux", ["/", "/data"])
        self.cloudwatch.get_paginator.return_value.paginate.return_value = [{"MetricAlarms": self.alarms}]
        self.metric_overrides = {}
        self.cloudwatch.get_metric_data.side_effect = self.metric_response
        self.write_manifest("i-linux", "linux", ["/", "/data"])

    def write_json(self, filename, value):
        (self.state / filename).write_text(json.dumps(value), encoding="utf-8")

    def write_manifest(self, instance_id, platform, filesystems, collected=None):
        self.write_json(f"{instance_id}.json", {
            "instance_id": instance_id, "platform": platform,
            "collected_at": (collected or self.now - timedelta(minutes=2)).isoformat(),
            "filesystems": filesystems,
        })

    def make_alarms(self, instance_id, platform, filesystems):
        alarms = []
        for filesystem in filesystems:
            for level, threshold in (("warning", 80), ("critical", 90)):
                alarm = disk_metric(instance_id, platform, filesystem, self.settings)
                alarm.update({
                    "AlarmName": f"Disk-{level}-{instance_id}-{filesystem}",
                    "Threshold": 100 - threshold if platform == "windows" else threshold,
                    "ComparisonOperator": "LessThanOrEqualToThreshold" if platform == "windows" else "GreaterThanOrEqualToThreshold",
                    "Statistic": "Average", "Period": 60, "EvaluationPeriods": 3,
                    "DatapointsToAlarm": 3, "TreatMissingData": "missing", "ActionsEnabled": True,
                    "AlarmActions": [self.target["sns_topic_arn"]],
                    "OKActions": [self.target["sns_topic_arn"]],
                })
                alarms.append(alarm)
        return alarms

    def metric_response(self, **request):
        results = []
        for query in request["MetricDataQueries"]:
            dimensions = {item["Name"]: item["Value"] for item in query["MetricStat"]["Metric"]["Dimensions"]}
            filesystem = dimensions.get("path", dimensions.get("instance"))
            result = {"Id": query["Id"], "StatusCode": "Complete", "Values": [0.0], "Timestamps": [self.now - timedelta(minutes=1)]}
            result.update(self.metric_overrides.get(filesystem, {}))
            results.append(result)
        return {"MetricDataResults": results}

    def audit(self, now=None):
        return audit_target(self.session, self.target, self.settings, self.state, now or self.now)

    def test_all_filesystems_require_fresh_metrics_and_both_alarms(self):
        result = self.audit()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["findings"], [])
        self.metric_overrides["/data"] = {"Values": [], "Timestamps": []}
        result = self.audit()
        self.assertEqual(result["status"], "gap")
        self.assertIn("i-linux /data: no recent disk metric", result["findings"])

    def test_zero_is_valid_but_old_or_future_timestamps_are_not(self):
        for timestamp in (self.now - timedelta(minutes=6), self.now + timedelta(minutes=1)):
            with self.subTest(timestamp=timestamp):
                self.metric_overrides["/data"] = {"Timestamps": [timestamp]}
                self.assertEqual(self.audit()["status"], "gap")

    def test_bad_alarm_configuration_never_passes(self):
        original = copy.deepcopy(self.alarms[0])
        changes = [
            {"ActionsEnabled": False}, {"OKActions": []}, {"AlarmActions": []},
            {"Threshold": 70}, {"Period": 300}, {"EvaluationPeriods": 1},
            {"DatapointsToAlarm": 1}, {"TreatMissingData": "breaching"},
            {"Statistic": "Maximum"}, {"ComparisonOperator": "GreaterThanThreshold"},
            {"Namespace": "Other"}, {"MetricName": "Other"},
            {"Dimensions": [{"Name": "InstanceId", "Value": "i-linux"}]},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.alarms[0] = dict(original, **change)
                result = self.audit()
                self.assertEqual(result["status"], "gap", result)
        self.alarms[0] = original

    def test_all_discovery_and_alarm_pages_are_consumed(self):
        second = dict(self.instance, InstanceId="i-windows", Platform="windows")
        self.ec2.get_paginator.return_value.paginate.return_value.append({"Reservations": [{"Instances": [second]}]})
        self.ssm.get_paginator.return_value.paginate.return_value.append({
            "InstanceInformationList": [{"InstanceId": "i-windows", "PingStatus": "Online"}]
        })
        self.cloudwatch.get_paginator.return_value.paginate.return_value.append({
            "MetricAlarms": self.make_alarms("i-windows", "windows", ["C:"])
        })
        self.write_manifest("i-windows", "windows", ["C:"])
        result = self.audit()
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("2 running instances, 3 expected filesystems", result["summary"])
        windows_query = self.cloudwatch.get_metric_data.call_args.kwargs["MetricDataQueries"][-1]
        dimensions = windows_query["MetricStat"]["Metric"]["Dimensions"]
        self.assertEqual({item["Name"] for item in dimensions}, {"InstanceId", "instance", "objectname"})
        self.ec2.get_paginator.return_value.paginate.assert_called_with(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )

    def test_missing_or_stale_inventory_and_failed_deployment_do_not_pass(self):
        (self.state / "i-linux.json").unlink()
        self.assertEqual(self.audit()["status"], "gap")
        self.write_manifest("i-linux", "linux", ["/"], self.now - timedelta(minutes=31))
        self.assertEqual(self.audit()["status"], "error")
        self.write_manifest("i-linux", "linux", ["/", "/data"])
        self.deployment["status"] = "failed"
        self.write_json("deployment.json", self.deployment)
        self.assertEqual(self.audit()["status"], "error")

    def test_new_disk_that_never_emitted_cannot_hide(self):
        self.write_manifest("i-linux", "linux", ["/", "/data", "/new"])
        self.metric_overrides["/new"] = {"Values": [], "Timestamps": []}
        result = self.audit()
        self.assertEqual(result["status"], "gap")
        self.assertEqual(len([item for item in result["findings"] if "/new:" in item]), 3)

    def test_initial_grace_expires_and_manifest_refresh_does_not_reset_it(self):
        self.deployment["first_started_at"] = (self.now - timedelta(minutes=3)).isoformat()
        self.write_json("deployment.json", self.deployment)
        self.metric_overrides["/data"] = {"Values": [], "Timestamps": []}
        self.assertEqual(self.audit()["status"], "pending")
        later = self.now + timedelta(minutes=16)
        self.write_manifest("i-linux", "linux", ["/", "/data"], later - timedelta(minutes=1))
        self.assertEqual(self.audit(later)["status"], "gap")

    def test_ssm_offline_is_a_gap_but_stopped_instances_are_excluded(self):
        self.ssm.get_paginator.return_value.paginate.return_value = [{"InstanceInformationList": []}]
        self.assertEqual(self.audit()["status"], "gap")
        self.instance["State"]["Name"] = "stopped"
        result = self.audit()
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("0 running instances", result["summary"])

    def test_api_failure_and_partial_results_are_errors(self):
        for code in ("PartialData", "InternalError", "Forbidden"):
            with self.subTest(code=code):
                self.metric_overrides["/data"] = {"StatusCode": code}
                self.assertEqual(self.audit()["status"], "error")
        self.cloudwatch.get_metric_data.side_effect = RuntimeError("Request failed")
        self.assertEqual(self.audit()["status"], "error")

    def test_metric_queries_follow_pages_and_respect_batch_limit(self):
        requests = []

        def paged_response(**request):
            requests.append(request)
            complete = "NextToken" in request
            results = [{
                "Id": query["Id"], "StatusCode": "Complete" if complete else "PartialData",
                "Values": [10.0] if complete else [],
                "Timestamps": [self.now] if complete else [],
            } for query in request["MetricDataQueries"]]
            return {"MetricDataResults": results, **({} if complete else {"NextToken": "next"})}

        self.cloudwatch.get_metric_data.side_effect = paged_response
        queries = [{"Id": f"m{number}"} for number in range(501)]
        recent, errors = recent_metrics(self.cloudwatch, queries, self.settings, self.now)
        self.assertEqual(len(recent), 501)
        self.assertEqual(errors, {})
        self.assertEqual([len(item["MetricDataQueries"]) for item in requests], [500, 500, 1, 1])

    def test_unchanged_problem_has_stable_findings(self):
        self.metric_overrides["/data"] = {"Values": [], "Timestamps": []}
        first = self.audit()
        second = self.audit(self.now + timedelta(minutes=1))
        self.assertEqual(first["findings"], second["findings"])

    def test_stale_alarm_candidates_require_fresh_inventory_and_do_not_fail_audit(self):
        removed = self.make_alarms("i-linux", "linux", ["/removed"])
        unknown = self.make_alarms("i-unknown", "linux", ["/removed"])
        self.alarms.extend(removed + unknown)
        result = self.audit()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["stale_alarms"], sorted(item["AlarmName"] for item in removed))
        self.write_manifest("i-linux", "linux", ["/", "/data"], self.now - timedelta(minutes=31))
        self.assertEqual(self.audit()["stale_alarms"], [])


if __name__ == "__main__":
    unittest.main()
