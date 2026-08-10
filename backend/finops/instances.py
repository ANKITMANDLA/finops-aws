"""Instance type arithmetic: sizing down, generation upgrades, and Graviton equivalents."""

from __future__ import annotations

# Each size maps to the one with roughly half the vCPU and memory.
_HALVING_LADDER = {
    "48xlarge": "24xlarge",
    "32xlarge": "16xlarge",
    "24xlarge": "12xlarge",
    "16xlarge": "8xlarge",
    "12xlarge": "6xlarge",
    "9xlarge": "4xlarge",
    "8xlarge": "4xlarge",
    "6xlarge": "3xlarge",
    "4xlarge": "2xlarge",
    "3xlarge": "xlarge",
    "2xlarge": "xlarge",
    "xlarge": "large",
    "large": "medium",
    "medium": "small",
    "small": "micro",
    "micro": "nano",
}

# Families AWS has superseded. The replacement is the closest current-generation
# equivalent, which is normally both cheaper per hour and faster.
PREVIOUS_GENERATION = {
    "t1": "t3",
    "t2": "t3",
    "m1": "m7i",
    "m2": "r7i",
    "m3": "m7i",
    "m4": "m7i",
    "m5": "m7i",
    "m5a": "m7a",
    "c1": "c7i",
    "c3": "c7i",
    "c4": "c7i",
    "c5": "c7i",
    "c5a": "c7a",
    "r3": "r7i",
    "r4": "r7i",
    "r5": "r7i",
    "r5a": "r7a",
    "i2": "i4i",
    "i3": "i4i",
    "d2": "d3",
    "g2": "g5",
    "g3": "g5",
    "p2": "p4d",
    "x1": "x2idn",
}

# x86 families with a drop-in ARM equivalent. Graviton instances list at roughly 20%
# less per hour, but the workload must be rebuilt for arm64.
GRAVITON_EQUIVALENT = {
    "t3": "t4g",
    "t3a": "t4g",
    "t4": "t4g",
    "m5": "m7g",
    "m5a": "m7g",
    "m6i": "m7g",
    "m6a": "m7g",
    "m7i": "m7g",
    "m7a": "m7g",
    "c5": "c7g",
    "c5a": "c7g",
    "c6i": "c7g",
    "c6a": "c7g",
    "c7i": "c7g",
    "c7a": "c7g",
    "r5": "r7g",
    "r5a": "r7g",
    "r6i": "r7g",
    "r6a": "r7g",
    "r7i": "r7g",
    "r7a": "r7g",
}

# Published Graviton discount versus the equivalent x86 instance.
GRAVITON_DISCOUNT = 0.20

# Operating systems that are licensed per instance and cannot move to ARM.
ARM_INCOMPATIBLE_PLATFORMS = ("windows", "sql server", "red hat", "suse")


def split_instance_type(instance_type: str) -> tuple[str, str] | None:
    """Split ``m5.2xlarge`` into ``("m5", "2xlarge")``."""
    if not instance_type or "." not in instance_type:
        return None
    family, _, size = instance_type.partition(".")
    return (family, size) if family and size else None


def smaller_instance_type(instance_type: str) -> str | None:
    """The next size down in the same family, or None at the bottom of the ladder."""
    parts = split_instance_type(instance_type)
    if parts is None:
        return None
    family, size = parts
    smaller = _HALVING_LADDER.get(size)
    return f"{family}.{smaller}" if smaller else None


def current_generation_equivalent(instance_type: str) -> str | None:
    """The current-generation replacement for a superseded family."""
    parts = split_instance_type(instance_type)
    if parts is None:
        return None
    family, size = parts
    replacement = PREVIOUS_GENERATION.get(family)
    if not replacement:
        return None
    # t2.nano has no t3.nano-and-below concern, but burstable sizes do differ; keeping
    # the same size label is the safe, like-for-like suggestion.
    return f"{replacement}.{size}"


def graviton_equivalent(instance_type: str) -> str | None:
    parts = split_instance_type(instance_type)
    if parts is None:
        return None
    family, size = parts
    replacement = GRAVITON_EQUIVALENT.get(family)
    return f"{replacement}.{size}" if replacement else None


def is_previous_generation(instance_type: str) -> bool:
    parts = split_instance_type(instance_type)
    return bool(parts and parts[0] in PREVIOUS_GENERATION)


def supports_graviton(platform_details: str | None) -> bool:
    """Windows and per-core licensed platforms cannot move to ARM."""
    lowered = (platform_details or "linux").lower()
    return not any(token in lowered for token in ARM_INCOMPATIBLE_PLATFORMS)


def rds_instance_family(instance_class: str) -> str | None:
    """``db.r5.large`` yields ``r5``."""
    parts = instance_class.split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "db" else None


def rds_graviton_equivalent(instance_class: str) -> str | None:
    family = rds_instance_family(instance_class)
    if not family:
        return None
    replacement = {"m5": "m6g", "r5": "r6g", "t3": "t4g", "m6i": "m6g", "r6i": "r6g"}.get(family)
    if not replacement:
        return None
    return instance_class.replace(f".{family}.", f".{replacement}.", 1)
