# S3 Filesystem

| Information         | Details                                                          |
|:--------------------|:-----------------------------------------------------------------|
| Tutorial type       | Advanced Example                                                 |
| Agent type          | Travel planning assistant                                        |
| Agentic Framework   | None (direct boto3)                                              |
| LLM model           | Anthropic Claude Haiku 4.5                                       |
| Tutorial components | AgentCore harness — S3 Files access point, VPC, filesystemConfigurations |
| Example complexity  | Intermediate                                                     |

## Overview

The harness persists file state on the agent's filesystem. Each session runs in a Firecracker microVM with a working filesystem, and for state that needs to outlast a single session or be shared across agents, you mount S3-backed storage through AgentCore filesystem configurations.

This sample demonstrates attaching an **Amazon S3 Files access point** to a harness. The S3 Files access point is one of three filesystem types the harness supports:

- **Session storage** — service-managed, per-session storage that persists across stop/resume cycles. No VPC required.
- **Amazon EFS access point** — bring-your-own EFS file system, shared across sessions and agents. VPC required.
- **Amazon S3 Files access point** — bring-your-own S3 Files file system that syncs bidirectionally with an S3 bucket. VPC required.

When you configure an S3 Files access point, the harness mounts the file system at a path you specify (e.g., `/mnt/s3data`). Files written to this mount sync automatically with the backing S3 bucket — the agent reads and writes using standard file operations, while the same data remains accessible via S3 APIs from external applications, pipelines, or other agents.

You do not need to install mount helpers, manage TLS certificates, or write mount code in your agent. AgentCore handles all mount operations automatically via NFSv4.2 over TLS with IAM authentication.

## What is S3 Files?

Amazon S3 Files provides a standard POSIX file system interface over S3 bucket data. Key properties:

- **Bidirectional sync** — changes at the mount path appear as S3 objects; objects uploaded via S3 APIs appear as files
- **Close-to-open consistency** for NFS clients; S3 eventual consistency for bucket-side access
- **Shared access** — multiple sessions, agents, or external applications can access the same data simultaneously
- **No size constraints for agents** — max file size 48 TiB; max directory depth 1,000 levels
- **IAM-authenticated** — uses NFSv4.2 over TLS on port 2049; TLS and IAM are always enabled

## Architecture

```
[harness] → filesystemConfigurations=[{s3FilesAccessPoint: {accessPointArn, mountPath}}]
                │
                ▼
        [Firecracker microVM in VPC]
        - /mnt/s3data mounted via NFS
        - agent reads/writes with standard file ops
                │  NFSv4.2 over TLS (port 2049)
                ▼
        [S3 Files access point]  ── POSIX UID/GID scoping
                │
                ▼
        [S3 Files file system]  ── bidirectional sync
                │
                ▼
        [S3 bucket]  ── same objects accessible via S3 APIs
```

## End-to-End Flow

```
1. Create IAM execution role       (reuses utils/iam.py helper)
2. Create S3 Files file system     → backed by an existing S3 bucket
3. Create mount targets            → one per AZ, in the same VPC as harness subnets
4. Create access point             → POSIX UID/GID 1000:1000, root directory /
5. Create security groups          → harness SG (egress 2049) ↔ mount target SG (ingress 2049)
6. Create harness                  → VPC mode + filesystemConfigurations with access point ARN
7. invoke_harness                  → agent sees /mnt/s3data, reads/writes files
8. Verify in S3                    → written files appear as objects in the bucket
9. Cleanup                         → delete harness, access point, mount targets, file system, SGs
```

## Prerequisites

1. **VPC with subnets** — At least two subnets in different Availability Zones
2. **Security groups** — One for the harness (outbound TCP 2049) and one for the S3 Files mount target (inbound TCP 2049)
3. **S3 bucket** — A general-purpose S3 bucket in the same region
4. **IAM permissions** — Your execution role needs `s3files:ClientMount`, `s3files:ClientWrite`, and `s3files:GetAccessPoint`

The script can create all networking and S3 Files resources for you (`--setup-infra`), or you can provide existing ones via CLI flags.

## Sample Prompts

**Prompt (itinerary)**: "Plan a 5-day trip to Tokyo. Include daily activities, restaurant recommendations, and travel tips. Save the full itinerary as a JSON file to /mnt/s3data/tokyo_itinerary.json."
**Expected Behavior**: Agent generates a structured itinerary and writes it to the S3-mounted path. The file appears in the backing S3 bucket within seconds.

**Prompt (multi-file)**: "Create a 3-day Barcelona travel plan. Save the itinerary to /mnt/s3data/barcelona/itinerary.md and a packing list to /mnt/s3data/barcelona/packing_list.md."
**Expected Behavior**: Agent creates a subdirectory, writes both files with detailed content. Both objects appear in the S3 bucket under the `barcelona/` prefix.

**Prompt (read and extend)**: "Read the itinerary at /mnt/s3data/tokyo_itinerary.json and add a 6th day focused on day trips outside the city. Save the updated version back to the same file."
**Expected Behavior**: Agent reads the existing file from the mount, extends it with new content, and overwrites with the updated version. The S3 object updates accordingly.

**Prompt (summary)**: "List all itinerary files in /mnt/s3data, then create a master index at /mnt/s3data/index.md linking to each trip plan with a one-line summary."
**Expected Behavior**: Agent runs `ls` or `find`, reads each file, generates a markdown index, and writes it to the mount.

## Key Concepts

**VPC requirement**: S3 Files access points require the harness to run in VPC mode. The microVM mounts the file system over the VPC network.

**Mount target placement**: S3 Files mount targets must be in the same VPC and Availability Zone as the harness subnets. The script creates one mount target per subnet AZ.

**Access point UID/GID**: The access point specifies a POSIX UID (1000) and GID (1000) — all file operations through the mount run as this identity. Match this to the user your agent process runs as.

**Bidirectional sync**: Files uploaded to S3 appear at the mount path; files written to the mount appear as S3 objects. Close-to-open consistency applies.

**Shared access**: Unlike session storage, the S3 Files mount is shared across all sessions and agents using the same access point.

## Troubleshooting

### Issue: Mount fails with "Access denied"
**Solution**: Ensure the execution role has `s3files:ClientMount`, `s3files:ClientWrite`, and `s3files:GetAccessPoint` permissions with the correct `s3files:AccessPointArn` condition.

### Issue: Harness invocation returns HTTP 424 (Failed Dependency)
**Solution**: Mount failed. Check that the S3 Files mount target is in the same VPC and AZ as the harness subnets, and that security groups allow TCP 2049 between them.

### Issue: Files written by agent don't appear in S3
**Solution**: Ensure the file handle is closed (close-to-open consistency). The agent should explicitly close files after writing. Also verify the access point has write permissions. Note: S3 Files sync may take a few minutes after close — check again after a short delay.

### Issue: `filesystemConfigurations` accepted but mount not present
**Solution**: The S3 Files filesystem feature for Harness requires the latest boto3 SDK and service availability in your region. Verify with `mount | grep nfs` inside the microVM. If no NFS mount appears, check that the boto3 version supports the full harness environment API, and that S3 Files is available in your region.

### Issue: "ResourceNotFound" during harness creation
**Solution**: Verify the S3 Files access point ARN is correct and the file system status is `AVAILABLE`. Use `aws s3files get-file-system` to check.

## AgentCore CLI

S3 Files filesystem configuration is supported via the AgentCore CLI (preview channel):

```bash
npm install -g @aws/agentcore@preview
agentcore create --name mytripagent --model-provider bedrock
```

Configure the filesystem in `agentcore.json`:

```json
{
  "agents": {
    "mytripagent": {
      "networkMode": "VPC",
      "filesystemConfigurations": [
        {
          "s3FilesAccessPoint": {
            "accessPointArn": "arn:aws:s3files:<region>:<account-id>:file-system/<fs-id>/access-point/<ap-id>",
            "mountPath": "/mnt/s3data"
          }
        }
      ]
    }
  }
}
```

Then deploy:

```bash
agentcore deploy
agentcore invoke --harness mytripagent \
  --session-id "$(uuidgen)" \
  "Plan a 5-day trip to Tokyo and save the itinerary to /mnt/s3data/tokyo_itinerary.json"
```

## Clean Up

```python
control.delete_harness(harnessId=harness_id)
# If --setup-infra was used, also cleans up:
#   S3 Files access point, mount targets, file system
#   Security groups, IAM role
```

## Running the Python Scripts

```bash
pip install -r ../../requirements.txt
```

```bash
# Run the demo (creates infra on first run, reuses on subsequent runs)
python s3_filesystem_integration.py

# Keep the harness after the demo
python s3_filesystem_integration.py --skip-cleanup

# Tear down ALL resources (bucket, file system, IAM, security groups)
python s3_filesystem_integration.py --cleanup
```
