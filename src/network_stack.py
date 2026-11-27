import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkStack(cdk.Stack):
    """
    The VPC the graph database (NeptuneStack) runs in, as in sagebrain-infra:

      - 2 AZs, each with a public and a private subnet (Neptune is in the private ones)
      - one NAT gateway, for what later runs in the private subnets and needs the internet
        (Neptune itself does not). One, not one per AZ, until something depends on it: raising
        nat_gateways is an in-place change, while switching the private subnets to isolated
        ones (no NAT) later would replace the subnets Neptune is in
      - an S3 gateway endpoint, which the Neptune bulk loader needs to read S3
      - the default security group allows no traffic
    """

    def __init__(self, scope: Construct, construct_id: str, vpc_cidr: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=2,
            nat_gateways=1,
            # sagebrain-infra gets this from cdk.json's @aws-cdk/aws-ec2:restrictDefaultSecurityGroup
            restrict_default_security_group=True,
        )

        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
