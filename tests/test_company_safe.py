import json
import tempfile
import unittest
from pathlib import Path

from company_safe import (
    SafetyError,
    build_aws2tf_command,
    validate_account,
    write_run_manifest,
)


class CompanySafeWrapperTests(unittest.TestCase):
    def test_account_mismatch_fails_closed(self):
        with self.assertRaisesRegex(SafetyError, "AWS account mismatch"):
            validate_account("111122223333", "999900001111")

    def test_account_match_is_accepted(self):
        self.assertEqual(
            validate_account("111122223333", "111122223333"),
            "111122223333",
        )

    def test_command_is_always_scoped_and_validation_only(self):
        command = build_aws2tf_command(
            python_path="/repo/.venv/bin/python",
            script_path="/repo/aws2tf.py",
            profile="network-readonly",
            region="us-east-1",
            resource_type="aws_vpc",
            resource_id="vpc-0123456789abcdef0",
        )

        self.assertEqual(command[:2], ["/repo/.venv/bin/python", "/repo/aws2tf.py"])
        self.assertIn("--validate", command)
        self.assertNotIn("--accept", command)
        self.assertEqual(command[command.index("--type") + 1], "aws_vpc")
        self.assertEqual(command[command.index("--id") + 1], "vpc-0123456789abcdef0")

    def test_unrestricted_scan_is_rejected(self):
        for resource_type in ("", "all", "stack"):
            with self.subTest(resource_type=resource_type):
                with self.assertRaises(SafetyError):
                    build_aws2tf_command(
                        python_path="python",
                        script_path="aws2tf.py",
                        profile="network-readonly",
                        region="us-east-1",
                        resource_type=resource_type,
                    )

    def test_sensitive_resource_types_are_rejected(self):
        for resource_type in (
            "aws_secretsmanager_secret_version",
            "aws_lambda_function",
            "aws_instance",
            "aws_launch_configuration",
        ):
            with self.subTest(resource_type=resource_type):
                with self.assertRaisesRegex(SafetyError, "blocked"):
                    build_aws2tf_command(
                        python_path="python",
                        script_path="aws2tf.py",
                        profile="network-readonly",
                        region="us-east-1",
                        resource_type=resource_type,
                    )

    def test_manifest_is_private_and_contains_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "run.json"
            write_run_manifest(
                manifest_path,
                {
                    "account": "111122223333",
                    "region": "us-east-1",
                    "upstream_commit": "abc123",
                },
            )

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["upstream_commit"], "abc123")
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
