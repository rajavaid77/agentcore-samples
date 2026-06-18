"""
Helper functions for provisioning S3 Files infrastructure used by the s3_filesystem_integration.py script.

Each function is self-contained: it creates one logical resource group,
prints progress, and returns a dict with the values the caller needs.
All functions are idempotent — safe to re-run if a resource already exists.
Resources are discovered by deterministic names derived from a prefix parameter.
"""

import json
import time

import boto3


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _ensure_role(iam, role_name, trust_doc, description=""):
    """Create an IAM role if it doesn't exist. Returns the role ARN."""
    try:
        return iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=trust_doc,
            Description=description,
        )["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]


def _find_sg_by_name(ec2, vpc_id, group_name):
    """Return security group ID if it exists, else None."""
    resp = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [group_name]},
        ]
    )
    groups = resp["SecurityGroups"]
    return groups[0]["GroupId"] if groups else None


def _find_file_system_for_bucket(s3files, bucket_arn):
    """Return file_system_id if one already exists for this bucket, else None."""
    try:
        for fs in s3files.list_file_systems().get("fileSystems", []):
            if fs.get("bucket") == bucket_arn and fs["status"].upper() in ("AVAILABLE", "CREATING"):
                return fs["fileSystemId"]
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────
# S3 Bucket
# ─────────────────────────────────────────────────────────────────────


def create_bucket(region, account_id, prefix):
    """Create (or reuse) an S3 bucket with versioning enabled. Returns bucket name."""
    s3 = boto3.client("s3", region_name=region)
    bucket_name = f"{prefix}-{account_id}-{region}"

    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"  Bucket already exists: {bucket_name}")
    except s3.exceptions.ClientError:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        print(f"  Bucket created: {bucket_name}")

    # S3 Files requires versioning
    s3.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"})
    print(f"  Bucket versioning enabled: {bucket_name}")

    return bucket_name


# ─────────────────────────────────────────────────────────────────────
# VPC / Subnets
# ─────────────────────────────────────────────────────────────────────


def get_default_vpc_subnets(region):
    """Return subnet IDs from the default VPC (picks two subnets in different AZs)."""
    ec2 = boto3.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("No default VPC found in this region.")
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    seen_azs = set()
    result = []
    for s in subnets:
        if s["AvailabilityZone"] not in seen_azs:
            result.append(s["SubnetId"])
            seen_azs.add(s["AvailabilityZone"])
        if len(result) >= 2:
            break
    if len(result) < 2:
        raise RuntimeError(f"Default VPC {vpc_id} has fewer than 2 AZs with subnets.")
    print(f"  Default VPC: {vpc_id}")
    print(f"  Subnets: {result}")
    return result


def _get_vpc_from_subnets(subnet_ids, region):
    """Return (vpc_id, availability_zones) for the given subnets."""
    ec2 = boto3.client("ec2", region_name=region)
    subnet_info = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    vpc_id = subnet_info[0]["VpcId"]
    azs = [s["AvailabilityZone"] for s in subnet_info]
    return vpc_id, azs


# ─────────────────────────────────────────────────────────────────────
# Security Groups
# ─────────────────────────────────────────────────────────────────────


def create_security_groups(vpc_id, region, prefix):
    """Create (or reuse) harness and mount-target security groups with NFS rules.

    Returns (harness_sg_id, mount_target_sg_id).
    """
    ec2 = boto3.client("ec2", region_name=region)

    mt_sg_name = f"{prefix}-s3files-mt"
    harness_sg_name = f"{prefix}-s3files-harness"

    mt_sg_id = _find_sg_by_name(ec2, vpc_id, mt_sg_name)
    if mt_sg_id:
        print(f"  Mount target SG already exists: {mt_sg_id}")
    else:
        mt_sg_id = ec2.create_security_group(
            GroupName=mt_sg_name,
            Description="S3 Files mount target - allows NFS from harness",
            VpcId=vpc_id,
        )["GroupId"]
        print(f"  Mount target SG created: {mt_sg_id}")

    harness_sg_id = _find_sg_by_name(ec2, vpc_id, harness_sg_name)
    if harness_sg_id:
        print(f"  Harness SG already exists: {harness_sg_id}")
    else:
        harness_sg_id = ec2.create_security_group(
            GroupName=harness_sg_name,
            Description="Harness - allows outbound NFS to S3 Files",
            VpcId=vpc_id,
        )["GroupId"]
        print(f"  Harness SG created: {harness_sg_id}")

    try:
        ec2.authorize_security_group_egress(
            GroupId=harness_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [{"GroupId": mt_sg_id}],
                }
            ],
        )
    except ec2.exceptions.ClientError as e:
        if "Duplicate" not in str(e) and "already exists" not in str(e):
            raise

    try:
        ec2.authorize_security_group_ingress(
            GroupId=mt_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [{"GroupId": harness_sg_id}],
                }
            ],
        )
    except ec2.exceptions.ClientError as e:
        if "Duplicate" not in str(e) and "already exists" not in str(e):
            raise

    return harness_sg_id, mt_sg_id


# ─────────────────────────────────────────────────────────────────────
# S3 Files IAM Role
# ─────────────────────────────────────────────────────────────────────


def create_s3files_role(bucket_name, prefix):
    """Create (or reuse) the IAM role S3 Files assumes to sync with the bucket. Returns role ARN."""
    iam = boto3.client("iam")
    role_name = f"{prefix}-s3files-sync-role"

    trust_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )
    role_arn = _ensure_role(iam, role_name, trust_policy, "Role for S3 Files to sync with S3 bucket")

    permissions_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{bucket_name}",
                        f"arn:aws:s3:::{bucket_name}/*",
                    ],
                }
            ],
        }
    )
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="S3FilesSyncPolicy",
        PolicyDocument=permissions_policy,
    )
    print(f"  S3 Files role: {role_name} (already exists or created)")
    time.sleep(10)
    return role_arn


# ─────────────────────────────────────────────────────────────────────
# S3 Files File System
# ─────────────────────────────────────────────────────────────────────


def create_file_system(bucket_name, s3files_role_arn, region):
    """Create (or reuse) an S3 Files file system backed by the bucket. Returns file_system_id."""
    s3files = boto3.client("s3files", region_name=region)
    bucket_arn = f"arn:aws:s3:::{bucket_name}"

    existing_id = _find_file_system_for_bucket(s3files, bucket_arn)
    if existing_id:
        print(f"  File system already exists: {existing_id}")
        for _ in range(30):
            status = s3files.get_file_system(fileSystemId=existing_id)["status"]
            if status.upper() == "AVAILABLE":
                return existing_id
            if status.upper() == "ERROR":
                break
            time.sleep(10)
        return existing_id

    fs_resp = s3files.create_file_system(
        bucket=bucket_arn,
        roleArn=s3files_role_arn,
    )
    file_system_id = fs_resp["fileSystemId"]
    print(f"  File system created: {file_system_id}")

    print("  Waiting for file system to become AVAILABLE...")
    for i in range(30):
        fs_status = s3files.get_file_system(fileSystemId=file_system_id)
        status = fs_status["status"]
        print(f"    [{i + 1}] {status}")
        if status.upper() == "AVAILABLE":
            return file_system_id
        if status.upper() == "ERROR":
            raise RuntimeError(f"File system error: {fs_status.get('statusMessage', 'unknown')}")
        time.sleep(10)

    raise TimeoutError("File system did not become AVAILABLE within timeout")


# ─────────────────────────────────────────────────────────────────────
# Mount Targets
# ─────────────────────────────────────────────────────────────────────


def create_mount_targets(file_system_id, subnet_ids, security_group_id, region):
    """Create (or reuse) one mount target per subnet. Returns list of mount_target_ids."""
    s3files = boto3.client("s3files", region_name=region)

    existing_mts = s3files.list_mount_targets(fileSystemId=file_system_id).get("mountTargets", [])
    existing_subnets = {mt["subnetId"]: mt["mountTargetId"] for mt in existing_mts}

    mount_target_ids = []
    for subnet_id in subnet_ids:
        if subnet_id in existing_subnets:
            mt_id = existing_subnets[subnet_id]
            print(f"  Mount target already exists: {mt_id} ({subnet_id})")
            mount_target_ids.append(mt_id)
        else:
            mt_resp = s3files.create_mount_target(
                fileSystemId=file_system_id,
                subnetId=subnet_id,
                securityGroups=[security_group_id],
            )
            mt_id = mt_resp["mountTargetId"]
            mount_target_ids.append(mt_id)
            print(f"  Mount target created: {mt_id} ({subnet_id})")

    print("  Waiting for mount targets to become AVAILABLE...")
    for i in range(36):
        mts = s3files.list_mount_targets(fileSystemId=file_system_id)["mountTargets"]
        statuses = [mt["status"] for mt in mts]
        if all(s.upper() == "AVAILABLE" for s in statuses):
            print("  All mount targets AVAILABLE")
            return mount_target_ids
        if i % 6 == 0:
            print(f"    Statuses: {statuses}")
        time.sleep(10)

    print("  Warning: timed out waiting for mount targets")
    return mount_target_ids


# ─────────────────────────────────────────────────────────────────────
# Access Point
# ─────────────────────────────────────────────────────────────────────


def create_access_point(file_system_id, region, uid=1000, gid=1000):
    """Create (or reuse) an S3 Files access point. Returns access_point_id."""
    s3files = boto3.client("s3files", region_name=region)

    existing_aps = s3files.list_access_points(fileSystemId=file_system_id).get("accessPoints", [])
    if existing_aps:
        ap_id = existing_aps[0]["accessPointId"]
        print(f"  Access point already exists: {ap_id}")
        return ap_id

    ap_resp = s3files.create_access_point(
        fileSystemId=file_system_id,
        posixUser={"uid": uid, "gid": gid},
        rootDirectory={"path": "/"},
    )
    access_point_id = ap_resp["accessPointId"]
    print(f"  Access point created: {access_point_id}")
    return access_point_id


# ─────────────────────────────────────────────────────────────────────
# Harness Execution Role + Mount Policy
# ─────────────────────────────────────────────────────────────────────


def _build_access_point_arn(file_system_id, access_point_id, region, account_id):
    """Construct the full ARN for an S3 Files access point."""
    return f"arn:aws:s3files:{region}:{account_id}:file-system/{file_system_id}/access-point/{access_point_id}"


def create_harness_execution_role(region, prefix, account_id):
    """Create (or reuse) the IAM execution role the harness assumes. Returns role ARN."""
    iam = boto3.client("iam")
    role_name = f"{prefix}-harness-role"

    trust = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )
    role_arn = _ensure_role(iam, role_name, trust, f"Execution role for {prefix} harness")

    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                    "Resource": "*",
                },
            ],
        }
    )
    iam.put_role_policy(RoleName=role_name, PolicyName=f"{prefix}-harness-policy", PolicyDocument=policy)
    print(f"  Harness role: {role_name} (already exists or created)")
    time.sleep(10)
    return role_arn


def _add_mount_policy_to_role(role_name, file_system_id, access_point_arn, region, account_id):
    """Add s3files:ClientMount/ClientWrite/GetAccessPoint permissions to the harness execution role."""
    iam = boto3.client("iam")
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ClientMount",
                        "s3files:ClientWrite",
                        "s3files:GetAccessPoint",
                        "s3files:ListMountTargets",
                    ],
                    "Resource": [
                        f"arn:aws:s3files:{region}:{account_id}:file-system/{file_system_id}",
                        access_point_arn,
                    ],
                }
            ],
        }
    )
    iam.put_role_policy(RoleName=role_name, PolicyName="S3FilesMountPolicy", PolicyDocument=policy)
    print(f"  Added S3FilesMountPolicy to {role_name}")
    print("  Waiting for IAM policy propagation...")
    time.sleep(15)


# ─────────────────────────────────────────────────────────────────────
# Orchestrators
# ─────────────────────────────────────────────────────────────────────


def setup_s3files_infrastructure(region, account_id, prefix="harness-s3fs-demo", subnet_ids=None):
    """Provision (or reuse) all S3 Files resources needed to mount in a Harness.

    All functions are idempotent — resources are discovered by deterministic names
    derived from the prefix. Safe to re-run; only creates what doesn't exist.

    Returns a dict with all resource IDs and the access_point_arn + harness_sg_id
    needed to create the Harness.
    """
    print("\n  S3 bucket...")
    bucket_name = create_bucket(region, account_id, prefix)

    print("\n  VPC subnets...")
    if subnet_ids:
        print(f"  Using provided subnets: {subnet_ids}")
    else:
        subnet_ids = get_default_vpc_subnets(region)
    vpc_id, azs = _get_vpc_from_subnets(subnet_ids, region)
    print(f"  Availability Zones: {azs}")

    print("\n  Security groups...")
    harness_sg_id, mt_sg_id = create_security_groups(vpc_id, region, prefix)

    print("\n  S3 Files sync role...")
    s3files_role_arn = create_s3files_role(bucket_name, prefix)

    print("\n  S3 Files file system...")
    file_system_id = create_file_system(bucket_name, s3files_role_arn, region)

    print("\n  Mount targets...")
    mount_target_ids = create_mount_targets(file_system_id, subnet_ids, mt_sg_id, region)

    print("\n  Access point...")
    access_point_id = create_access_point(file_system_id, region)
    access_point_arn = _build_access_point_arn(file_system_id, access_point_id, region, account_id)
    print(f"  Access point ARN: {access_point_arn}")

    print("\n  Harness execution role...")
    harness_role_arn = create_harness_execution_role(region, prefix, account_id)

    print("\n  S3 Files mount permissions...")
    role_name = f"{prefix}-harness-role"
    _add_mount_policy_to_role(role_name, file_system_id, access_point_arn, region, account_id)

    return {
        "bucket_name": bucket_name,
        "subnet_ids": subnet_ids,
        "file_system_id": file_system_id,
        "access_point_id": access_point_id,
        "access_point_arn": access_point_arn,
        "mount_target_ids": mount_target_ids,
        "harness_sg_id": harness_sg_id,
        "mt_sg_id": mt_sg_id,
        "s3files_role_arn": s3files_role_arn,
        "harness_role_arn": harness_role_arn,
        "harness_role_name": role_name,
        "prefix": prefix,
    }


def cleanup_all(region, prefix):
    """Delete every resource created by this script, in reverse order.

    Discovers resources by name so it works even after a fresh Python session.
    Skips gracefully if a resource was never created.
    """
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    ec2 = boto3.client("ec2", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    s3files = boto3.client("s3files", region_name=region)
    iam = boto3.client("iam")

    bucket_name = f"{prefix}-{account_id}-{region}"
    harness_sg_name = f"{prefix}-s3files-harness"
    mt_sg_name = f"{prefix}-s3files-mt"
    s3files_role_name = f"{prefix}-s3files-sync-role"
    harness_role_name = f"{prefix}-harness-role"
    harness_policy_name = f"{prefix}-harness-policy"

    deleted, skipped = [], []

    def ok(m):
        deleted.append(m)
        print(f"  ✓ {m}")

    def skip(m):
        skipped.append(m)
        print(f"  - {m}")

    # 1. S3 Files resources
    print("\n[1/6] S3 Files resources (access point, mount targets, file system)")
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    file_system_id = _find_file_system_for_bucket(s3files, bucket_arn)

    if file_system_id:
        for ap in s3files.list_access_points(fileSystemId=file_system_id).get("accessPoints", []):
            try:
                s3files.delete_access_point(accessPointId=ap["accessPointId"])
                ok(f"Access point {ap['accessPointId']} deleted")
            except Exception as e:
                skip(f"Access point: {e}")

        for mt in s3files.list_mount_targets(fileSystemId=file_system_id).get("mountTargets", []):
            try:
                s3files.delete_mount_target(mountTargetId=mt["mountTargetId"])
                ok(f"Mount target {mt['mountTargetId']} deleted")
            except Exception as e:
                skip(f"Mount target: {e}")

        print("  Waiting for mount targets to delete...")
        for _ in range(24):
            mts = s3files.list_mount_targets(fileSystemId=file_system_id).get("mountTargets", [])
            if not mts:
                break
            statuses = [mt.get("status", "UNKNOWN") for mt in mts]
            print(f"    Mount target statuses: {statuses}")
            time.sleep(10)

        try:
            s3files.delete_file_system(fileSystemId=file_system_id)
            ok(f"File system {file_system_id} deleted")
        except Exception as e:
            skip(f"File system: {e}")
    else:
        skip("File system not found")

    # 2. Security groups
    print("\n[2/6] Security groups")
    for sg_name in [mt_sg_name, harness_sg_name]:
        resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_name]}])
        for sg in resp["SecurityGroups"]:
            try:
                ec2.delete_security_group(GroupId=sg["GroupId"])
                ok(f"SG {sg['GroupId']} ({sg_name}) deleted")
            except Exception as e:
                skip(f"SG {sg_name}: {e}")
        if not resp["SecurityGroups"]:
            skip(f"SG {sg_name} not found")

    # 3. S3 Files sync role
    print("\n[3/6] S3 Files sync role")
    try:
        iam.get_role(RoleName=s3files_role_name)
        try:
            iam.delete_role_policy(RoleName=s3files_role_name, PolicyName="S3FilesSyncPolicy")
        except Exception:
            pass
        iam.delete_role(RoleName=s3files_role_name)
        ok(f"Role {s3files_role_name} deleted")
    except iam.exceptions.NoSuchEntityException:
        skip(f"Role {s3files_role_name} not found")

    # 4. Harness execution role
    print("\n[4/6] Harness execution role")
    try:
        iam.get_role(RoleName=harness_role_name)
        try:
            iam.delete_role_policy(RoleName=harness_role_name, PolicyName=harness_policy_name)
        except Exception:
            pass
        try:
            iam.delete_role_policy(RoleName=harness_role_name, PolicyName="S3FilesMountPolicy")
        except Exception:
            pass
        iam.delete_role(RoleName=harness_role_name)
        ok(f"Role {harness_role_name} deleted")
    except iam.exceptions.NoSuchEntityException:
        skip(f"Role {harness_role_name} not found")

    # 5. S3 bucket
    print("\n[5/6] S3 bucket")
    try:
        s3.head_bucket(Bucket=bucket_name)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = page.get("Contents", [])
            if objects:
                s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
        s3.delete_bucket(Bucket=bucket_name)
        ok(f"Bucket {bucket_name} deleted")
    except s3.exceptions.ClientError as e:
        if "404" in str(e) or "NoSuchBucket" in str(e):
            skip(f"Bucket {bucket_name} not found")
        else:
            skip(f"Bucket: {e}")

    # 6. Summary
    print(f"\n{'=' * 50}")
    print(f"Deleted {len(deleted)} resources, skipped {len(skipped)}")
    print("Cleanup complete!")
