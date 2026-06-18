"""
AgentCore S3 Filesystem Integration with Harness.

Demonstrates the full lifecycle of attaching an Amazon S3 Files access point
to a Harness so the agent can read and write files that sync bidirectionally
with an S3 bucket:

1. Provision S3 Files infrastructure — bucket, VPC subnets, security groups,
   IAM roles, file system, mount targets, and access point
2. Create a Harness with VPC networking and S3 Files filesystem configuration
3. Invoke the agent — it reads/writes files at the mounted path
4. Verify via ExecuteCommand — inspect the NFS mount directly
5. Verify in S3 — confirm written files appear as objects in the bucket
6. Clean up the harness (infrastructure persists for re-runs)

All infrastructure is idempotent — safe to re-run. Resources are discovered
by deterministic names derived from a prefix, so only the first run creates them.
Uses the default VPC and auto-creates an S3 bucket.

Amazon S3 Files provides a POSIX filesystem interface over S3 bucket data.
When mounted in a Harness, the agent uses standard file operations (read, write,
ls, cat) on data that is also accessible via S3 APIs — enabling shared access
between agents, pipelines, and external applications.

Usage:
    # Run the demo (creates infra on first run, reuses on subsequent runs)
    python s3_filesystem_integration.py

    # Keep the harness after the demo
    python s3_filesystem_integration.py --skip-cleanup

    # Tear down ALL resources (bucket, file system, IAM, security groups)
    python s3_filesystem_integration.py --cleanup

Prerequisites:
    - AWS CLI configured with credentials
    - pip install -r ../../requirements.txt
    - AWS_DEFAULT_REGION environment variable set
    - A default VPC with subnets in at least two Availability Zones
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from utils.client import get_agentcore_control_client, get_agentcore_client
from utils.s3_filesystem import (
    setup_s3files_infrastructure,
    cleanup_all,
)

# ── Constants ─────────────────────────────────────────────────────────────────

REGION = boto3.session.Session().region_name or "us-east-1"
ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
PREFIX = "harness-s3fs-demo"

DEFAULT_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_MOUNT_PATH = "/mnt/s3data"
DEFAULT_PROMPT = (
    "Plan a 5-day trip to Tokyo. Include daily activities, restaurant recommendations, "
    "and travel tips. Save the full itinerary as a JSON file to {mount_path}/tokyo_itinerary.json. "
    "After writing, verify the file exists by reading it back and showing the first few entries."
)

HARNESS_POLL_INTERVAL = 5
HARNESS_POLL_TIMEOUT = 120  # seconds

# ── CLI ────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="AgentCore S3 Filesystem Integration — mount S3 Files in a Harness, invoke agent.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument("--skip-cleanup", action="store_true", help="Keep harness after the demo")
parser.add_argument("--cleanup", action="store_true", help="Delete all resources and exit")
parser.add_argument("--subnets", nargs="+", help="Private subnet IDs (must have NAT gateway route)")
args = parser.parse_args()

# ── Script ────────────────────────────────────────────────────────────────────

print(f"Account: {ACCOUNT_ID}")
print(f"Region:  {REGION}")

# Handle --cleanup: delete everything and exit
if args.cleanup:
    print("\n=== Cleanup: deleting all resources ===")
    cleanup_all(REGION, PREFIX)
    sys.exit(0)

control = get_agentcore_control_client()
client = get_agentcore_client()
HARNESS_ID = None


# ── Step 1: S3 Files infrastructure (idempotent) ─────────────────────────
print("\n=== Step 1: S3 Files infrastructure (idempotent) ===")
infra = setup_s3files_infrastructure(
    region=REGION,
    account_id=ACCOUNT_ID,
    prefix=PREFIX,
    subnet_ids=args.subnets,
)

# ── Step 2: Create Harness with S3 Files mount ───────────────────────────
print("\n=== Step 2: Create Harness with S3 Files filesystem ===")
harness_name = f"{PREFIX}_harness".replace("-", "_")
system_prompt = (
    "You are a travel planning assistant. You create detailed trip itineraries with "
    "daily activities, restaurant recommendations, budget estimates, and practical travel tips. "
    f"Save all plans to {DEFAULT_MOUNT_PATH} — this directory is synchronized with an S3 bucket, "
    "so anything you write here is automatically available to other systems and users. "
    "Use JSON for structured itineraries and Markdown for human-readable guides. "
    "When writing files, ensure you close file handles so changes sync to S3."
)


def _create_harness():
    return control.create_harness(
        harnessName=harness_name,
        executionRoleArn=infra["harness_role_arn"],
        environment={
            "agentCoreRuntimeEnvironment": {
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "subnets": infra["subnet_ids"],
                        "securityGroups": [infra["harness_sg_id"]],
                    },
                },
                "filesystemConfigurations": [
                    {
                        "s3FilesAccessPoint": {
                            "accessPointArn": infra["access_point_arn"],
                            "mountPath": DEFAULT_MOUNT_PATH,
                        }
                    }
                ],
            }
        },
        systemPrompt=[{"text": system_prompt}],
    )


try:
    resp = _create_harness()
    HARNESS_ID = resp["harness"]["harnessId"]
    HARNESS_ARN = resp["harness"]["arn"]
    print(f"Harness created: {HARNESS_ID}")
except control.exceptions.ConflictException:
    # Find existing harness — if DELETING, wait and retry create
    HARNESS_ID = None
    for h in control.list_harnesses().get("harnesses", []):
        if h.get("harnessName") == harness_name:
            HARNESS_ID = h["harnessId"]
            HARNESS_ARN = h.get("arn", "")
            break
    if not HARNESS_ID:
        raise RuntimeError(f"Harness {harness_name} conflict but not found")

    h_status = control.get_harness(harnessId=HARNESS_ID)["harness"]["status"]
    if h_status in ("DELETING", "CREATE_FAILED"):
        if h_status == "CREATE_FAILED":
            print(f"  Harness {HARNESS_ID} is CREATE_FAILED, deleting...")
            control.delete_harness(harnessId=HARNESS_ID)
        else:
            print(f"  Harness {HARNESS_ID} is DELETING, waiting...")
        for _ in range(30):
            try:
                control.get_harness(harnessId=HARNESS_ID)
            except Exception:
                break
            time.sleep(10)
        resp = _create_harness()
        HARNESS_ID = resp["harness"]["harnessId"]
        HARNESS_ARN = resp["harness"]["arn"]
        print(f"Harness created: {HARNESS_ID}")
    else:
        print(f"Harness already exists: {HARNESS_ID}")

print(f"Harness ID:  {HARNESS_ID}")
print(f"Harness ARN: {HARNESS_ARN}")

print("Waiting for harness READY...")
for _ in range(30):
    h_status = control.get_harness(harnessId=HARNESS_ID)["harness"]["status"]
    print(f"  {h_status}")
    if h_status == "READY":
        break
    if h_status == "FAILED":
        raise RuntimeError("Harness creation FAILED")
    time.sleep(10)
print(f"Harness status: {h_status}")

# ── Step 3: Invoke the agent ─────────────────────────────────────────────
print("\n=== Step 3: Invoke agent (files served via S3 Files mount) ===")
session_id = str(uuid.uuid4()).upper()
message = DEFAULT_PROMPT.format(mount_path=DEFAULT_MOUNT_PATH)
print(f"  Session ID: {session_id}")
print(f"  Model: {DEFAULT_MODEL}")
print(f"  Message: {message[:80]}...\n")

response = client.invoke_harness(
    harnessArn=HARNESS_ARN,
    runtimeSessionId=session_id,
    messages=[{"role": "user", "content": [{"text": message}]}],
    model={"bedrockModelConfig": {"modelId": DEFAULT_MODEL}},
)
for event in response["stream"]:
    if "contentBlockStart" in event:
        start = event["contentBlockStart"].get("start", {})
        if "toolUse" in start:
            print(f"\n  [Tool: {start['toolUse'].get('name', '?')}]", flush=True)
    elif "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            print(delta["text"], end="", flush=True)
    elif "messageStop" in event:
        print()
    elif "internalServerException" in event:
        print(f"\n  Error: {event['internalServerException']}")

# ── Step 4: Verify via ExecuteCommand ────────────────────────────────────
print("\n=== Step 4: Verify filesystem mount (ExecuteCommand) ===")
for cmd in [
    f"ls -la {DEFAULT_MOUNT_PATH}/",
    f"cat {DEFAULT_MOUNT_PATH}/tokyo_itinerary.json 2>/dev/null | head -40 || echo 'File not found'",
    "mount | grep nfs",
]:
    print(f"  $ {cmd}")
    resp = client.invoke_agent_runtime_command(
        agentRuntimeArn=HARNESS_ARN,
        runtimeSessionId=session_id,
        body={"command": cmd},
    )
    for event in resp["stream"]:
        if "chunk" in event:
            chunk = event["chunk"]
            if "contentDelta" in chunk:
                d = chunk["contentDelta"]
                if "stdout" in d:
                    print(f"    {d['stdout']}", end="", flush=True)
                if "stderr" in d:
                    print(f"    {d['stderr']}", end="", flush=True)
            elif "contentStop" in chunk:
                print(f"\n    [exit: {chunk['contentStop']['exitCode']}]")
    print()

# ── Step 5: Verify file in S3 bucket ─────────────────────────────────────
print("\n=== Step 5: Verify file synced to S3 bucket ===")
print("  Waiting for S3 sync (close-to-open consistency)...")
time.sleep(5)
s3 = boto3.client("s3", region_name=REGION)
bucket_name = infra["bucket_name"]
try:
    resp = s3.get_object(Bucket=bucket_name, Key="tokyo_itinerary.json")
    content = resp["Body"].read().decode("utf-8")
    print(f"  File found in s3://{bucket_name}/tokyo_itinerary.json")
    print(f"  Content (first 200 chars): {content[:200]}")
except s3.exceptions.NoSuchKey:
    print("  File not yet visible in S3 (sync may take a moment)")
    print(f"  Try: aws s3 ls s3://{bucket_name}/tokyo_itinerary.json")

print("\n=== Done! ===")


# ── Cleanup ───────────────────────────────────────────────────────────────────
if not args.skip_cleanup:
    print("\n=== Cleanup: deleting harness ===")
    if HARNESS_ID:
        try:
            control.delete_harness(harnessId=HARNESS_ID)
            print(f"  Deleted harness: {HARNESS_ID}")
        except Exception as e:
            print(f"  Warning: harness cleanup failed: {e}")
    print("  Infrastructure preserved for next run. Use --cleanup to tear down.")
else:
    print("\n=== Skipping cleanup (--skip-cleanup) ===")
    print(f"  Harness ID: {HARNESS_ID}")
    print("  Run again to reuse existing infrastructure.")
