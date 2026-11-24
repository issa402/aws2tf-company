resource "aws_vpc" "fixture" {
  cidr_block = "10.0.0.0/24"
}

resource "aws_route_table" "fixture" {
  vpc_id = aws_vpc.fixture.id

  route = [{
    carrier_gateway_id         = null
    cidr_block                 = "0.0.0.0/0"
    core_network_arn           = null
    destination_prefix_list_id = null
    egress_only_gateway_id     = null
    gateway_id                 = null
    ipv6_cidr_block            = null
    local_gateway_id           = null
    nat_gateway_id             = null
    network_interface_id       = null
    odb_network_arn            = null
    transit_gateway_id         = "tgw-0123456789abcdef0"
    vpc_endpoint_id            = null
    vpc_peering_connection_id  = null
  }]
}

resource "aws_subnet" "fixture" {
  vpc_id            = aws_vpc.fixture.id
  cidr_block        = "10.0.0.0/28"
  availability_zone = "us-east-1a"
}

resource "aws_ssm_parameter" "metadata_only" {
  name  = "/aws2tf/metadata-only-fixture"
  type  = "SecureString"
  value = ""

  lifecycle {
    ignore_changes = [value]
  }
}
