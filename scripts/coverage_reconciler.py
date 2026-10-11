"""Read-only checks of discovered filesystems, recent metrics, and disk alarms."""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.config import Config


AWS_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5})


def utc_time(value):
    """Accept SDK datetimes and the ISO timestamps stored by the deployment job."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Expected a timestamp with a timezone")
    return value.astimezone(timezone.utc)


def read_json(path):
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def disk_metric(instance_id, platform, filesystem, settings):
    dimensions = {"InstanceId": instance_id}
    if platform == "windows":
        name = settings["windows_disk_metric_name"]
        dimensions.update(instance=filesystem, objectname="LogicalDisk")
    else:
        name = settings["linux_disk_metric_name"]
        dimensions["path"] = filesystem
    return {
        "Namespace": settings["cw_namespace"],
        "MetricName": name,
        "Dimensions": [{"Name": key, "Value": value} for key, value in dimensions.items()],
    }


def metric_key(metric):
    return (
        metric.get("Namespace"),
        metric.get("MetricName"),
        tuple(sorted((item["Name"], item["Value"]) for item in metric.get("Dimensions", []))),
    )


def alarm_matches(alarm, level, platform, settings, topic):
    threshold = settings[f"disk_{level}_threshold"]
    comparison = "GreaterThanOrEqualToThreshold"
    if platform == "windows":
        threshold = 100 - threshold
        comparison = "LessThanOrEqualToThreshold"
    return (
        alarm.get("Threshold") == threshold
        and alarm.get("ComparisonOperator") == comparison
        and alarm.get("Statistic") == "Average"
        and alarm.get("Period") == settings["alarm_period_seconds"]
        and alarm.get("EvaluationPeriods") == settings["alarm_evaluation_periods"]
        and alarm.get("DatapointsToAlarm", alarm.get("EvaluationPeriods"))
        == settings["alarm_datapoints_to_alarm"]
        and alarm.get("TreatMissingData") == "missing"
        and alarm.get("ActionsEnabled") is True
        and topic in alarm.get("AlarmActions", [])
        and topic in alarm.get("OKActions", [])
    )


def recent_metrics(cloudwatch, queries, settings, now):
    """Return freshness and API errors for each query, including all result pages."""
    freshness = timedelta(minutes=settings["coverage_max_age_minutes"])
    oldest = now - freshness
    recent = set()
    errors = {}
    for offset in range(0, len(queries), 500):
        batch = queries[offset:offset + 500]
        statuses = {}
        request = {
            "MetricDataQueries": batch,
            "StartTime": now - max(freshness * 2, timedelta(minutes=15)),
            "EndTime": now,
        }
        while True:
            response = cloudwatch.get_metric_data(**request)
            if response.get("Messages"):
                raise RuntimeError("CloudWatch returned an incomplete metric response")
            for result in response.get("MetricDataResults", []):
                query_id = result["Id"]
                statuses[query_id] = result.get("StatusCode")
                if result.get("StatusCode") in ("InternalError", "Forbidden"):
                    errors[query_id] = result["StatusCode"]
                for timestamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                    if oldest <= utc_time(timestamp) <= now and math.isfinite(value):
                        recent.add(query_id)
            token = response.get("NextToken")
            if not token:
                break
            request["NextToken"] = token
        for query in batch:
            query_id = query["Id"]
            if statuses.get(query_id) != "Complete":
                errors[query_id] = statuses.get(query_id) or "No result returned"
    return recent, errors


def audit_target(session, target, settings, state_dir, now=None):
    """Return a stable summary for the runner; never change AWS resources or state."""
    findings = {"error": [], "gap": [], "pending": []}
    stale_alarms = []
    instance_count = 0
    filesystem_count = 0
    try:
        now = utc_time(now or datetime.now(timezone.utc))
        state_dir = Path(state_dir)
        grace = timedelta(minutes=settings["enrollment_grace_minutes"])
        inventory_age = timedelta(minutes=settings["coverage_inventory_max_age_minutes"])
        first_started = None
        try:
            deployment = read_json(state_dir / "deployment.json")
            if deployment.get("first_started_at"):
                first_started = utc_time(deployment["first_started_at"])
            started = utc_time(deployment["started_at"])
            if started > now or (first_started and first_started > now):
                raise ValueError("Deployment timestamp is in the future")
            if deployment["status"] == "running":
                status = "pending" if now - started <= grace else "error"
                findings[status].append("Deployment is still running; coverage is not yet verified")
            elif deployment["status"] == "failed":
                findings["error"].append("The most recent deployment failed")
            elif deployment["status"] == "success":
                finished = utc_time(deployment["finished_at"])
                if not started <= finished <= now:
                    raise ValueError("Invalid deployment completion timestamp")
                if now - finished > inventory_age:
                    findings["error"].append("The most recent successful deployment is stale")
            else:
                raise ValueError("Unknown deployment status")
        except (OSError, ValueError, KeyError, TypeError) as error:
            findings["error"].append(f"Cannot verify deployment state: {error}")

        ec2 = session.client("ec2", region_name=target["region"], config=AWS_CONFIG)
        cloudwatch = session.client("cloudwatch", region_name=target["region"], config=AWS_CONFIG)
        ssm = session.client("ssm", region_name=target["region"], config=AWS_CONFIG)
        instances = []
        for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ):
            for reservation in page.get("Reservations", []):
                instances.extend(reservation.get("Instances", []))
        # Recheck state in case an instance stopped while discovery was paginating.
        instances = [item for item in instances if item.get("State", {}).get("Name") == "running"]
        instance_count = len(instances)

        online = set()
        alarms = {}
        if instances:
            for page in ssm.get_paginator("describe_instance_information").paginate():
                for instance in page.get("InstanceInformationList", []):
                    if instance.get("PingStatus") == "Online":
                        online.add(instance["InstanceId"])
            for page in cloudwatch.get_paginator("describe_alarms").paginate(AlarmNamePrefix="Disk-"):
                for alarm in page.get("MetricAlarms", []):
                    alarms.setdefault(metric_key(alarm), []).append(alarm)

        expected = []
        queries = []
        verified_filesystems = {}
        for instance in sorted(instances, key=lambda item: item["InstanceId"]):
            instance_id = instance["InstanceId"]
            platform = instance.get("Platform", "linux")
            grace_start = utc_time(instance["LaunchTime"])
            if first_started:
                grace_start = max(grace_start, first_started)
            missing_status = "pending" if timedelta(0) <= now - grace_start <= grace else "gap"
            if instance_id not in online:
                findings[missing_status].append(f"{instance_id}: SSM is not online")
            manifest_path = state_dir / f"{instance_id}.json"
            if not manifest_path.exists():
                findings[missing_status].append(f"{instance_id}: filesystem inventory is missing")
                continue
            try:
                manifest = read_json(manifest_path)
                if manifest["instance_id"] != instance_id or manifest["platform"] != platform:
                    raise ValueError("Instance identity or operating system does not match")
                collected = utc_time(manifest["collected_at"])
                if collected > now or now - collected > inventory_age:
                    raise ValueError("Filesystem inventory is stale or has a future timestamp")
                filesystems = manifest["filesystems"]
                if not isinstance(filesystems, list) or not filesystems:
                    raise ValueError("No filesystems were discovered")
                if any(not isinstance(item, str) or not item for item in filesystems):
                    raise ValueError("Invalid filesystem name")
                if len(set(filesystems)) != len(filesystems):
                    raise ValueError("Duplicate filesystem names")
            except (OSError, ValueError, KeyError, TypeError) as error:
                findings["error"].append(f"{instance_id}: cannot verify filesystem inventory: {error}")
                continue

            verified_filesystems[instance_id] = (platform, set(filesystems))
            for filesystem in sorted(filesystems):
                filesystem_count += 1
                label = f"{instance_id} {filesystem}"
                metric = disk_metric(instance_id, platform, filesystem, settings)
                query_id = f"m{len(queries)}"
                queries.append({
                    "Id": query_id,
                    "MetricStat": {"Metric": metric, "Period": settings["alarm_period_seconds"], "Stat": "Average"},
                    "ReturnData": True,
                })
                expected.append((query_id, label, missing_status))
                candidates = alarms.get(metric_key(metric), [])
                for level in ("warning", "critical"):
                    if not any(alarm_matches(alarm, level, platform, settings, target["sns_topic_arn"])
                               for alarm in candidates):
                        findings[missing_status].append(f"{label}: {level} alarm is missing or misconfigured")

        # Only fresh host discovery can identify a removed filesystem safely.
        for candidates in alarms.values():
            for alarm in candidates:
                dimensions = {item["Name"]: item["Value"] for item in alarm.get("Dimensions", [])}
                instance_id = dimensions.get("InstanceId")
                if instance_id not in verified_filesystems:
                    continue
                platform, filesystems = verified_filesystems[instance_id]
                metric_name = settings[f"{platform}_disk_metric_name"]
                filesystem = dimensions.get("instance" if platform == "windows" else "path")
                if (alarm.get("AlarmName", "").startswith("Disk-")
                        and alarm.get("Namespace") == settings["cw_namespace"]
                        and alarm.get("MetricName") == metric_name
                        and (platform != "windows" or dimensions.get("objectname") == "LogicalDisk")
                        and filesystem and filesystem not in filesystems):
                    stale_alarms.append(alarm["AlarmName"])

        recent, metric_errors = recent_metrics(cloudwatch, queries, settings, now)
        for query_id, label, missing_status in expected:
            if query_id in metric_errors:
                findings["error"].append(f"{label}: metric query failed ({metric_errors[query_id]})")
            elif query_id not in recent:
                findings[missing_status].append(f"{label}: no recent disk metric")
    except Exception as error:
        # The runner can continue auditing other targets after a failed API request.
        findings["error"].append(f"Audit could not complete: {error}")

    status = next((name for name in ("error", "gap", "pending") if findings[name]), "ok")
    messages = sorted(message for group in findings.values() for message in group)
    summary = (f"{target['name']} ({target['account_id']}/{target['region']}): {status.upper()} — "
               f"{instance_count} running instances, {filesystem_count} expected filesystems")
    return {"status": status, "findings": messages, "summary": summary, "stale_alarms": sorted(stale_alarms)}
