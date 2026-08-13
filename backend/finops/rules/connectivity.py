"""Connectivity and key management charges that outlive whatever needed them.

The pattern is the same in every rule here: something was created to connect one network
to another, or to protect data that no longer exists, and the hourly or monthly charge
carried on after the reason for it went away. None of these resources appear as "running",
so nothing in the console draws attention to them.
"""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import (
    ACTION_DELETE,
    ACTION_RELEASE,
    Evidence,
    Finding,
    Remediation,
    Resource,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

BYTES_PER_MB = 1024**2

# A private CA sits at $400 a month, so it is worth flagging even while disabled, which is
# a state people leave them in believing the charge stops.
_DISABLED_CA_STATES = {"DISABLED", "PENDING_CERTIFICATE"}


def _mb(value: float) -> str:
    return f"{value / BYTES_PER_MB:,.2f} MB"


def _too_new(ctx: RuleContext, resource: Resource) -> bool:
    """Recently built connectivity may simply not be carrying traffic yet."""
    age = ctx.age_days(resource)
    return age is not None and age < ctx.thresholds.network_unused_min_age_days


@register
class IdleVpcEndpoint(Rule):
    """An interface endpoint bills per availability zone whether anything calls it."""

    id = "vpce.idle"
    category = "network"
    title = "Idle VPC interface endpoint"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for endpoint in ctx.of_type("ec2:vpc-endpoint"):
            attributes = endpoint.attributes
            # Gateway endpoints for S3 and DynamoDB are free, so an idle one costs nothing.
            if not attributes.get("billable"):
                continue
            if endpoint.state != "available" or _too_new(ctx, endpoint):
                continue

            bytes_per_day = endpoint.metrics.get("endpoint_bytes_per_day")
            if bytes_per_day is None:
                continue
            if bytes_per_day >= ctx.thresholds.endpoint_idle_bytes_per_day:
                continue

            savings = ctx.monthly_cost(endpoint)
            if savings <= 0:
                continue

            interfaces = attributes.get("network_interface_count") or 1
            service_name = attributes.get("service_name") or "unknown service"
            yield finding_for(
                endpoint,
                rule_id=self.id,
                title=(
                    f"VPC endpoint for {_short_service(service_name)} processes almost no traffic"
                ),
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"This endpoint moved {_mb(bytes_per_day)} a day across "
                    f"{interfaces} network interface(s). The hourly charge applies per "
                    "interface, one per subnet the endpoint is placed in, and is the same "
                    "whether traffic flows or not. Deleting it sends the traffic back over "
                    "whatever route existed before, which for AWS APIs usually means a NAT "
                    "Gateway, so check that path still exists before removing it."
                ),
                evidence=[
                    Evidence(label="Service", value=service_name),
                    Evidence(label="Bytes processed per day", value=_mb(bytes_per_day)),
                    Evidence(
                        label="Active connections",
                        value=(
                            f"{endpoint.metrics['endpoint_active_connections']:,.0f}"
                            if endpoint.metrics.get("endpoint_active_connections") is not None
                            else "no data"
                        ),
                    ),
                    Evidence(label="Network interfaces", value=str(interfaces)),
                    Evidence(label="VPC", value=str(attributes.get("vpc_id"))),
                    Evidence(label="Cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Confirm nothing resolves the endpoint's private DNS name, then delete "
                        "it. Traffic falls back to the public endpoint over the subnet's "
                        "existing route."
                    ),
                    cli=(
                        "aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "
                        f"{endpoint.resource_id} --region {endpoint.region}"
                    ),
                    terraform="# Remove the aws_vpc_endpoint resource from your configuration.",
                    console_path="VPC > Endpoints",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
            )


@register
class IdleTransitGatewayAttachment(Rule):
    """An attachment carrying nothing still costs its hourly rate."""

    id = "tgw.idle_attachment"
    category = "network"
    title = "Idle transit gateway attachment"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for attachment in ctx.of_type("ec2:transit-gateway-attachment"):
            attributes = attachment.attributes
            # The account that created the attachment is the one billed for it.
            if not attributes.get("owned_by_this_account"):
                continue
            if attachment.state != "available" or _too_new(ctx, attachment):
                continue

            bytes_per_day = attachment.metrics.get("tgw_bytes_per_day")
            if bytes_per_day is None:
                continue
            if bytes_per_day >= ctx.thresholds.tgw_attachment_idle_bytes_per_day:
                continue

            savings = ctx.monthly_cost(attachment)
            if savings <= 0:
                continue

            attached_to = attributes.get("resource_id") or "unknown"
            yield finding_for(
                attachment,
                rule_id=self.id,
                title=(f"Transit gateway attachment for {attached_to} carries almost no traffic"),
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"The attachment moved {_mb(bytes_per_day)} a day in both directions "
                    "combined. Attachments are charged by the hour on top of any data they "
                    "process, and the charge lands on the account that created the "
                    "attachment rather than the account that owns the gateway. If the VPC "
                    "behind it no longer needs to reach the rest of the network, deleting "
                    "the attachment removes the whole charge."
                ),
                evidence=[
                    Evidence(label="Attached resource", value=str(attached_to)),
                    Evidence(label="Attachment kind", value=str(attributes.get("attachment_kind"))),
                    Evidence(
                        label="Transit gateway", value=str(attributes.get("transit_gateway_id"))
                    ),
                    Evidence(label="Bytes per day", value=_mb(bytes_per_day)),
                    Evidence(label="Cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Check the route tables that point at this attachment, then delete it. "
                        "Routes referencing it become blackholes until they are removed too."
                    ),
                    cli=(
                        "aws ec2 delete-transit-gateway-vpc-attachment "
                        f"--transit-gateway-attachment-id {attachment.resource_id} "
                        f"--region {attachment.region}"
                    ),
                    console_path="VPC > Transit gateway attachments",
                ),
                confidence="medium",
                effort="medium",
                risk="high",
            )


@register
class UnusedVpnConnection(Rule):
    """A VPN connection with dead tunnels or no traffic, still billed by the hour."""

    id = "vpn.unused"
    category = "network"
    title = "Unused site-to-site VPN connection"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for connection in ctx.of_type("ec2:vpn-connection"):
            if connection.state != "available":
                continue
            attributes = connection.attributes
            tunnels_up = attributes.get("tunnels_up")
            bytes_per_day = connection.metrics.get("vpn_bytes_per_day")

            tunnels_down = tunnels_up == 0
            no_traffic = (
                bytes_per_day is not None and bytes_per_day < ctx.thresholds.vpn_idle_bytes_per_day
            )
            if not tunnels_down and not no_traffic:
                continue
            if not tunnels_down and _too_new(ctx, connection):
                continue

            savings = ctx.monthly_cost(connection)
            if savings <= 0:
                continue

            reason = (
                "every tunnel is down, so nothing can be crossing it"
                if tunnels_down
                else f"it moved {_mb(bytes_per_day or 0)} a day"
            )
            yield finding_for(
                connection,
                rule_id=self.id,
                title=f"VPN connection {connection.display_name} appears unused",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"This connection is billed for every hour it exists, and {reason}. A "
                    "down tunnel can also mean a live connection whose far end is "
                    "misconfigured, so confirm with whoever owns the customer gateway before "
                    "deleting anything."
                ),
                evidence=[
                    Evidence(
                        label="Tunnels up",
                        value=f"{tunnels_up} of {attributes.get('tunnel_count', 0)}",
                    ),
                    Evidence(
                        label="Bytes per day",
                        value=_mb(bytes_per_day) if bytes_per_day is not None else "no data",
                    ),
                    Evidence(
                        label="Customer gateway", value=str(attributes.get("customer_gateway_id"))
                    ),
                    Evidence(label="Cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Confirm the remote site no longer needs the tunnel, then delete the "
                        "connection. The customer gateway and virtual private gateway are free "
                        "to leave in place."
                    ),
                    cli=(
                        "aws ec2 delete-vpn-connection --vpn-connection-id "
                        f"{connection.resource_id} --region {connection.region}"
                    ),
                    console_path="VPC > Site-to-Site VPN connections",
                ),
                confidence="medium" if tunnels_down else "low",
                effort="low",
                risk="high",
            )


@register
class UnusedClientVpnEndpoint(Rule):
    """Client VPN charges per associated subnet, before anyone dials in."""

    id = "clientvpn.unused"
    category = "network"
    title = "Client VPN endpoint with no connections"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for endpoint in ctx.of_type("ec2:client-vpn-endpoint"):
            subnets = endpoint.attributes.get("associated_subnet_count") or 0
            if not subnets or _too_new(ctx, endpoint):
                continue
            savings = ctx.monthly_cost(endpoint)
            if savings <= 0:
                continue

            yield finding_for(
                endpoint,
                rule_id=self.id,
                title=(
                    f"Client VPN endpoint {endpoint.display_name} holds "
                    f"{subnets} subnet association(s)"
                ),
                category=self.category,
                action=ACTION_RELEASE,
                savings=savings,
                detail=(
                    "Client VPN bills an hourly charge for each associated subnet, on top of "
                    "a separate charge per connected client. The subnet charge continues "
                    "whether anyone connects or not, so an endpoint kept for occasional "
                    "access costs the same as one in daily use. Disassociating the subnets "
                    "stops the standing charge while leaving the endpoint and its "
                    "certificates in place."
                ),
                evidence=[
                    Evidence(label="Associated subnets", value=str(subnets)),
                    Evidence(label="VPC", value=str(endpoint.attributes.get("vpc_id"))),
                    Evidence(label="Standing cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "If access is only needed occasionally, disassociate the target "
                        "networks and reassociate them when required. Delete the endpoint if "
                        "nobody uses it at all."
                    ),
                    cli=(
                        "aws ec2 describe-client-vpn-target-networks --client-vpn-endpoint-id "
                        f"{endpoint.resource_id} --region {endpoint.region}"
                    ),
                    console_path="VPC > Client VPN endpoints",
                ),
                confidence="low",
                effort="low",
                risk="high",
            )


@register
class BillingPrivateCertificateAuthority(Rule):
    """A private CA bills $400 a month, and disabling it changes nothing."""

    id = "acm.private_ca_billing"
    category = "governance"
    title = "Private certificate authority still billing"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for authority in ctx.of_type("acm-pca:certificate-authority"):
            if authority.state not in _DISABLED_CA_STATES:
                continue
            savings = ctx.monthly_cost(authority)
            if savings <= 0:
                continue

            issued = sum(
                1
                for certificate in ctx.of_type("acm:certificate")
                if certificate.attributes.get("certificate_authority_arn") == authority.arn
            )
            yield finding_for(
                authority,
                rule_id=self.id,
                title=(
                    f"Private CA {authority.display_name} is {authority.state.lower()} "
                    "but still charged"
                ),
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    "A private certificate authority is charged monthly from creation until "
                    "deletion. Disabling it stops it issuing certificates but not the charge. "
                    f"This one currently backs {issued} certificate(s) visible in ACM. If it "
                    "is genuinely finished with, delete it; the deletion is staged with a "
                    "restore window, so it can be recovered if that turns out to be wrong."
                ),
                evidence=[
                    Evidence(label="Status", value=str(authority.state)),
                    Evidence(label="Usage mode", value=str(authority.attributes.get("usage_mode"))),
                    Evidence(label="Certificates issued from it", value=str(issued)),
                    Evidence(label="Cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Delete the authority with a restore period long enough to notice a "
                        "mistake. Certificates it already issued keep working until they expire."
                    ),
                    cli=(
                        "aws acm-pca delete-certificate-authority --certificate-authority-arn "
                        f"{authority.arn} --permanent-deletion-time-in-days 30 "
                        f"--region {authority.region}"
                    ),
                    console_path="AWS Private CA > Certificate authorities",
                ),
                confidence="high",
                effort="low",
                risk="high",
                rollback_possible=True,
            )


@register
class KmsKeyPendingDeletion(Rule):
    """Keys awaiting deletion keep billing for the whole waiting period."""

    id = "kms.pending_deletion"
    category = "governance"
    title = "KMS keys awaiting deletion"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for key in ctx.of_type("kms:key"):
            if key.state != "PendingDeletion":
                continue
            savings = ctx.monthly_cost(key)
            if savings <= 0:
                continue

            deletion_date = key.attributes.get("deletion_date")
            yield finding_for(
                key,
                rule_id=self.id,
                title=f"KMS key {key.display_name} is scheduled for deletion and still billed",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    "A customer managed key is charged monthly until deletion actually "
                    "happens, and the waiting period can be as long as 30 days. Nothing needs "
                    "doing here beyond letting the schedule run out; the finding exists so the "
                    "charge is accounted for rather than mistaken for an active key."
                ),
                evidence=[
                    Evidence(label="Key state", value=str(key.state)),
                    Evidence(label="Deletion date", value=str(deletion_date or "unknown")),
                    Evidence(
                        label="Aliases",
                        value=", ".join(key.attributes.get("aliases") or []) or "none",
                    ),
                    Evidence(label="Cost until then", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "No action needed unless the deletion was a mistake, in which case "
                        "cancel it before the scheduled date."
                    ),
                    cli=(f"aws kms describe-key --key-id {key.resource_id} --region {key.region}"),
                    console_path="KMS > Customer managed keys",
                ),
                confidence="high",
                effort="low",
                risk="low",
            )


def _short_service(service_name: str) -> str:
    """``com.amazonaws.us-west-2.secretsmanager`` reads better as ``secretsmanager``."""
    return service_name.rsplit(".", 1)[-1] if service_name else service_name
