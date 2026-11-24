import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import company_safe
from company_safe import (
    SafetyError,
    build_aws2tf_command,
    get_actual_account,
    get_worktree_provenance,
    main,
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

    def test_implicit_unrestricted_scan_is_rejected(self):
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

    def test_explicit_full_inventory_is_validation_only_and_unscoped(self):
        command = build_aws2tf_command(
            python_path="python",
            script_path="aws2tf.py",
            profile="inventory-readonly",
            region="us-east-1",
            full_inventory=True,
        )

        self.assertIn("--validate", command)
        self.assertNotIn("--accept", command)
        self.assertNotIn("--type", command)
        self.assertNotIn("--id", command)

    def test_full_inventory_cannot_be_combined_with_type_or_id(self):
        with self.assertRaisesRegex(SafetyError, "cannot be combined"):
            build_aws2tf_command(
                python_path="python",
                script_path="aws2tf.py",
                profile="inventory-readonly",
                region="us-east-1",
                resource_type="aws_vpc",
                full_inventory=True,
            )
        with self.assertRaisesRegex(SafetyError, "cannot be combined"):
            build_aws2tf_command(
                python_path="python",
                script_path="aws2tf.py",
                profile="inventory-readonly",
                region="us-east-1",
                resource_id="vpc-123",
                full_inventory=True,
            )

    def test_sensitive_resource_types_are_rejected(self):
        for resource_type in (
            "secret",
            "secrets",
            "asg",
            "aws_secretsmanager_secret_version",
            "aws_lambda_function",
            "aws_lambda_layer_version",
            "aws_instance",
            "aws_launch_configuration",
            "aws_launch_template",
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

    def test_mixed_scope_fails_if_any_type_is_not_approved_networking(self):
        with self.assertRaisesRegex(SafetyError, "blocked|not approved"):
            build_aws2tf_command(
                python_path="python",
                script_path="aws2tf.py",
                profile="network-readonly",
                region="us-east-1",
                resource_type="aws_vpc,aws_lambda_layer_version",
            )

    def test_approved_networking_aliases_and_direct_types_are_accepted(self):
        for resource_type in (
            "vpc",
            "net",
            "tgw",
            "ram",
            "nfw",
            "route53",
            "aws_route_table",
            "aws_ec2_transit_gateway",
            "aws_ec2_transit_gateway_route_table",
            "aws_networkfirewall_firewall",
            "aws_ram_resource_share",
        ):
            with self.subTest(resource_type=resource_type):
                command = build_aws2tf_command(
                    python_path="python",
                    script_path="aws2tf.py",
                    profile="network-readonly",
                    region="us-east-1",
                    resource_type=resource_type,
                )
                self.assertIn(resource_type, command)

    def test_invalid_profile_and_region_are_rejected(self):
        with self.assertRaisesRegex(SafetyError, "Profile"):
            build_aws2tf_command(
                python_path="python",
                script_path="aws2tf.py",
                profile="bad;profile",
                region="us-east-1",
                resource_type="aws_vpc",
            )
        with self.assertRaisesRegex(SafetyError, "region"):
            build_aws2tf_command(
                python_path="python",
                script_path="aws2tf.py",
                profile="network-readonly",
                region="not-a-region",
                resource_type="aws_vpc",
            )

    @patch("company_safe.subprocess.run")
    def test_get_actual_account_uses_argument_list(self, run):
        run.return_value = Mock(stdout='{"Account":"111122223333"}')

        account = get_actual_account("network-readonly")

        self.assertEqual(account, "111122223333")
        self.assertEqual(
            run.call_args.args[0],
            [
                "aws",
                "sts",
                "get-caller-identity",
                "--profile",
                "network-readonly",
                "--output",
                "json",
            ],
        )

    @patch("company_safe.write_run_manifest")
    @patch("company_safe.get_git_commit", side_effect=["fork123", "upstream123"])
    @patch(
        "company_safe.get_worktree_provenance",
        return_value={"worktree_dirty": False, "worktree_fingerprint_sha256": "abc"},
    )
    @patch("company_safe.get_actual_account", return_value="111122223333")
    def test_dry_run_verifies_identity_without_running_upstream(
        self, _account, _provenance, _commits, write_manifest
    ):
        with patch("company_safe.subprocess.run") as run:
            exit_code = main(
                [
                    "--expected-account",
                    "111122223333",
                    "--profile",
                    "network-readonly",
                    "--region",
                    "us-east-1",
                    "--type",
                    "aws_vpc",
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 0)
        write_manifest.assert_called_once()
        manifest_payload = write_manifest.call_args.args[1]
        self.assertEqual(manifest_payload["fork_commit"], "fork123")
        self.assertEqual(manifest_payload["upstream_base_commit"], "upstream123")
        run.assert_not_called()

    @patch("company_safe.write_run_manifest")
    @patch("company_safe.get_git_commit", side_effect=["fork123", "upstream123"])
    @patch(
        "company_safe.get_worktree_provenance",
        return_value={"worktree_dirty": False, "worktree_fingerprint_sha256": "abc"},
    )
    @patch("company_safe.get_actual_account", return_value="111122223333")
    def test_upstream_exit_code_is_propagated(
        self, _account, _provenance, _commits, _write_manifest
    ):
        with patch("company_safe.subprocess.run", return_value=Mock(returncode=7)) as run:
            exit_code = main(
                [
                    "--expected-account",
                    "111122223333",
                    "--profile",
                    "network-readonly",
                    "--region",
                    "us-east-1",
                    "--type",
                    "aws_vpc",
                ]
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("--validate", run.call_args.args[0])
        self.assertNotIn("--accept", run.call_args.args[0])

    @patch("company_safe.write_run_manifest")
    @patch("company_safe.get_git_commit", side_effect=["fork123", "upstream123"])
    @patch(
        "company_safe.get_worktree_provenance",
        return_value={"worktree_dirty": True, "worktree_fingerprint_sha256": "abc"},
    )
    @patch("company_safe.get_actual_account", return_value="111122223333")
    @patch("company_safe.os.umask", side_effect=[0o022, 0o077])
    def test_full_inventory_runs_with_metadata_only_environment(
        self, umask, _account, _provenance, _commits, write_manifest
    ):
        with patch("company_safe.subprocess.run", return_value=Mock(returncode=0)) as run:
            exit_code = main(
                [
                    "--expected-account",
                    "111122223333",
                    "--profile",
                    "inventory-readonly",
                    "--region",
                    "us-east-1",
                    "--full-inventory",
                ]
            )

        self.assertEqual(exit_code, 0)
        command = run.call_args.args[0]
        self.assertNotIn("--type", command)
        self.assertEqual(run.call_args.kwargs["env"]["AWS2TF_METADATA_ONLY"], "1")
        self.assertEqual([call.args[0] for call in umask.call_args_list], [0o077, 0o022])
        payload = write_manifest.call_args.args[1]
        self.assertEqual(payload["scope"], "full-inventory")
        self.assertEqual(payload["payload_suppression"], "best-effort")
        self.assertTrue(payload["selected_handler_payload_suppression"])
        self.assertTrue(payload["sensitive_provider_reads_possible"])
        self.assertTrue(payload["worktree_dirty"])

    @patch("company_safe.get_actual_account", return_value="999900001111")
    def test_main_stops_before_generation_on_account_mismatch(self, _account):
        exit_code = main(
            [
                "--expected-account",
                "111122223333",
                "--profile",
                "network-readonly",
                "--region",
                "us-east-1",
                "--type",
                "aws_vpc",
            ]
        )

        self.assertEqual(exit_code, 2)

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

    @patch("company_safe.subprocess.run")
    def test_dirty_worktree_is_fingerprinted(self, run):
        run.side_effect = [
            Mock(stdout=" M company_safe.py\n"),
            Mock(stdout="diff --git a/company_safe.py b/company_safe.py\n"),
        ]

        provenance = get_worktree_provenance(Path("/repo"))

        self.assertTrue(provenance["worktree_dirty"])
        self.assertEqual(len(provenance["worktree_fingerprint_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
