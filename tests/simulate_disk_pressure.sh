#!/usr/bin/env bash
# Run only on a disposable instance and an independently mounted test filesystem.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 /mnt/disk-monitor-test [85|95]" >&2
    exit 1
fi

mount_path=$(realpath -- "$1")
target_percent=${2:-85}
if [[ "$target_percent" != 85 && "$target_percent" != 95 ]]; then
    echo "The target must be 85 (warning) or 95 (critical)." >&2
    exit 1
fi
if [[ "$mount_path" == / || "$mount_path" == /tmp || "$mount_path" == /var || "$mount_path" == /home ]]; then
    echo "Refusing to fill a system filesystem." >&2
    exit 1
fi
if ! mountpoint -q -- "$mount_path"; then
    echo "The supplied path must be a dedicated mounted test filesystem." >&2
    exit 1
fi
if [[ $(stat -c %d -- "$mount_path") == $(stat -c %d -- /) ]]; then
    echo "The test filesystem must use a different device from root." >&2
    exit 1
fi
filesystem_type=$(findmnt -n -o FSTYPE --target "$mount_path")
if [[ "$filesystem_type" != ext4 && "$filesystem_type" != xfs ]]; then
    echo "Use an ext4 or xfs test filesystem that the monitoring agent collects." >&2
    exit 1
fi

# Limit both the test filesystem and allocation; retain at least 16 MiB free.
mib=$((1024 * 1024))
max_bytes=$((1024 * mib))
reserve_bytes=$((16 * mib))
read -r size used available < <(df -B1 --output=size,used,avail -- "$mount_path" | tail -n 1)
if (( size > max_bytes )); then
    echo "Use a dedicated test filesystem no larger than 1 GiB." >&2
    exit 1
fi
fill_bytes=$((size * target_percent / 100 - used))
fill_mib=$(((fill_bytes + mib - 1) / mib))
if (( fill_bytes <= 0 || fill_mib * mib > max_bytes || available - fill_mib * mib < reserve_bytes )); then
    echo "Cannot reach the requested target while preserving the free-space reserve." >&2
    exit 1
fi

test_file=$(mktemp -- "$mount_path/disk-monitor-test.XXXXXXXX")
cleanup() {
    rm -f -- "$test_file"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Allocating ${fill_mib} MiB on $mount_path for approximately ${target_percent}% usage."
dd if=/dev/zero of="$test_file" bs=1M count="$fill_mib" status=progress conv=fsync
df -h -- "$mount_path"
echo "Verify this filesystem's CloudWatch metric, alarm, and SNS notification."
echo "Allow up to 10 minutes; an exact three-minute transition is not guaranteed."
read -r -p "Press Enter to remove the payload and verify recovery: "
cleanup
trap - EXIT
df -h -- "$mount_path"
echo "Payload removed. Verify the metric recovers and the alarm sends its OK notification."
