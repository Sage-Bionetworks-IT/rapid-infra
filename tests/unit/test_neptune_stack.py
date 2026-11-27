import json

import pytest
from aws_cdk.assertions import Match, Template

from src.neptune_stack import NeptuneStack
from src.network_stack import NetworkStack

VPC_CIDR = "10.254.176.0/24"
NEPTUNE = {
    "enabled": True,
    "engine_version": "1.3.2.1",
    "serverless_min_capacity": 1.0,
    "serverless_max_capacity": 4.0,
    "instance_count": 2,
    "backup_retention_days": 7,
    "preferred_backup_window": "03:00-04:00",
    "preferred_maintenance_window": "sun:04:00-sun:05:00",
    "deletion_protection": True,
    "cluster_parameters": {
        "neptune_enable_audit_log": 1,
        "neptune_enable_slow_query_log": "info",
        "neptune_slow_query_log_threshold": 5000,
    },
}
TAG = {"Key": "DataClass", "Value": "internal"}


@pytest.fixture
def network(app):
    return NetworkStack(app, "test-network", vpc_cidr=VPC_CIDR)


@pytest.fixture
def network_template(network):
    return Template.from_stack(network)


@pytest.fixture
def template(app, network):
    stack = NeptuneStack(
        app,
        "test-neptune",
        vpc=network.vpc,
        neptune_config=NEPTUNE,
        data_store_tags={TAG["Key"]: TAG["Value"]},
    )
    return Template.from_stack(stack)


def _only(template, resource_type):
    (resource,) = template.find_resources(resource_type).values()
    return resource


# --------------------------------------------------------------------------- network


def test_vpc_uses_the_configured_cidr(network_template):
    network_template.has_resource_properties("AWS::EC2::VPC", {"CidrBlock": VPC_CIDR})


def test_vpc_has_two_azs_and_one_nat_gateway(network_template):
    # A public and a private subnet per AZ
    network_template.resource_count_is("AWS::EC2::Subnet", 4)
    network_template.resource_count_is("AWS::EC2::NatGateway", 1)


def test_vpc_has_an_s3_gateway_endpoint(network_template):
    # The Neptune bulk loader reads S3 through it
    endpoint = _only(network_template, "AWS::EC2::VPCEndpoint")["Properties"]
    assert endpoint["VpcEndpointType"] == "Gateway"
    assert ".s3" in json.dumps(endpoint["ServiceName"])


def test_vpc_default_security_group_is_restricted(network_template):
    network_template.resource_count_is("Custom::VpcRestrictDefaultSG", 1)


# --------------------------------------------------------------------------- cluster


def test_cluster_is_serverless_iam_authenticated_and_encrypted(template):
    template.has_resource_properties(
        "AWS::Neptune::DBCluster",
        {
            "EngineVersion": "1.3.2.1",
            "ServerlessScalingConfiguration": {"MinCapacity": 1, "MaxCapacity": 4},
            "IamAuthEnabled": True,
            "StorageEncrypted": True,
            "DeletionProtection": True,
            "BackupRetentionPeriod": 7,
            "CopyTagsToSnapshot": True,
            "EnableCloudwatchLogsExports": ["audit", "slowquery"],
        },
    )


def test_cluster_keeps_a_final_snapshot(template):
    cluster = _only(template, "AWS::Neptune::DBCluster")
    assert cluster["DeletionPolicy"] == "Snapshot"
    assert cluster["UpdateReplacePolicy"] == "Snapshot"


def test_cluster_parameter_group_turns_on_the_exported_logs(template):
    # The default parameter group has audit and slow-query logging off, so the log exports
    # would stay empty
    template.has_resource_properties(
        "AWS::Neptune::DBClusterParameterGroup",
        {
            "Family": "neptune1.3",
            "Parameters": {
                "neptune_enable_audit_log": "1",
                "neptune_enable_slow_query_log": "info",
                "neptune_slow_query_log_threshold": "5000",
            },
        },
    )
    (group_id,) = template.find_resources("AWS::Neptune::DBClusterParameterGroup")
    template.has_resource_properties(
        "AWS::Neptune::DBCluster", {"DBClusterParameterGroupName": {"Ref": group_id}}
    )


def test_instances_are_serverless_in_the_cluster(template):
    template.resource_count_is("AWS::Neptune::DBInstance", 2)
    (cluster_id,) = template.find_resources("AWS::Neptune::DBCluster")
    template.all_resources_properties(
        "AWS::Neptune::DBInstance",
        {"DBInstanceClass": "db.serverless", "DBClusterIdentifier": {"Ref": cluster_id}},
    )


def test_cluster_is_in_the_private_subnets(template):
    subnet_ids = _only(template, "AWS::Neptune::DBSubnetGroup")["Properties"]["SubnetIds"]
    assert len(subnet_ids) == 2
    assert all("PrivateSubnet" in json.dumps(s) for s in subnet_ids)


def test_security_group_allows_no_ingress_and_only_https_out(template):
    group = _only(template, "AWS::EC2::SecurityGroup")["Properties"]
    assert "SecurityGroupIngress" not in group
    (rule,) = group["SecurityGroupEgress"]
    assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 443, 443)
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 0)


# --------------------------------------------------------------------------- bulk load


def test_data_bucket_is_private_versioned_and_retained(template):
    template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                }
            ),
        },
    )


def test_load_role_is_neptunes_and_associated_with_the_cluster(template):
    (role_id,) = template.find_resources("AWS::IAM::Role")
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {"Action": "sts:AssumeRole", "Principal": {"Service": "rds.amazonaws.com"}}
                    )
                ]
            }
        },
    )
    template.has_resource_properties(
        "AWS::Neptune::DBCluster",
        {"AssociatedRoles": [{"RoleArn": {"Fn::GetAtt": [role_id, "Arn"]}}]},
    )


def test_load_role_can_only_read_the_bucket(template):
    policy = _only(template, "AWS::IAM::Policy")["Properties"]["PolicyDocument"]
    actions = {a for s in policy["Statement"] for a in s["Action"]}
    assert actions and all(a.startswith(("s3:Get", "s3:List")) for a in actions)


# --------------------------------------------------------------------------- tags, outputs


@pytest.mark.parametrize(
    "resource_type", ["AWS::S3::Bucket", "AWS::Neptune::DBCluster", "AWS::Neptune::DBInstance"]
)
def test_data_store_tags_on_the_data_stores(template, resource_type):
    template.all_resources_properties(resource_type, {"Tags": Match.array_with([TAG])})


def test_data_store_tags_only_on_the_data_stores(template):
    data_stores = {"AWS::S3::Bucket", "AWS::Neptune::DBCluster", "AWS::Neptune::DBInstance"}
    for resource in template.to_json()["Resources"].values():
        if resource["Type"] not in data_stores:
            assert TAG not in resource.get("Properties", {}).get("Tags", [])


def test_outputs(template):
    assert set(template.find_outputs("*")) == {
        "NeptuneClusterEndpoint",
        "NeptuneClusterReadEndpoint",
        "NeptuneClusterPort",
        "NeptuneClusterResourceId",
        "NeptuneSecurityGroupId",
        "NeptuneDataBucketName",
        "NeptuneLoadRoleArn",
    }
    # tools/setup_synapse_projects.py takes any stack with a BucketName output for a provider
    # bucket's, and nothing reads these from another stack
    assert not any("Export" in output for output in template.find_outputs("*").values())
