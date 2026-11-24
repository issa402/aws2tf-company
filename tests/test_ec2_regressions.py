import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "code"))

from fixtf_aws_resources import fixtf_ec2


class Ec2GenerationRegressionTests(unittest.TestCase):
    def test_empty_vpc_ipv6_pair_is_removed_together(self):
        skip_pool, _, skip_ipv6, _ = fixtf_ec2.aws_vpc(
            "ipv6_ipam_pool_id = null\n",
            "ipv6_ipam_pool_id",
            "null",
            False,
            False,
        )
        skip_length, _, _, _ = fixtf_ec2.aws_vpc(
            "ipv6_netmask_length = 0\n",
            "ipv6_netmask_length",
            "0",
            skip_ipv6,
            False,
        )

        self.assertEqual((skip_pool, skip_length), (1, 1))

    def test_real_vpc_ipv6_ipam_pair_is_preserved(self):
        skip_pool, _, skip_ipv6, _ = fixtf_ec2.aws_vpc(
            'ipv6_ipam_pool_id = "ipam-pool-123"\n',
            "ipv6_ipam_pool_id",
            "ipam-pool-123",
            False,
            False,
        )
        skip_length, _, _, _ = fixtf_ec2.aws_vpc(
            "ipv6_netmask_length = 56\n",
            "ipv6_netmask_length",
            "56",
            skip_ipv6,
            False,
        )

        self.assertEqual((skip_pool, skip_length), (0, 0))

    def test_empty_route_object_members_become_null(self):
        for field in (
            "core_network_arn",
            "gateway_id",
            "network_interface_id",
            "transit_gateway_id",
            "vpc_endpoint_id",
        ):
            with self.subTest(field=field):
                _, rendered, _, _ = fixtf_ec2.aws_route_table(
                    f'{field} = ""\n', field, "", False, False
                )
                self.assertEqual(rendered, f"{field} = null\n")


if __name__ == "__main__":
    unittest.main()
