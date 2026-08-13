"""KMS keys, Secrets Manager secrets, and ACM certificates.

Small per-item charges that only matter in bulk, and one that matters on its own:

* A customer managed KMS key is $1 a month, forever, and nothing in the console adds them
  up. Keys AWS manages on a service's behalf are free, and ``list_keys`` returns both, so
  each key has to be described to tell them apart.
* A secret is $0.40 a month whether anything reads it or not.
* Public certificates are free. A private certificate authority is $400 a month from the
  moment it is created until it is deleted, and disabling it changes nothing.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    tags_to_dict,
)
from finops.model import Resource

logger = logging.getLogger(__name__)


@register
class KmsKeyCollector(Collector):
    key = "kms"
    service = "KMS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("kms", region)
        key_ids = [item["KeyId"] for item in paginate(client, "list_keys", "Keys")]
        if not key_ids:
            return []

        aliases = self._aliases(client)
        workers = min(ctx.settings.max_workers, max(len(key_ids), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kms") as pool:
            found = list(
                pool.map(
                    lambda key_id: self._describe(ctx, client, region, key_id, aliases), key_ids
                )
            )
        return [resource for resource in found if resource is not None]

    def _aliases(self, client) -> dict[str, list[str]]:
        """Aliases per key. The alias is usually the only human-readable name a key has."""
        aliases: dict[str, list[str]] = {}
        try:
            for alias in paginate(client, "list_aliases", "Aliases"):
                target = alias.get("TargetKeyId")
                if target:
                    aliases.setdefault(target, []).append(alias["AliasName"])
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_aliases failed: %s", exc)
        return aliases

    def _describe(
        self,
        ctx: CollectionContext,
        client,
        region: str,
        key_id: str,
        aliases: dict[str, list[str]],
    ) -> Resource | None:
        try:
            metadata = client.describe_key(KeyId=key_id)["KeyMetadata"]
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_key failed for %s: %s", key_id, exc)
            return None

        # AWS managed and AWS owned keys are free, and there is nothing to act on either
        # way, so they are not worth carrying through the rest of the scan.
        if metadata.get("KeyManager") != "CUSTOMER":
            return None

        key_aliases = aliases.get(key_id, [])
        rotation = self._rotation_enabled(client, key_id, metadata)
        return Resource(
            arn=metadata["Arn"],
            resource_id=key_id,
            resource_type="kms:key",
            service="KMS",
            region=region,
            account_id=ctx.account_id,
            name=key_aliases[0] if key_aliases else None,
            state=metadata.get("KeyState"),
            created_at=metadata.get("CreationDate"),
            tags=self._tags(client, key_id),
            attributes={
                "aliases": key_aliases,
                "description": metadata.get("Description"),
                "key_manager": metadata.get("KeyManager"),
                "key_usage": metadata.get("KeyUsage"),
                "key_spec": metadata.get("KeySpec"),
                "multi_region": metadata.get("MultiRegion", False),
                "origin": metadata.get("Origin"),
                "rotation_enabled": rotation,
                # A key pending deletion still bills until the waiting period ends.
                "deletion_date": (
                    metadata["DeletionDate"].isoformat() if metadata.get("DeletionDate") else None
                ),
                "enabled": metadata.get("Enabled", False),
            },
        )

    def _rotation_enabled(self, client, key_id: str, metadata: dict) -> bool | None:
        # Rotation only applies to keys whose material AWS generates, and asking about
        # any other kind is an error rather than a no.
        if metadata.get("Origin") != "AWS_KMS" or metadata.get("KeyState") != "Enabled":
            return None
        try:
            return client.get_key_rotation_status(KeyId=key_id).get("KeyRotationEnabled")
        except (ClientError, BotoCoreError) as exc:
            logger.debug("get_key_rotation_status failed for %s: %s", key_id, exc)
            return None

    def _tags(self, client, key_id: str) -> dict[str, str]:
        try:
            response = client.list_resource_tags(KeyId=key_id)
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_resource_tags failed for %s: %s", key_id, exc)
            return {}
        return tags_to_dict(response.get("Tags"), key="TagKey", value="TagValue")


@register
class SecretsManagerCollector(Collector):
    key = "secrets"
    service = "Secrets Manager"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("secretsmanager", region)
        resources: list[Resource] = []
        for secret in paginate(client, "list_secrets", "SecretList"):
            rotation = secret.get("RotationRules") or {}
            resources.append(
                Resource(
                    arn=secret["ARN"],
                    resource_id=secret["Name"],
                    resource_type="secretsmanager:secret",
                    service="Secrets Manager",
                    region=region,
                    account_id=ctx.account_id,
                    name=secret["Name"],
                    # A secret scheduled for deletion stops being charged for.
                    state="pending-deletion" if secret.get("DeletedDate") else "active",
                    created_at=secret.get("CreatedDate"),
                    tags=tags_to_dict(secret.get("Tags")),
                    attributes={
                        "description": secret.get("Description"),
                        "rotation_enabled": secret.get("RotationEnabled", False),
                        "rotation_days": rotation.get("AutomaticallyAfterDays"),
                        "last_accessed_date": (
                            secret["LastAccessedDate"].isoformat()
                            if secret.get("LastAccessedDate")
                            else None
                        ),
                        "last_changed_date": (
                            secret["LastChangedDate"].isoformat()
                            if secret.get("LastChangedDate")
                            else None
                        ),
                        "deleted_date": (
                            secret["DeletedDate"].isoformat() if secret.get("DeletedDate") else None
                        ),
                        "primary_region": secret.get("PrimaryRegion"),
                        "kms_key_id": secret.get("KmsKeyId"),
                    },
                )
            )
        return resources


@register
class CertificateCollector(Collector):
    """ACM certificates, plus the private authorities that are the only paid part."""

    key = "acm"
    service = "Certificate Manager"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        resources = self._certificates(ctx, region)
        resources.extend(self._authorities(ctx, region))
        return resources

    def _certificates(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("acm", region)
        resources: list[Resource] = []
        for summary in paginate(client, "list_certificates", "CertificateSummaryList"):
            arn = summary["CertificateArn"]
            detail = self._describe(client, arn) or summary
            in_use_by = detail.get("InUseBy") or []
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=arn.rsplit("/", 1)[-1],
                    resource_type="acm:certificate",
                    service="Certificate Manager",
                    region=region,
                    account_id=ctx.account_id,
                    name=detail.get("DomainName") or summary.get("DomainName"),
                    state=detail.get("Status") or summary.get("Status"),
                    created_at=detail.get("CreatedAt") or detail.get("ImportedAt"),
                    tags=self._tags(client, arn),
                    attributes={
                        "domain_name": detail.get("DomainName"),
                        "subject_alternative_names": detail.get("SubjectAlternativeNames") or [],
                        # AMAZON_ISSUED public certificates are free; PRIVATE ones are
                        # charged when issued, and the authority is the standing cost.
                        "certificate_type": detail.get("Type"),
                        "certificate_authority_arn": detail.get("CertificateAuthorityArn"),
                        "in_use": bool(in_use_by),
                        "in_use_by": in_use_by[:10],
                        "renewal_eligibility": detail.get("RenewalEligibility"),
                        "not_after": (
                            detail["NotAfter"].isoformat() if detail.get("NotAfter") else None
                        ),
                        "key_algorithm": detail.get("KeyAlgorithm"),
                    },
                )
            )
        return resources

    def _describe(self, client, arn: str) -> dict | None:
        try:
            return client.describe_certificate(CertificateArn=arn).get("Certificate")
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_certificate failed for %s: %s", arn, exc)
            return None

    def _tags(self, client, arn: str) -> dict[str, str]:
        try:
            response = client.list_tags_for_certificate(CertificateArn=arn)
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_tags_for_certificate failed for %s: %s", arn, exc)
            return {}
        return tags_to_dict(response.get("Tags"))

    def _authorities(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("acm-pca", region)
        resources: list[Resource] = []
        for authority in paginate(client, "list_certificate_authorities", "CertificateAuthorities"):
            arn = authority["Arn"]
            usage_mode = authority.get("UsageMode", "GENERAL_PURPOSE")
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=arn.rsplit("/", 1)[-1],
                    resource_type="acm-pca:certificate-authority",
                    service="Certificate Manager",
                    region=region,
                    account_id=ctx.account_id,
                    name=(authority.get("CertificateAuthorityConfiguration") or {})
                    .get("Subject", {})
                    .get("CommonName"),
                    state=authority.get("Status"),
                    created_at=authority.get("CreatedAt"),
                    tags={},
                    attributes={
                        "authority_type": authority.get("Type"),
                        # Short-lived certificate mode is billed at an eighth of the
                        # general purpose rate.
                        "usage_mode": usage_mode,
                        "key_algorithm": (
                            authority.get("CertificateAuthorityConfiguration") or {}
                        ).get("KeyAlgorithm"),
                        # DISABLED and ACTIVE both bill; only DELETED stops the charge.
                        "billable": authority.get("Status")
                        not in {"DELETED", "CREATING", "FAILED", "PENDING_CERTIFICATE"},
                    },
                )
            )
        return resources
