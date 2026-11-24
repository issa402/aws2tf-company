import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "code"))

import context
import cmd_runner
from fixtf_aws_resources import (
    fixtf_autoscaling,
    fixtf_ec2,
    fixtf_glue,
    fixtf_ssm,
)
from get_aws_resources import aws_ec2, aws_lambda, aws_secretsmanager


class MetadataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.previous = context.metadata_only
        self.previous_environment_credentials = context.environment_credentials
        self.previous_profile = context.profile
        self.previous_merge = context.merge
        context.metadata_only = True

    def tearDown(self):
        context.metadata_only = self.previous
        context.environment_credentials = self.previous_environment_credentials
        context.profile = self.previous_profile
        context.merge = self.previous_merge

    @patch("get_aws_resources.aws_lambda.boto3.client")
    @patch("get_aws_resources.aws_lambda.requests.get")
    def test_lambda_function_package_is_not_requested(self, request_get, client):
        self.assertTrue(aws_lambda.get_lambda_code("function-name"))
        client.assert_not_called()
        request_get.assert_not_called()

    @patch("get_aws_resources.aws_lambda.boto3.client")
    @patch("get_aws_resources.aws_lambda.requests.get")
    def test_lambda_layer_package_is_not_requested(self, request_get, client):
        self.assertTrue(
            aws_lambda.get_lambdalayer_code(
                "arn:aws:lambda:us-east-1:111122223333:layer:example:1"
            )
        )
        client.assert_not_called()
        request_get.assert_not_called()

    @patch("fixtf_aws_resources.fixtf_ec2.boto3.Session")
    def test_ec2_user_data_is_not_requested(self, session):
        skip, _, _, _ = fixtf_ec2.aws_instance(
            "user_data = null\n", "user_data", "null", False, "aws_instance__i-123"
        )
        self.assertEqual(skip, 1)
        session.assert_not_called()

    @patch("fixtf_aws_resources.fixtf_autoscaling.boto3.client")
    def test_launch_configuration_user_data_is_not_requested(self, client):
        skip, _, _, _ = fixtf_autoscaling.aws_launch_configuration(
            "user_data = null\n",
            "user_data",
            "null",
            False,
            "aws_launch_configuration__example",
        )
        self.assertEqual(skip, 1)
        client.assert_not_called()

    @patch("get_aws_resources.aws_secretsmanager.boto3.client")
    def test_secret_versions_are_not_enumerated_or_read(self, client):
        result = aws_secretsmanager.get_aws_secretsmanager_secret_version(
            "aws_secretsmanager_secret_version",
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:example",
            "secretsmanager",
            "list_secret_version_ids",
            "Versions",
            "VersionId",
            "",
        )
        self.assertTrue(result)
        client.assert_not_called()

    @patch("fixtf_aws_resources.fixtf_ssm.boto3.client")
    def test_ssm_parameter_value_is_not_decrypted(self, client):
        skip, rendered, _, _ = fixtf_ssm.aws_ssm_parameter(
            "value = null\n", "value", "null", False, False
        )
        self.assertEqual(skip, 0)
        self.assertIn('value = ""', rendered)
        self.assertIn("ignore_changes = [value]", rendered)
        client.assert_not_called()

    @patch("get_aws_resources.aws_ec2.boto3.Session")
    def test_instance_discovery_uses_environment_credential_chain(self, session):
        context.environment_credentials = True
        context.region = "us-east-1"
        session.return_value.client.return_value.get_paginator.return_value.paginate.return_value = []

        result = aws_ec2.get_aws_instance(
            "aws_instance",
            None,
            "ec2",
            "describe_instances",
            "Reservations",
            "InstanceId",
            "",
        )

        self.assertTrue(result)
        session.assert_called_once_with(region_name="us-east-1", profile_name=None)

    def test_active_profile_is_none_for_environment_credentials(self):
        context.environment_credentials = True
        context.profile = "default"

        self.assertIsNone(context.active_profile())

    def test_active_profile_preserves_named_profile(self):
        context.environment_credentials = False
        context.profile = "network-readonly"

        self.assertEqual(context.active_profile(), "network-readonly")

    @patch("cmd_runner.rc")
    def test_generated_provider_omits_conflicting_profile_in_environment_mode(self, rc):
        context.environment_credentials = True
        context.profile = "wrong-account"
        context.merge = True
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                Path("imported").mkdir()
                cmd_runner.aws_tf("us-east-1", Mock())
                provider = Path("provider.tf").read_text(encoding="utf-8")
            finally:
                os.chdir(original_cwd)

        self.assertNotIn("profile", provider)
        self.assertNotIn("shared_credentials_files", provider)
        rc.assert_called_once_with("cp provider.tf imported/provider.tf")

    @patch("fixtf_aws_resources.fixtf_glue.common.rc")
    def test_glue_script_is_not_downloaded(self, rc):
        fixtf_glue.aws_glue_job(
            'script_location = "s3://bucket/job.py"\n',
            "script_location",
            "s3://bucket/job.py",
            False,
            False,
        )
        rc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
