"""Networking resources that bill even when nothing uses them.

Everything here charges by the hour for merely existing: Elastic IPs, NAT Gateways,
transit gateway attachments, interface endpoints, and VPN connections. None of them show
up as "running" anywhere in the console, which is exactly why they are easy to leave
behind after the workload that needed them is gone.

Ownership matters in this file. A transit gateway shared into the account through Resource
Access Manager appears in ``describe_transit_gateways`` exactly like one the account owns,
but the hourly charge lands on the owner's bill. Attachments are the reverse: whoever
creates the attachment pays for it, even when the gateway belongs to someone else.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    synthesize_arn,
    tags_to_dict,
)
from finops.model import Resource

logger = logging.getLogger(__name__)

# describe_transit_gateway_attachments reports the resource kind in AWS's own spelling;
# the pricing lookups key attachment charges off these.
_ATTACHMENT_KINDS = {
    "vpc": "vpc",
    "vpn": "vpn",
    "direct-connect-gateway": "direct-connect-gateway",
    "peering": "peering",
    "connect": "connect",
    "tgw-peering": "peering",
}


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


@register
class TransitGatewayCollector(Collector):
    """Transit gateways and their attachments, which are billed to different accounts."""

    key = "tgw"
    service = "VPC"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []

        for gateway in paginate(client, "describe_transit_gateways", "TransitGateways"):
            gateway_id = gateway["TransitGatewayId"]
            owner = gateway.get("OwnerId")
            tags = tags_to_dict(gateway.get("Tags"))
            resources.append(
                Resource(
                    arn=gateway.get("TransitGatewayArn")
                    or synthesize_arn(
                        "ec2", region, owner or ctx.account_id, f"transit-gateway/{gateway_id}"
                    ),
                    resource_id=gateway_id,
                    resource_type="ec2:transit-gateway",
                    service="VPC",
                    region=region,
                    account_id=owner or ctx.account_id,
                    name=tags.get("Name") or gateway.get("Description") or None,
                    state=gateway.get("State"),
                    created_at=gateway.get("CreationTime"),
                    tags=tags,
                    attributes={
                        "owner_id": owner,
                        # A gateway shared in through RAM is billed to its owner, so the
                        # hourly charge must not be counted here.
                        "owned_by_this_account": owner == ctx.account_id,
                        "description": gateway.get("Description"),
                        "amazon_side_asn": (gateway.get("Options") or {}).get("AmazonSideAsn"),
                        "dns_support": (gateway.get("Options") or {}).get("DnsSupport"),
                        "multicast_support": (gateway.get("Options") or {}).get("MulticastSupport"),
                    },
                )
            )

        resources.extend(self._attachments(ctx, client, region))
        return resources

    def _attachments(self, ctx: CollectionContext, client, region: str) -> list[Resource]:
        """Each attachment is an hourly charge on whoever created it."""
        resources: list[Resource] = []
        for attachment in paginate(
            client, "describe_transit_gateway_attachments", "TransitGatewayAttachments"
        ):
            attachment_id = attachment["TransitGatewayAttachmentId"]
            tags = tags_to_dict(attachment.get("Tags"))
            resource_owner = attachment.get("ResourceOwnerId")
            kind = _ATTACHMENT_KINDS.get((attachment.get("ResourceType") or "").lower())
            resources.append(
                Resource(
                    arn=synthesize_arn(
                        "ec2",
                        region,
                        resource_owner or ctx.account_id,
                        f"transit-gateway-attachment/{attachment_id}",
                    ),
                    resource_id=attachment_id,
                    resource_type="ec2:transit-gateway-attachment",
                    service="VPC",
                    region=region,
                    account_id=resource_owner or ctx.account_id,
                    name=tags.get("Name"),
                    state=attachment.get("State"),
                    created_at=attachment.get("CreationTime"),
                    tags=tags,
                    attributes={
                        "transit_gateway_id": attachment.get("TransitGatewayId"),
                        "transit_gateway_owner_id": attachment.get("TransitGatewayOwnerId"),
                        "resource_owner_id": resource_owner,
                        "resource_id": attachment.get("ResourceId"),
                        "attachment_kind": kind,
                        "resource_type_reported": attachment.get("ResourceType"),
                        # The attachment is billed to the account that attached its
                        # resource, which is not always the gateway's owner.
                        "owned_by_this_account": resource_owner == ctx.account_id,
                    },
                )
            )
        return resources


@register
class VpcEndpointCollector(Collector):
    """Interface and Gateway Load Balancer endpoints bill per hour, per subnet."""

    key = "vpce"
    service = "VPC"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        for endpoint in paginate(client, "describe_vpc_endpoints", "VpcEndpoints"):
            endpoint_id = endpoint["VpcEndpointId"]
            tags = tags_to_dict(endpoint.get("Tags"))
            endpoint_type = endpoint.get("VpcEndpointType", "Interface")
            subnets = endpoint.get("SubnetIds") or []
            resources.append(
                Resource(
                    arn=synthesize_arn(
                        "ec2", region, ctx.account_id, f"vpc-endpoint/{endpoint_id}"
                    ),
                    resource_id=endpoint_id,
                    resource_type="ec2:vpc-endpoint",
                    service="VPC",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name") or endpoint.get("ServiceName"),
                    state=endpoint.get("State"),
                    created_at=endpoint.get("CreationTimestamp"),
                    tags=tags,
                    attributes={
                        "endpoint_type": endpoint_type,
                        "service_name": endpoint.get("ServiceName"),
                        "vpc_id": endpoint.get("VpcId"),
                        "subnet_ids": subnets,
                        # Charged per AZ, and an endpoint has one network interface per
                        # subnet it is placed in.
                        "network_interface_count": len(
                            endpoint.get("NetworkInterfaceIds") or subnets
                        ),
                        # Gateway endpoints for S3 and DynamoDB are free.
                        "billable": endpoint_type != "Gateway",
                        "private_dns_enabled": endpoint.get("PrivateDnsEnabled"),
                    },
                )
            )
        return resources


@register
class VpnConnectionCollector(Collector):
    """Site-to-site and client VPN, both charged by the hour regardless of traffic."""

    key = "vpn"
    service = "VPC"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []

        # describe_vpn_connections is not paginated.
        for connection in client.describe_vpn_connections().get("VpnConnections", []):
            connection_id = connection["VpnConnectionId"]
            tags = tags_to_dict(connection.get("Tags"))
            tunnels = connection.get("VgwTelemetry") or []
            resources.append(
                Resource(
                    arn=synthesize_arn(
                        "ec2", region, ctx.account_id, f"vpn-connection/{connection_id}"
                    ),
                    resource_id=connection_id,
                    resource_type="ec2:vpn-connection",
                    service="VPC",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name"),
                    state=connection.get("State"),
                    tags=tags,
                    attributes={
                        "customer_gateway_id": connection.get("CustomerGatewayId"),
                        "vpn_gateway_id": connection.get("VpnGatewayId"),
                        "transit_gateway_id": connection.get("TransitGatewayId"),
                        "category": connection.get("Category"),
                        "tunnel_count": len(tunnels),
                        "tunnels_up": sum(1 for t in tunnels if t.get("Status") == "UP"),
                        "tunnel_status": [t.get("Status") for t in tunnels],
                    },
                )
            )

        # Client VPN is a separate permission, and losing it should not cost us the
        # site-to-site connections we already have.
        try:
            resources.extend(self._client_vpn(ctx, client, region))
        except (ClientError, BotoCoreError, NotImplementedError) as exc:
            logger.debug("Client VPN unavailable in %s: %s", region, exc)
        return resources

    def _client_vpn(self, ctx: CollectionContext, client, region: str) -> list[Resource]:
        resources: list[Resource] = []
        for endpoint in paginate(client, "describe_client_vpn_endpoints", "ClientVpnEndpoints"):
            endpoint_id = endpoint["ClientVpnEndpointId"]
            tags = tags_to_dict(endpoint.get("Tags"))
            associations = self._associated_subnets(client, endpoint_id)
            resources.append(
                Resource(
                    arn=synthesize_arn(
                        "ec2", region, ctx.account_id, f"client-vpn-endpoint/{endpoint_id}"
                    ),
                    resource_id=endpoint_id,
                    resource_type="ec2:client-vpn-endpoint",
                    service="VPC",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name") or endpoint.get("Description") or None,
                    state=(endpoint.get("Status") or {}).get("Code"),
                    created_at=endpoint.get("CreationTime"),
                    tags=tags,
                    attributes={
                        "description": endpoint.get("Description"),
                        # The endpoint hour is charged per associated subnet, so an
                        # endpoint with no associations costs nothing yet.
                        "associated_subnet_count": associations,
                        "vpc_id": endpoint.get("VpcId"),
                        "split_tunnel": endpoint.get("SplitTunnel"),
                    },
                )
            )
        return resources

    def _associated_subnets(self, client, endpoint_id: str) -> int:
        associations: list[dict[str, Any]] = list(
            paginate(
                client,
                "describe_client_vpn_target_networks",
                "ClientVpnTargetNetworks",
                ClientVpnEndpointId=endpoint_id,
            )
        )
        return sum(1 for a in associations if (a.get("Status") or {}).get("Code") == "associated")
