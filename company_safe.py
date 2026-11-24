#!/usr/bin/env python3
"""Fail-closed wrapper for evaluating aws2tf in company AWS accounts.

The upstream CLI imports resources into local Terraform state during a normal
run. This wrapper always supplies ``--validate`` so the discovery/generation
pipeline stops before that import apply. It also requires an explicit account,
region, profile, and resource scope.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ACCOUNT_RE = re.compile(r"^\d{12}$")
REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d{1,2}$")
RESOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# These paths can retrieve secret values, instance user data, or deployment
# packages. They are outside the network-migration evaluation scope.
BLOCKED_RESOURCE_TYPES = {
    "all",
    "stack",
    "ec2",
    "lambda",
    "secretsmanager",
    "autoscaling",
    "aws_instance",
    "aws_launch_configuration",
    "aws_lambda_function",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_version",
}


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
    return resource_types


def build_aws2tf_command(
    *,
    python_path: str,
    script_path: str,
    profile: str,
    region: str,
    resource_type: str,
    resource_id: str | None = None,
) -> list[str]:
    """Build an argument-list invocation that can never request upstream apply."""
    if not profile or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise SafetyError("Profile must contain only letters, numbers, hyphens, or underscores")
    if not REGION_RE.fullmatch(region):
        raise SafetyError(f"Invalid AWS region: {region}")
    validate_resource_scope(resource_type)

    command = [
        python_path,
        script_path,
        "--validate",
        "--profile",
        profile,
        "--region",
        region,
        "--type",
        resource_type,
    ]
    if resource_id:
        command.extend(["--id", resource_id])
    return command


def write_run_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Write provenance without making it readable by other local users."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def get_actual_account(profile: str) -> str:
    """Read the caller account through the AWS CLI without logging credentials."""
    result = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    return str(payload.get("Account", ""))


def get_upstream_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--type", required=True, dest="resource_type")
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
        )
    except (SafetyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"SAFETY STOP: {exc}", file=sys.stderr)
        return 2

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = repo_root / "company-runs" / f"{timestamp}.json"
    write_run_manifest(
        manifest_path,
        {
            "account": actual_account,
            "command": command,
            "mode": "validation-only",
            "profile": args.profile,
            "region": args.region,
            "resource_id": args.resource_id,
            "resource_type": args.resource_type,
            "timestamp_utc": timestamp,
            "upstream_commit": get_upstream_commit(repo_root),
        },
    )

    print(f"Verified account: {actual_account}")
    print(f"Run manifest: {manifest_path}")
    print(f"Command: {shlex.join(command)}")
    if args.dry_run:
        return 0

    return subprocess.run(command, cwd=repo_root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
