#!/usr/bin/env python3
"""Create a reviewed bridge from aws2tf imports to final module addresses."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CSV_FIELDS = (
    "source_file",
    "resource_type",
    "source_address",
    "import_id",
    "suggested_domain",
    "target_address",
    "decision",
    "notes",
)

IMPORT_BLOCK_RE = re.compile(r"(?:^|\n)\s*import\s*\{(?P<body>.*?)\n\s*\}", re.DOTALL)
TO_RE = re.compile(r"^\s*to\s*=\s*(?P<value>[^\n]+?)\s*$", re.MULTILINE)
ID_RE = re.compile(
    r'^\s*id\s*=\s*(?P<value>"(?:[^"\\]|\\.)*")\s*$', re.MULTILINE
)
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
INDEX = r'(?:\[(?:[0-9]+|"[A-Za-z0-9_.:/@+=, -]+")\])?'
TARGET_ADDRESS_RE = re.compile(
    rf"^(?:module\.{IDENTIFIER}{INDEX}\.)*aws_[a-z0-9_]+\.{IDENTIFIER}{INDEX}$"
)
CSV_ENCODE_PREFIX = "aws2tf-b64:"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class MappingError(ValueError):
    """Raised when import evidence or a reviewed mapping is unsafe/incomplete."""


@dataclass(frozen=True)
class ImportRecord:
    source_file: str
    resource_type: str
    source_address: str
    import_id: str
    suggested_domain: str


def suggested_domain(resource_type: str) -> str:
    """Suggest a review bucket without claiming final ownership or module design."""
    if resource_type.startswith("aws_ec2_transit_gateway") or resource_type in {
        "aws_customer_gateway",
        "aws_vpn_connection",
        "aws_vpn_gateway",
    }:
        return "transit"
    if resource_type.startswith("aws_networkfirewall_"):
        return "inspection"
    if resource_type.startswith("aws_ram_"):
        return "ram-sharing"
    if resource_type.startswith(("aws_route53_", "aws_route53resolver_")):
        return "dns"
    if resource_type.startswith("aws_vpc_endpoint") or resource_type.startswith(
        "aws_vpclattice_"
    ):
        return "endpoints"
    if resource_type in {"aws_security_group", "aws_security_group_rule"}:
        return "security"
    if resource_type in {"aws_flow_log", "aws_cloudwatch_log_group"}:
        return "observability"
    if resource_type in {
        "aws_route",
        "aws_route_table",
        "aws_route_table_association",
        "aws_main_route_table_association",
        "aws_ec2_managed_prefix_list",
    }:
        return "routing"
    if resource_type in {
        "aws_vpc",
        "aws_subnet",
        "aws_internet_gateway",
        "aws_nat_gateway",
        "aws_eip",
        "aws_network_acl",
        "aws_network_acl_rule",
        "aws_vpc_dhcp_options",
    }:
        return "vpc-foundation"
    if resource_type.startswith(("aws_dx_", "aws_networkmanager_")):
        return "connectivity"
    return "review-non-network"


def discover_imports(generated_dir: Path) -> list[ImportRecord]:
    """Parse and deduplicate literal import blocks produced by aws2tf."""
    root = generated_dir.resolve()
    if not root.is_dir():
        raise MappingError(f"generated directory does not exist: {generated_dir}")

    records: dict[tuple[str, str], ImportRecord] = {}
    for path in sorted(root.rglob("import__*.tf")):
        if "notimported" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8")
        matches = list(IMPORT_BLOCK_RE.finditer(text))
        if not matches:
            raise MappingError(f"no parseable import block in {path}")
        for match in matches:
            body = match.group("body")
            to_match = TO_RE.search(body)
            id_match = ID_RE.search(body)
            if not to_match or not id_match:
                raise MappingError(f"unsupported import block in {path}")
            source_address = to_match.group("value").strip()
            if not TARGET_ADDRESS_RE.fullmatch(source_address):
                raise MappingError(f"invalid source Terraform address in {path}")
            try:
                import_id = json.loads(id_match.group("value"))
            except json.JSONDecodeError as exc:
                raise MappingError(f"invalid import ID string in {path}") from exc
            if not isinstance(import_id, str) or not import_id:
                raise MappingError(f"empty import ID in {path}")
            resource_type = _resource_type(source_address)
            record = ImportRecord(
                source_file=str(path.relative_to(root)),
                resource_type=resource_type,
                source_address=source_address,
                import_id=import_id.replace("$${", "${").replace("%%{", "%{"),
                suggested_domain=suggested_domain(resource_type),
            )
            records[(source_address, record.import_id)] = record

    if not records:
        raise MappingError(f"no aws2tf import blocks found under {generated_dir}")
    return sorted(records.values(), key=lambda item: (item.resource_type, item.import_id))


def write_review_csv(records: Iterable[ImportRecord], output: Path) -> None:
    """Write a private review ledger; final addresses are intentionally blank."""
    rows = list(records)
    if not rows:
        raise MappingError("cannot write an empty import review")
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for record in rows:
        writer.writerow(
            {
                key: _csv_encode(str(value))
                for key, value in {
                    **record.__dict__,
                    "target_address": "",
                    "decision": "pending",
                    "notes": "",
                }.items()
            }
        )
    _write_private_text(output, handle.getvalue())


def generate_imports(mapping_csv: Path, output: Path) -> int:
    """Validate a completed review ledger and render final Terraform imports."""
    rows = _read_mapping(mapping_csv)
    blocks: list[str] = []
    targets: set[str] = set()
    identities: set[tuple[str, str]] = set()

    for line_number, row in enumerate(rows, start=2):
        decision = row["decision"].strip().lower()
        if decision == "pending" or not decision:
            raise MappingError(f"line {line_number}: pending mapping must be reviewed")
        if decision in {"external", "ignore"}:
            continue
        if decision != "import":
            raise MappingError(f"line {line_number}: invalid decision {decision!r}")

        target = row["target_address"].strip()
        if not TARGET_ADDRESS_RE.fullmatch(target):
            raise MappingError(f"line {line_number}: invalid target Terraform address")
        if target in targets:
            raise MappingError(f"line {line_number}: duplicate target address {target}")
        if not row["import_id"]:
            raise MappingError(f"line {line_number}: import ID is required")

        resource_type = row["resource_type"].strip()
        source_type = _resource_type(row["source_address"].strip())
        target_type = _resource_type(target)
        if resource_type != source_type or resource_type != target_type:
            raise MappingError(
                f"line {line_number}: resource type mismatch "
                f"({source_type}, {resource_type}, {target_type})"
            )
        identity = (resource_type, row["import_id"])
        if identity in identities:
            raise MappingError(f"line {line_number}: duplicate AWS identity")

        targets.add(target)
        identities.add(identity)
        import_id = _hcl_string_value(row["import_id"])
        blocks.append(
            "import {\n"
            f"  to = {target}\n"
            f"  id = {import_id}\n"
            "}\n"
        )

    if not blocks:
        raise MappingError("no rows were approved for import")
    _write_private_text(output, "\n".join(blocks))
    return len(blocks)


def _read_mapping(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise MappingError(f"mapping CSV does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise MappingError("mapping CSV columns were changed or are incomplete")
        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise MappingError(f"line {line_number}: malformed CSV row")
            rows.append({key: _csv_decode(value) for key, value in row.items()})
        return rows


def _resource_type(address: str) -> str:
    matches = re.findall(r"(?:^|\.)(aws_[a-z0-9_]+)\.", address)
    if not matches:
        raise MappingError(f"address has no AWS resource type: {address}")
    return matches[-1]


def _hcl_string_value(value: str) -> str:
    escaped = value.replace("${", "$${").replace("%{", "%%{")
    return json.dumps(escaped)


def _csv_encode(value: str) -> str:
    if value.startswith(CSV_ENCODE_PREFIX) or value.startswith(FORMULA_PREFIXES):
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
        return CSV_ENCODE_PREFIX + encoded
    return value


def _csv_decode(value: str) -> str:
    if not value.startswith(CSV_ENCODE_PREFIX):
        return value
    payload = value[len(CSV_ENCODE_PREFIX) :]
    try:
        decoded = base64.b64decode(payload.encode("ascii"), altchars=b"-_", validate=True)
        return decoded.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise MappingError("invalid encoded CSV cell") from exc


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MappingError(f"refusing symlink output path: {path}")
    if path.exists() and not path.is_file():
        raise MappingError(f"output path is not a regular file: {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export", help="Export aws2tf import blocks into a review CSV"
    )
    export_parser.add_argument("--generated-dir", required=True, type=Path)
    export_parser.add_argument("--output", required=True, type=Path)

    generate_parser = subparsers.add_parser(
        "generate", help="Generate final import blocks from a completed review CSV"
    )
    generate_parser.add_argument("--mapping", required=True, type=Path)
    generate_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "export":
            records = discover_imports(args.generated_dir)
            write_review_csv(records, args.output)
            print(f"Exported {len(records)} imports to {args.output}")
        else:
            count = generate_imports(args.mapping, args.output)
            print(f"Generated {count} reviewed imports in {args.output}")
    except (MappingError, OSError) as exc:
        print(f"IMPORT MAPPING STOP: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
