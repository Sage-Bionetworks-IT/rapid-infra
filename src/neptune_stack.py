import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_neptune as neptune
from aws_cdk import aws_s3 as s3
from constructs import Construct


class NeptuneStack(cdk.Stack):
    """
    A Neptune Serverless graph database in the VPC's private subnets, as in sagebrain-infra
    (same construct IDs and output names, so its runbooks carry over):

      - the cluster: IAM authentication (SigV4-signed requests only), encrypted storage, and
        audit and slow-query logs to CloudWatch, which its cluster parameter group turns on
        (NEPTUNE.cluster_parameters). A final snapshot is kept if the cluster is deleted
      - NEPTUNE.instance_count db.serverless instances, spread over the AZs: the first is the
        writer, the others readers
      - a security group with no ingress: what queries or loads the graph adds its own rule,
        on the cluster's port (NeptuneClusterPort output) from its own security group
      - a data bucket, and the role the bulk loader reads it with (associated with the cluster;
        pass NeptuneLoadRoleArn as iamRoleArn to the cluster's /loader API)
      - data_store_tags (config DATA_STORE_TAGS) on the bucket, the cluster and the instances,
        e.g. Vanta's data classification
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc,
        neptune_config: dict,
        data_store_tags: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        def tag_data_store(construct: Construct) -> None:
            for key, value in (data_store_tags or {}).items():
                cdk.Tags.of(construct).add(key, value)

        # -------------------
        # Security group: no ingress here. The bulk loader reads S3 over HTTPS (through the
        # VPC's S3 gateway endpoint).
        # -------------------
        self.neptune_security_group = ec2.SecurityGroup(
            self,
            "NeptuneSecurityGroup",
            vpc=vpc,
            description="Security group for Neptune cluster",
            allow_all_outbound=False,
        )
        self.neptune_security_group.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS outbound for Neptune bulk loader (S3 access)",
        )

        # -------------------
        # Bulk load data, and the role Neptune reads it with
        # -------------------
        self.data_bucket = s3.Bucket(
            self,
            "NeptuneDataBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        tag_data_store(self.data_bucket)

        self.neptune_load_role = iam.Role(
            self,
            "NeptuneLoadRole",
            assumed_by=iam.ServicePrincipal("rds.amazonaws.com"),
            description="Allows Neptune bulk loader to read data from S3",
        )
        self.data_bucket.grant_read(self.neptune_load_role)

        # -------------------
        # Cluster
        # -------------------
        subnet_group = neptune.CfnDBSubnetGroup(
            self,
            "NeptuneSubnetGroup",
            db_subnet_group_description="Subnet group for Neptune cluster",
            subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets],
        )

        engine_version = neptune_config["engine_version"]
        major, minor = engine_version.split(".")[:2]
        # A cluster parameter group, not sagebrain-infra's default one: the audit and slow-query
        # logs are only written with their parameters on (the default group has them off)
        parameter_group = neptune.CfnDBClusterParameterGroup(
            self,
            "NeptuneClusterParameterGroup",
            family=f"neptune{major}.{minor}",
            description=f"Cluster parameters for {construct_id}",
            parameters={
                key: str(value)
                for key, value in (neptune_config.get("cluster_parameters") or {}).items()
            },
        )

        self.neptune_cluster = neptune.CfnDBCluster(
            self,
            "NeptuneCluster",
            engine_version=engine_version,
            db_subnet_group_name=subnet_group.ref,
            vpc_security_group_ids=[self.neptune_security_group.security_group_id],
            db_cluster_parameter_group_name=parameter_group.ref,
            serverless_scaling_configuration=(
                neptune.CfnDBCluster.ServerlessScalingConfigurationProperty(
                    min_capacity=neptune_config["serverless_min_capacity"],
                    max_capacity=neptune_config["serverless_max_capacity"],
                )
            ),
            iam_auth_enabled=True,
            storage_encrypted=True,
            enable_cloudwatch_logs_exports=["audit", "slowquery"],
            backup_retention_period=neptune_config["backup_retention_days"],
            preferred_backup_window=neptune_config["preferred_backup_window"],
            preferred_maintenance_window=neptune_config["preferred_maintenance_window"],
            deletion_protection=neptune_config["deletion_protection"],
            copy_tags_to_snapshot=True,
            associated_roles=[
                neptune.CfnDBCluster.DBClusterRoleProperty(role_arn=self.neptune_load_role.role_arn)
            ],
            tags=[cdk.CfnTag(key="Name", value=f"{construct_id}-cluster")],
        )
        # Deleting the cluster (or the stack) keeps a final snapshot
        self.neptune_cluster.apply_removal_policy(cdk.RemovalPolicy.SNAPSHOT)
        tag_data_store(self.neptune_cluster)

        self.neptune_instances = []
        for i in range(neptune_config["instance_count"]):
            instance = neptune.CfnDBInstance(
                self,
                f"NeptuneInstance{i + 1}",
                db_instance_class="db.serverless",
                db_cluster_identifier=self.neptune_cluster.ref,
                availability_zone=vpc.availability_zones[i % len(vpc.availability_zones)],
                auto_minor_version_upgrade=True,
                tags=[cdk.CfnTag(key="Name", value=f"{construct_id}-instance-{i + 1}")],
            )
            tag_data_store(instance)
            self.neptune_instances.append(instance)

        # -------------------
        # Outputs (no export names: these are for people and tools, not for other stacks). No
        # output may be named BucketName: tools/setup_synapse_projects.py takes a stack with one
        # for a provider bucket's
        # -------------------
        outputs = {
            "NeptuneClusterEndpoint": (
                self.neptune_cluster.attr_endpoint,
                "Neptune cluster (writer) endpoint",
            ),
            "NeptuneClusterReadEndpoint": (
                self.neptune_cluster.attr_read_endpoint,
                "Neptune cluster read endpoint",
            ),
            "NeptuneClusterPort": (self.neptune_cluster.attr_port, "Neptune cluster port"),
            "NeptuneClusterResourceId": (
                self.neptune_cluster.attr_cluster_resource_id,
                "For neptune-db IAM policies: arn:aws:neptune-db:<region>:<account>:<id>/*",
            ),
            "NeptuneSecurityGroupId": (
                self.neptune_security_group.security_group_id,
                "Neptune security group ID",
            ),
            "NeptuneDataBucketName": (
                self.data_bucket.bucket_name,
                "S3 bucket for Neptune bulk data loading",
            ),
            "NeptuneLoadRoleArn": (
                self.neptune_load_role.role_arn,
                "IAM role ARN for Neptune bulk loader (pass to /loader API)",
            ),
        }
        for output_id, (value, description) in outputs.items():
            cdk.CfnOutput(self, output_id, value=value, description=description)
