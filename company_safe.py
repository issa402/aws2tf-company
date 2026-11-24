#!/usr/bin/env python3
"""Fail-closed wrapper for evaluating aws2tf in company AWS accounts.

The upstream CLI imports resources into local Terraform state during a normal
run. This wrapper always supplies ``--validate`` so the discovery/generation
pipeline stops before that import apply. It requires an explicit account,
region, profile, and either a scoped resource type or explicit full inventory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


ACCOUNT_RE = re.compile(r"^\d{12}$")
REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d{1,2}$")
RESOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# These paths can retrieve secret values, instance user data, or deployment
# packages. They are outside the network-migration evaluation scope.
BLOCKED_RESOURCE_TYPES = {
    "all",
    "asg",
    "autoscaling",
    "ec2",
    "lambda",
    "secret",
    "secrets",
    "stack",
    "aws_instance",
    "aws_launch_configuration",
    "aws_launch_template",
    "aws_lambda_function",
    "aws_lambda_layer_version",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_version",
}

APPROVED_NETWORK_ALIASES = {
    "cloudwan",
    "igw",
    "natgw",
    "net",
    "nfw",
    "privatelink",
    "ram",
    "route53",
    "security_group",
    "subnet",
    "tgw",
    "vpc",
    "vpclattice",
    "wan",
}

APPROVED_NETWORK_EXACT_TYPES = {
    "aws_customer_gateway",
    "aws_default_internet_gateway",
    "aws_default_network_acl",
    "aws_default_route_table",
    "aws_default_security_group",
    "aws_default_subnet",
    "aws_default_vpc",
    "aws_egress_only_internet_gateway",
    "aws_eip",
    "aws_eip_association",
    "aws_ec2_managed_prefix_list",
    "aws_ec2_transit_gateway",
    "aws_flow_log",
    "aws_internet_gateway",
    "aws_internet_gateway_attachment",
    "aws_main_route_table_association",
    "aws_nat_gateway",
    "aws_network_acl",
    "aws_network_interface",
    "aws_route",
    "aws_route_table",
    "aws_route_table_association",
    "aws_security_group",
    "aws_security_group_rule",
    "aws_subnet",
    "aws_vpc",
    "aws_vpc_dhcp_options",
    "aws_vpn_connection",
    "aws_vpn_gateway",
}

APPROVED_NETWORK_PREFIXES = (
    "aws_dx_",
    "aws_ec2_transit_gateway_",
    "aws_networkfirewall_",
    "aws_networkmanager_",
    "aws_ram_",
    "aws_route53_",
    "aws_vpc_",
    "aws_vpclattice_",
    "aws_vpn_",
)


class SafetyError(ValueError):
    """Raised when a requested run violates a company safety boundary."""


def validate_account(actual_account: str, expected_account: str) -> str:
    """Return the verified account ID or fail closed on malformed/mismatched IDs."""
    if not ACCOUNT_RE.fullmatch(expected_account):
        raise SafetyError("Expected AWS account must be exactly 12 digits")
    if not ACCOUNT_RE.fullmatch(actual_account):
        raise SafetyError("STS returned an invalid AWS account ID")
    if actual_account != expected_account:
        raise SafetyError(
            f"AWS account mismatch: expected {expected_account}, got {actual_account}"
        )
    return actual_account


def validate_resource_scope(resource_type: str) -> list[str]:
    """Validate a non-empty, explicitly scoped resource type list."""
    resource_types = [item.strip() for item in resource_type.split(",") if item.strip()]
    if not resource_types:
        raise SafetyError("A scoped --type is required; unrestricted scans are disabled")

    for item in resource_types:
        if not RESOURCE_TYPE_RE.fullmatch(item):
            raise SafetyError(f"Invalid resource type: {item}")
        if item in BLOCKED_RESOURCE_TYPES:
            raise SafetyError(f"Resource type is blocked in company-safe mode: {item}")
        approved = (
            item in APPROVED_NETWORK_ALIASES
            or item in APPROVED_NETWORK_EXACT_TYPES
            or item.startswith(APPROVED_NETWORK_PREFIXES)
        )
        if not approved:
            raise SafetyError(
                f"Resource type is not approved for the networking evaluation: {item}"
            )
    return resource_types


def build_aws2tf_command(
    *,
    python_path: str,
    script_path: str,
    profile: str | None,
    region: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    full_inventory: bool = False,
) -> list[str]:
    """Build an argument-list invocation that can never request upstream apply."""
    if profile is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise SafetyError("Profile must contain only letters, numbers, hyphens, or underscores")
    if not REGION_RE.fullmatch(region):
        raise SafetyError(f"Invalid AWS region: {region}")
    if full_inventory and (resource_type or resource_id):
        raise SafetyError("--full-inventory cannot be combined with --type or --id")
    if not full_inventory:
        validate_resource_scope(resource_type or "")

    command = [
        python_path,
        script_path,
        "--validate",
        "--region",
        region,
    ]
    if profile is not None:
        command.extend(["--profile", profile])
    if resource_type:
        command.extend(["--type", resource_type])
    if resource_id:
        command.extend(["--id", resource_id])
    return command


def write_run_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Write provenance without making it readable by other local users."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def validate_environment_credentials(environment: Mapping[str, str]) -> None:
    """Fail before AWS calls when terminal-provided credentials are incomplete."""
    access_key = environment.get("AWS_ACCESS_KEY_ID", "")
    if not access_key:
        raise SafetyError("AWS_ACCESS_KEY_ID is required for environment credentials")
    if not environment.get("AWS_SECRET_ACCESS_KEY"):
        raise SafetyError("AWS_SECRET_ACCESS_KEY is required for environment credentials")
    if access_key.startswith("ASIA") and not environment.get("AWS_SESSION_TOKEN"):
        raise SafetyError("AWS_SESSION_TOKEN is required for temporary ASIA credentials")


def get_actual_account(profile: str | None) -> str:
    """Read the caller account through the AWS CLI without logging credentials."""
    command = ["aws", "sts", "get-caller-identity"]
    if profile is not None:
        command.extend(["--profile", profile])
    command.extend(["--output", "json"])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    return str(payload.get("Account", ""))


def get_git_commit(repo_root: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_worktree_provenance(repo_root: Path) -> dict[str, Any]:
    """Record whether the checkout differs from the named fork commit."""
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fingerprint = hashlib.sha256((status + diff).encode("utf-8")).hexdigest()
    return {
        "worktree_dirty": bool(status),
        "worktree_fingerprint_sha256": fingerprint,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-account", required=True)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--profile")
    credentials.add_argument(
        "--use-environment-credentials",
        action="store_true",
        help="Use AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY and optional AWS_SESSION_TOKEN",
    )
    parser.add_argument("--region", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--type", dest="resource_type")
    scope.add_argument(
        "--full-inventory",
        action="store_true",
        help="Inventory every supported resource family without importing state",
    )
    parser.add_argument("--id", dest="resource_id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify identity and print the validation-only command without running aws2tf",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent

    try:
        if args.use_environment_credentials:
            validate_environment_credentials(os.environ)
        actual_account = validate_account(
            get_actual_account(args.profile), args.expected_account
        )
        command = build_aws2tf_command(
            python_path=str(repo_root / ".venv" / "bin" / "python"),
            script_path=str(repo_root / "aws2tf.py"),
            profile=args.profile,
            region=args.region,
            resource_type=args.resource_type,
            resource_id=args.resource_id,
            full_inventory=args.full_inventory,
        )
    except (SafetyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"SAFETY STOP: {exc}", file=sys.stderr)
        return 2

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = repo_root / "company-runs" / f"{timestamp}.json"
    fork_commit = get_git_commit(repo_root, "HEAD")
    worktree_provenance = get_worktree_provenance(repo_root)
    try:
        upstream_base_commit = get_git_commit(repo_root, "upstream/master")
    except subprocess.CalledProcessError:
        upstream_base_commit = get_git_commit(repo_root, "origin/master")
    write_run_manifest(
        manifest_path,
        {
            "account": actual_account,
            "command": command,
            "credential_source": args.profile or "environment",
            "fork_commit": fork_commit,
            "mode": "validation-only",
            "payload_suppression": "best-effort",
            "selected_handler_payload_suppression": True,
            "profile": args.profile,
            "region": args.region,
            "resource_id": args.resource_id,
            "resource_type": args.resource_type,
            "scope": "full-inventory" if args.full_inventory else "scoped",
            "sensitive_provider_reads_possible": True,
            "timestamp_utc": timestamp,
            "upstream_base_commit": upstream_base_commit,
            **worktree_provenance,
        },
    )

    print(f"Verified account: {actual_account}")
    print(f"Run manifest: {manifest_path}")
    print(f"Command: {shlex.join(command)}")
    if args.full_inventory:
        print(
            "WARNING: Full inventory refreshes complete resource configurations. "
            "Read-only prevents mutation, not sensitive reads; treat all output as sensitive."
        )
    if args.dry_run:
        return 0

    run_environment = {
        **os.environ,
        "AWS2TF_METADATA_ONLY": "1",
        "AWS2TF_USE_ENVIRONMENT_CREDENTIALS": (
            "1" if args.use_environment_credentials else "0"
        ),
    }
    previous_umask = os.umask(0o077)
    try:
        return subprocess.run(
            command, cwd=repo_root, check=False, env=run_environment
        ).returncode
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
