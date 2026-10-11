#!/usr/bin/env python3
"""Deploy or audit each configured AWS account and region, one at a time."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import yaml

from coverage_reconciler import audit_target


ROOT = Path(__file__).resolve().parents[1]
AWS_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5})


def read_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def load_config(path):
    config = read_yaml(path)
    if not isinstance(config, dict):
        raise ValueError("Target configuration must be a YAML mapping.")
    central_account = config.get("monitoring_account_id", "")
    if not re.fullmatch(r"\d{12}", str(central_account)):
        raise ValueError("monitoring_account_id must be a quoted 12-digit account ID.")
    if config.get("controller_profile") is not None and not isinstance(config["controller_profile"], str):
        raise ValueError("controller_profile must be a profile name or null.")
    targets = config.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("Configure at least one target.")
    names, locations = set(), set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Each target must be a YAML mapping.")
        for key in ("name", "account_id", "profile", "region", "sns_topic_arn"):
            if not isinstance(target.get(key), str) or not target[key]:
                raise ValueError(f"Each target needs a nonempty string: {key}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", target["name"]):
            raise ValueError("Target names must contain only lowercase letters, numbers and hyphens.")
        if not re.fullmatch(r"\d{12}", target["account_id"]):
            raise ValueError(f"Invalid account ID for {target['name']}.")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", target["region"]):
            raise ValueError(f"Unsupported commercial AWS region for {target['name']}.")
        expected_topic = f"arn:aws:sns:{target['region']}:{central_account}:enterprise-disk-alerts"
        if target["sns_topic_arn"] != expected_topic:
            raise ValueError(f"{target['name']}: use the central topic in the target's region.")
        location = (target["account_id"], target["region"])
        if target["name"] in names or location in locations:
            raise ValueError("Target names and account/region pairs must be unique.")
        names.add(target["name"])
        locations.add(location)
    return config


def write_json(path, data):
    # Replace a complete file so readers never see a partially written manifest.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def controller_lock(state_root):
    """The Linux controller uses one lock for deployment and auditing."""
    import fcntl

    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "controller.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another deployment or audit is still running.") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def check_identity(session, expected_account):
    account = session.client("sts", config=AWS_CONFIG).get_caller_identity()["Account"]
    if account != expected_account:
        raise ValueError(f"AWS identity is account {account}; expected {expected_account}.")


def deploy_target(target, state_dir):
    started_at = datetime.now(timezone.utc).isoformat()
    record_path = state_dir / "deployment.json"
    previous = json.loads(record_path.read_text()) if record_path.exists() else {}
    record = {
        "first_started_at": previous.get("first_started_at", started_at),
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
    }
    write_json(record_path, record)
    inventory = read_yaml(ROOT / "ansible/inventory/aws_ec2.yml")
    inventory["profile"] = target["profile"]
    inventory["regions"] = [target["region"]]
    variables = {
        "target_account_id": target["account_id"],
        "target_region": target["region"],
        "target_profile": target["profile"],
        "alarm_sns_topic_arn": target["sns_topic_arn"],
        "ansible_aws_ssm_bucket_name": f"ansible-ssm-staging-{target['account_id']}-{target['region']}",
        "monitoring_state_dir": str(state_dir.resolve()),
        "deployment_started_at": started_at,
    }
    environment = os.environ.copy()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_DEFAULT_PROFILE"):
        environment.pop(name, None)
    environment.update({
        "AWS_PROFILE": target["profile"],
        "AWS_REGION": target["region"],
        "AWS_DEFAULT_REGION": target["region"],
        "AWS_RETRY_MODE": "standard",
        "AWS_MAX_ATTEMPTS": "5",
        "ANSIBLE_CONFIG": str(ROOT / "ansible/ansible.cfg"),
    })
    try:
        with tempfile.TemporaryDirectory(prefix="disk-monitoring-") as directory:
            inventory_path = Path(directory) / "target.aws_ec2.yml"
            vars_path = Path(directory) / "variables.json"
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            vars_path.write_text(json.dumps(variables), encoding="utf-8")
            completed = subprocess.run([
                "ansible-playbook", "-i", str(inventory_path),
                str(ROOT / "ansible/playbook_deploy_monitoring.yml"),
                "--extra-vars", f"@{vars_path}",
            ], cwd=ROOT, env=environment, check=False)
            if completed.returncode:
                raise RuntimeError(f"Ansible exited with status {completed.returncode}.")
        record["status"] = "success"
    except BaseException:
        record["status"] = "failed"
        raise
    finally:
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(record_path, record)


def notify_changes(session, target, result, state_dir):
    path = state_dir / "notification.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    details = {"status": result["status"], "findings": sorted(result["findings"])}
    fingerprint = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    if previous.get("fingerprint") == fingerprint:
        return
    # Initial health or initial enrollment does not need an email.
    initial_quiet = not previous and result["status"] in ("ok", "pending")
    if not initial_quiet:
        message = "\n".join([
            f"Disk monitoring audit: {target['name']}",
            f"Account: {target['account_id']} | Region: {target['region']}",
            result["summary"], *result["findings"],
        ])
        session.client("sns", region_name=target["region"], config=AWS_CONFIG).publish(
            TopicArn=target["sns_topic_arn"],
            Subject=f"Disk coverage {result['status']}: {target['name']}"[:100],
            Message=message[:60000],
        )
    write_json(path, {"fingerprint": fingerprint, "status": result["status"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "deploy", "audit"))
    parser.add_argument("--targets", type=Path, default=ROOT / "targets.yml")
    parser.add_argument("--target", action="append", help="Run only this named target; may be repeated.")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".monitoring-state")
    parser.add_argument("--notify", action="store_true", help="Publish changed audit results to central SNS.")
    args = parser.parse_args(argv)
    if args.notify and args.action != "audit":
        parser.error("--notify is available only for audit.")
    config = load_config(args.targets)
    targets = config["targets"]
    if args.target:
        unknown = set(args.target) - {target["name"] for target in targets}
        if unknown:
            parser.error(f"Unknown target(s): {', '.join(sorted(unknown))}")
        targets = [target for target in targets if target["name"] in args.target]
    if args.action == "list":
        for target in targets:
            print(f"{target['name']}: {target['account_id']} / {target['region']} / profile {target['profile']}")
        return 0
    if sys.platform != "linux":
        parser.error("Deployment and audit jobs run on the Linux Ansible controller; 'list' works on any OS.")
    settings = read_yaml(ROOT / "ansible/inventory/group_vars/all.yml")
    state_root = args.state_dir.resolve()
    exit_code = 0
    with controller_lock(state_root):
        publisher = None
        if args.notify:
            try:
                publisher = boto3.Session(profile_name=config.get("controller_profile"))
                check_identity(publisher, str(config["monitoring_account_id"]))
            except (BotoCoreError, ClientError, ValueError) as error:
                print(f"Notifications unavailable: {error}", file=sys.stderr)
                publisher = None
                exit_code = 1
        for target in targets:
            print(f"\n{args.action.upper()}: {target['name']}", flush=True)
            state_dir = state_root / target["name"]
            try:
                session = boto3.Session(profile_name=target["profile"], region_name=target["region"])
                check_identity(session, target["account_id"])
                if args.action == "deploy":
                    deploy_target(target, state_dir)
                    print("Deployment completed; run audit to verify telemetry and alarms.")
                    continue
                result = audit_target(session, target, settings, state_dir)
            except Exception as error:
                result = {"status": "error", "summary": "Target could not be verified.", "findings": [str(error)]}
            print(result["summary"])
            for finding in result["findings"]:
                print(f"  {finding}")
            for alarm in result.get("stale_alarms", []):
                print(f"  Review obsolete alarm (no deletion performed): {alarm}")
            if result["status"] != "ok":
                exit_code = 1
            if publisher:
                try:
                    notify_changes(publisher, target, result, state_dir)
                except Exception as error:
                    print(f"Notification failed: {error}", file=sys.stderr)
                    exit_code = 1
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BotoCoreError, ClientError, ValueError, OSError, RuntimeError, yaml.YAMLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
