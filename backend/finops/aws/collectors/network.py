"""Networking resources that bill even when nothing uses them: Elastic IPs and NAT Gateways."""

from __future__ import annotations

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    synthesize_arn,
    tags_to_dict,
)
from finops.model import Resource


@register
class ElasticIpCollector(Collector):
    key = "eip"
    service = "VPC"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        # describe_addresses is not paginated.
        addresses = client.describe_addresses().get("Addresses", [])
        resources: list[Resource] = []
        for address in addresses:
            tags = tags_to_dict(address.get("Tags"))
            resource_id = address.get("AllocationId") or address.get("PublicIp", "unknown")
            associated = bool(address.get("AssociationId"))
            resources.append(
                Resource(
                    arn=synthesize_arn("ec2", region, ctx.account_id, f"elastic-ip/{resource_id}"),
                    resource_id=resource_id,
                    resource_type="ec2:elastic-ip",
                    service="VPC",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name"),
                    state="associated" if associated else "unassociated",
                    tags=tags,
                    attributes={
                        "public_ip": address.get("PublicIp"),
                        "domain": address.get("Domain"),
                        "associated": associated,
                        "instance_id": address.get("InstanceId") or None,
                        "network_interface_id": address.get("NetworkInterfaceId") or None,
                        "public_ipv4_pool": address.get("PublicIpv4Pool"),
                    },
                )
            )
        return resources


@register
class NatGatewayCollector(Collector):
    key = "natgw"
    service = "VPC"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        for gateway in paginate(client, "describe_nat_gateways", "NatGateways"):
            tags = tags_to_dict(gateway.get("Tags"))
            gateway_id = gateway["NatGatewayId"]
            addresses = gateway.get("NatGatewayAddresses", [])
            subnet_id = gateway.get("SubnetId")
            availability_zone = None
            for address in addresses:
                # NAT Gateway responses do not carry the AZ directly; the subnet does.
                availability_zone = address.get("AvailabilityZone") or availability_zone
            resources.append(
                Resource(
                    arn=synthesize_arn("ec2", region, ctx.account_id, f"natgateway/{gateway_id}"),
                    resource_id=gateway_id,
                    resource_type="ec2:nat-gateway",
                    service="VPC",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name"),
                    availability_zone=availability_zone,
                    state=gateway.get("State"),
                    created_at=gateway.get("CreateTime"),
                    tags=tags,
                    attributes={
                        "vpc_id": gateway.get("VpcId"),
                        "subnet_id": subnet_id,
                        "connectivity_type": gateway.get("ConnectivityType", "public"),
                        "private_ips": [
                            a.get("PrivateIp") for a in addresses if a.get("PrivateIp")
                        ],
                        "public_ips": [a.get("PublicIp") for a in addresses if a.get("PublicIp")],
                        "failure_message": gateway.get("FailureMessage"),
                    },
                )
            )
        return resources
