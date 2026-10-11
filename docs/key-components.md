# Key components of the AWS disk monitoring solution

The solution uses Ansible to configure EC2 instances and AWS services to collect metrics, display disk utilization and send alerts. It supports multiple AWS accounts and regions without a third-party monitoring platform. Ansible handles setup and enrollment; the CloudWatch Agent keeps publishing metrics independently of the controller.

## 1. Access management

The Linux Ansible controller uses an IAM role in the central monitoring account. For each workload account, an AWS profile assumes `AnsibleCrossAccountExecutionRole` through AWS Security Token Service (STS). This provides temporary credentials. The member role trusts the exact controller role, and the runner verifies the account identity before deployment or auditing.

Ansible connects through Systems Manager (SSM) Session Manager. VMs need a running SSM Agent, the required instance-role permissions and outbound connectivity. Inbound SSH and WinRM ports are unnecessary. These prerequisites must be provided through images, launch templates or an existing bootstrap process.

Each account and region has a private, encrypted S3 staging bucket for Ansible module transfers. The controller signs transfer URLs; VM roles do not need permission to the bucket. TLS is required, and abandoned transfers expire after one day.

VM roles provide `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy` permissions. Existing application roles should receive these permissions rather than be replaced. Runtime alarm permissions are limited to `Disk-*` alarms; provisioning uses separate administrator credentials.

## 2. VM discovery and enrollment for metric collection

[targets.yml](../targets.yml) defines each workload account, AWS profile, region and regional central SNS topic. The runner processes these targets sequentially.

For each deployment:

1. Dynamic EC2 inventory discovers running instances in the selected account and region and separates Linux and Windows hosts.
2. Ansible discovers mounted Linux filesystems, excluding configured temporary and pseudo filesystems, or Windows fixed logical drives.
3. It records the expected filesystems on the controller before installing or configuring the agent. An installation failure therefore remains visible to the coverage auditor.
4. It installs the CloudWatch Agent when absent, verifies the downloaded package signature, applies configuration and enables the service across reboots.
5. It creates warning and critical alarms for every discovered filesystem and restores disabled notification actions.

The agent collects eligible disks using wildcard resources. A newly bootstrapped VM or newly mounted disk receives configuration and alarms on the next successful deployment. The documented scheduler example runs deployment every 15 minutes; the schedule must be installed by the operator. Installed agent versions are preserved during regular deployments.

## 3. Metric collection, aggregation and alerts

The CloudWatch Agent publishes capacity metrics every 60 seconds to the workload account's `CWAgent` namespace.

| Platform | Metric | Warning | Critical |
|---|---|---|---|
| Linux | `disk_used_percent` per mount | At least 80% used | At least 90% used |
| Windows | `disk_free_percent` per fixed drive | At most 20% free | At most 10% free |

Alarms evaluate three consecutive one-minute datapoints and send alarm and recovery notifications to the central SNS topic in the same region. The email subscription must be confirmed.

CloudWatch Observability Access Manager (OAM) links make workload metrics visible through a central regional sink and dashboard. Metrics remain in their source accounts. Each monitored region has its own sink, dashboard and SNS topic.

## 4. Coverage verification

The coverage auditor compares running EC2 instances and independently discovered filesystems with SSM availability, recent metrics and both alarm configurations. It reports missing telemetry, offline SSM agents, stale discovery, failed deployments and misconfigured alarms.

The documented audit schedule runs every five minutes. A bounded enrollment grace period allows new VMs time to publish metrics; pending enrollment is not verified coverage. Stopped instances are excluded. Changed coverage results and recovery can be sent through SNS, while unchanged results are suppressed.

## 5. Scalability and account onboarding

Ansible processes hosts in batches of 25, and the auditor paginates AWS discovery responses and batches metric queries. New accounts need the account-wide IAM baseline, regional resources, VM prerequisites, controller permissions, AWS profiles and target entries.

For AWS Organization accounts, CloudFormation StackSets can distribute the baselines and deploy them automatically to new accounts in selected organizational units. This is an onboarding option; controller configuration and VM bootstrap remain explicit steps.

As the fleet grows, measure deployment duration and adjust schedules and discovery expiry. Targets can be distributed across controller jobs with independent state directories. Per-filesystem alarms continue evaluating when the controller is unavailable; enrollment and coverage auditing depend on the controller.

See the [deployment guide](../README.md) for commands, supported platforms, prerequisites and live verification steps. The [detailed project walkthrough](project-walkthrough.md) explains each file and its behavior.
