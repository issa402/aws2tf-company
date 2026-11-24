import csv
import os
import tempfile
import unittest
from pathlib import Path

from company_imports import (
    MappingError,
    discover_imports,
    generate_imports,
    suggested_domain,
    write_review_csv,
)


class CompanyImportMappingTests(unittest.TestCase):
    def test_discovers_aws2tf_import_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "import__aws_vpc__vpc-123.tf").write_text(
                'import {\n  to = aws_vpc.vpc-123\n  id = "vpc-123"\n}\n',
                encoding="utf-8",
            )

            rows = discover_imports(root)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_address, "aws_vpc.vpc-123")
        self.assertEqual(rows[0].import_id, "vpc-123")
        self.assertEqual(rows[0].suggested_domain, "vpc-foundation")

    def test_review_csv_starts_pending_without_guessing_target_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "import__aws_route_table__rtb-123.tf").write_text(
                'import {\n  to = aws_route_table.rtb-123\n  id = "rtb-123"\n}\n',
                encoding="utf-8",
            )
            output = root / "review.csv"

            write_review_csv(discover_imports(root), output)
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["decision"], "pending")
        self.assertEqual(row["target_address"], "")
        self.assertEqual(row["suggested_domain"], "routing")

    def test_generates_final_module_import_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            self._write_mapping(
                mapping,
                [
                    {
                        "source_file": "import__aws_vpc__vpc-123.tf",
                        "resource_type": "aws_vpc",
                        "source_address": "aws_vpc.vpc-123",
                        "import_id": "vpc-123",
                        "suggested_domain": "vpc-foundation",
                        "target_address": 'module.hub_network.aws_vpc.this["hub"]',
                        "decision": "import",
                        "notes": "",
                    }
                ],
            )

            count = generate_imports(mapping, output)
            rendered = output.read_text(encoding="utf-8")

        self.assertEqual(count, 1)
        self.assertIn('to = module.hub_network.aws_vpc.this["hub"]', rendered)
        self.assertIn('id = "vpc-123"', rendered)

    def test_refuses_pending_or_missing_target_mappings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            self._write_mapping(
                mapping,
                [
                    {
                        "source_file": "x.tf",
                        "resource_type": "aws_subnet",
                        "source_address": "aws_subnet.subnet-123",
                        "import_id": "subnet-123",
                        "suggested_domain": "vpc-foundation",
                        "target_address": "",
                        "decision": "pending",
                        "notes": "",
                    }
                ],
            )

            with self.assertRaisesRegex(MappingError, "pending"):
                generate_imports(mapping, output)

    def test_refuses_duplicate_or_unsafe_target_addresses(self):
        base = {
            "source_file": "x.tf",
            "resource_type": "aws_subnet",
            "source_address": "aws_subnet.subnet-123",
            "import_id": "subnet-123",
            "suggested_domain": "vpc-foundation",
            "target_address": "module.vpc.aws_subnet.this[0]",
            "decision": "import",
            "notes": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            self._write_mapping(mapping, [base, {**base, "import_id": "subnet-456"}])
            with self.assertRaisesRegex(MappingError, "duplicate target"):
                generate_imports(mapping, output)

            self._write_mapping(
                mapping,
                [{**base, "target_address": 'aws_subnet.this; local-exec = "bad"'}],
            )
            with self.assertRaisesRegex(MappingError, "invalid target"):
                generate_imports(mapping, output)

            self._write_mapping(
                mapping,
                [{**base, "target_address": 'module.vpc["${bad}"].aws_subnet.this'}],
            )
            with self.assertRaisesRegex(MappingError, "invalid target"):
                generate_imports(mapping, output)

    def test_refuses_resource_type_mismatch_and_duplicate_aws_identity(self):
        base = {
            "source_file": "x.tf",
            "resource_type": "aws_vpc",
            "source_address": "aws_vpc.first",
            "import_id": "vpc-123",
            "suggested_domain": "vpc-foundation",
            "target_address": "module.network.aws_subnet.this",
            "decision": "import",
            "notes": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            self._write_mapping(mapping, [base])
            with self.assertRaisesRegex(MappingError, "resource type mismatch"):
                generate_imports(mapping, output)

            first = {**base, "target_address": "module.network.aws_vpc.first"}
            second = {
                **base,
                "source_address": "aws_vpc.second",
                "target_address": "module.network.aws_vpc.second",
            }
            self._write_mapping(mapping, [first, second])
            with self.assertRaisesRegex(MappingError, "duplicate AWS identity"):
                generate_imports(mapping, output)

    def test_malformed_import_file_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "import__aws_vpc__valid.tf").write_text(
                'import {\n  to = aws_vpc.valid\n  id = "vpc-123"\n}\n',
                encoding="utf-8",
            )
            (root / "import__aws_subnet__broken.tf").write_text(
                "import { this is truncated", encoding="utf-8"
            )

            with self.assertRaisesRegex(MappingError, "no parseable import block"):
                discover_imports(root)

    def test_import_id_escapes_terraform_template_sequences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            self._write_mapping(
                mapping,
                [
                    {
                        "source_file": "x.tf",
                        "resource_type": "aws_s3_bucket",
                        "source_address": "aws_s3_bucket.example",
                        "import_id": '${file("/tmp/secret")}%{if true}',
                        "suggested_domain": "review-non-network",
                        "target_address": "module.storage.aws_s3_bucket.this",
                        "decision": "import",
                        "notes": "",
                    }
                ],
            )

            generate_imports(mapping, output)
            rendered = output.read_text(encoding="utf-8")

        self.assertIn('$${file(\\"/tmp/secret\\")}%%{if true}', rendered)
        self.assertNotIn('id = "${', rendered)

    def test_formula_like_aws_id_is_reversibly_encoded_in_review_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "import__aws_s3_bucket__formula.tf").write_text(
                'import {\n  to = aws_s3_bucket.formula\n  id = "=HYPERLINK(\\"bad\\")"\n}\n',
                encoding="utf-8",
            )
            mapping = root / "mapping.csv"
            output = root / "imports.auto.tf"
            write_review_csv(discover_imports(root), mapping)
            raw_csv = mapping.read_text(encoding="utf-8")
            with mapping.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["target_address"] = "module.storage.aws_s3_bucket.this"
            rows[0]["decision"] = "import"
            self._write_mapping(mapping, rows)

            generate_imports(mapping, output)
            rendered = output.read_text(encoding="utf-8")

        self.assertNotIn(",=HYPERLINK", raw_csv)
        self.assertIn('id = "=HYPERLINK', rendered)

    def test_private_outputs_reject_symlinks_and_use_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "import__aws_vpc__vpc-123.tf"
            source.write_text(
                'import {\n  to = aws_vpc.vpc-123\n  id = "vpc-123"\n}\n',
                encoding="utf-8",
            )
            victim = root / "victim.txt"
            victim.write_text("do not replace", encoding="utf-8")
            output = root / "mapping.csv"
            output.symlink_to(victim)

            with self.assertRaisesRegex(MappingError, "symlink"):
                write_review_csv(discover_imports(root), output)

            self.assertEqual(victim.read_text(encoding="utf-8"), "do not replace")
            output.unlink()
            write_review_csv(discover_imports(root), output)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_malformed_csv_row_returns_mapping_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.csv"
            output = Path(temp_dir) / "imports.auto.tf"
            mapping.write_text(
                ",".join(self._fieldnames()) + "\nonly-one-cell\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MappingError, "malformed CSV row"):
                generate_imports(mapping, output)

    def test_network_domains_are_suggested_without_changing_ownership(self):
        self.assertEqual(suggested_domain("aws_ec2_transit_gateway"), "transit")
        self.assertEqual(suggested_domain("aws_networkfirewall_firewall"), "inspection")
        self.assertEqual(suggested_domain("aws_ram_resource_share"), "ram-sharing")
        self.assertEqual(suggested_domain("aws_lambda_function"), "review-non-network")

    @staticmethod
    def _write_mapping(path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames = CompanyImportMappingTests._fieldnames()
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _fieldnames() -> list[str]:
        return [
            "source_file",
            "resource_type",
            "source_address",
            "import_id",
            "suggested_domain",
            "target_address",
            "decision",
            "notes",
        ]


if __name__ == "__main__":
    unittest.main()
