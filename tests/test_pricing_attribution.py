from __future__ import annotations

from datetime import date

import pytest
from tests.factories import make_resource
from tests.fakes import FakeAwsContext, FakePricingClient, price_list_entry

from finops.attribution import attribute_costs
from finops.aws.costs import CostSnapshot
from finops.aws.errors import NoteCollector
from finops.aws.pricing import (
    FALLBACK_PRICES,
    HOURS_PER_MONTH,
    PricingClient,
    _extract_on_demand_usd,
)


def build_pricing(prices=None, *, fail=False, tmp_path=None, settings=None):
    api = FakePricingClient(prices, fail=fail)
    client = PricingClient(
        aws=FakeAwsContext(api, settings),
        notes=NoteCollector(),
        cache_path=(tmp_path / "pricing.json") if tmp_path else None,
    )
    return client, api


def test_extract_on_demand_usd_skips_zero_priced_entries():
    assert _extract_on_demand_usd([price_list_entry("0.0832")]) == 0.0832
    assert _extract_on_demand_usd([price_list_entry("0.0000"), price_list_entry("0.5")]) == 0.5
    assert _extract_on_demand_usd([]) is None
    assert _extract_on_demand_usd(["not json"]) is None


def test_instance_price_is_converted_to_a_monthly_figure(tmp_path, settings):
    pricing, _ = build_pricing({"m5.large": "0.096"}, tmp_path=tmp_path, settings=settings)
    assert pricing.ec2_instance_monthly("us-east-1", "m5.large") == pytest.approx(
        0.096 * HOURS_PER_MONTH
    )


def test_repeated_lookups_hit_the_cache_not_the_api(tmp_path, settings):
    pricing, api = build_pricing({"m5.large": "0.096"}, tmp_path=tmp_path, settings=settings)
    for _ in range(4):
        pricing.ec2_instance_hourly("us-east-1", "m5.large")
    assert api.call_count == 1


def test_cache_persists_across_client_instances(tmp_path, settings):
    pricing, api = build_pricing({"m5.large": "0.096"}, tmp_path=tmp_path, settings=settings)
    pricing.ec2_instance_hourly("us-east-1", "m5.large")
    pricing.save_cache()

    reloaded, fresh_api = build_pricing({}, tmp_path=tmp_path, settings=settings)
    price = reloaded.ec2_instance_hourly("us-east-1", "m5.large")
    assert price is not None and price.amount == 0.096
    assert fresh_api.call_count == 0


def test_a_denied_pricing_api_falls_back_and_stops_retrying(tmp_path, settings):
    pricing, api = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)

    # No fallback exists for instance hours, so the answer is honestly "unknown".
    assert pricing.ec2_instance_hourly("us-east-1", "m5.large") is None
    # Storage has a defensible published default.
    volume_price = pricing.ebs_gb_month("us-east-1", "gp3")
    assert volume_price is not None
    assert volume_price.source == "fallback"
    assert volume_price.amount == FALLBACK_PRICES["ebs:gp3:gb-month"]

    # One failure disables further calls for the rest of the scan.
    assert api.call_count == 1
    assert pricing.notes.has_problem("pricing:GetProducts")


def test_gp3_volume_cost_only_bills_iops_above_the_free_allowance(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)

    baseline = pricing.ebs_volume_monthly("us-east-1", "gp3", 100, iops=3000, throughput_mibps=125)
    assert baseline == pytest.approx(100 * FALLBACK_PRICES["ebs:gp3:gb-month"])

    boosted = pricing.ebs_volume_monthly("us-east-1", "gp3", 100, iops=6000, throughput_mibps=250)
    expected = (
        100 * FALLBACK_PRICES["ebs:gp3:gb-month"]
        + 3000 * FALLBACK_PRICES["ebs:gp3:iops-month"]
        + 125 * FALLBACK_PRICES["ebs:gp3:throughput-month"]
    )
    assert boosted == pytest.approx(expected)


def test_io1_bills_every_provisioned_iop(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)
    cost = pricing.ebs_volume_monthly("us-east-1", "io1", 50, iops=1000)
    expected = (
        50 * FALLBACK_PRICES["ebs:io1:gb-month"] + 1000 * FALLBACK_PRICES["ebs:io1:iops-month"]
    )
    assert cost == pytest.approx(expected)


# ------------------------------------------------------------------ attribution


def empty_snapshot(**kwargs):
    return CostSnapshot(period_start=date(2026, 7, 1), period_end=date(2026, 7, 31), **kwargs)


def test_billed_cost_beats_the_list_price_estimate(tmp_path, settings):
    pricing, _ = build_pricing({"t3.large": "0.0832"}, tmp_path=tmp_path, settings=settings)
    instance = make_resource("i-0123456789abcdef0", attributes={"instance_type": "t3.large"})
    snapshot = empty_snapshot(resource_costs={"i-0123456789abcdef0": 44.0})

    attribute_costs([instance], snapshot, pricing)

    assert instance.monthly_cost == 44.0
    assert instance.cost_basis == "actual_resource_level"


def test_list_price_is_used_when_no_billed_cost_exists(tmp_path, settings):
    pricing, _ = build_pricing({"t3.large": "0.0832"}, tmp_path=tmp_path, settings=settings)
    instance = make_resource(
        "i-abc",
        attributes={"instance_type": "t3.large", "platform_details": "Linux/UNIX"},
    )

    attribute_costs([instance], empty_snapshot(), pricing)

    assert instance.monthly_cost == pytest.approx(0.0832 * HOURS_PER_MONTH, abs=0.01)
    assert instance.cost_basis == "list_price_estimate"


def test_stopped_instances_and_amis_cost_nothing_of_their_own(tmp_path, settings):
    pricing, _ = build_pricing({"t3.large": "0.0832"}, tmp_path=tmp_path, settings=settings)
    stopped = make_resource("i-stopped", state="stopped", attributes={"instance_type": "t3.large"})
    ami = make_resource(
        "ami-1", resource_type="ec2:image", service="AMI", attributes={"backing_size_gb": 8}
    )

    attribute_costs([stopped, ami], empty_snapshot(), pricing)

    # Compute stops billing; the attached volume is charged against the volume itself.
    assert stopped.monthly_cost == 0.0
    # The AMI's snapshots are inventoried separately, so pricing it here would double count.
    assert ami.monthly_cost == 0.0


def test_spot_instances_are_left_unpriced_rather_than_overstated(tmp_path, settings):
    pricing, _ = build_pricing({"t3.large": "0.0832"}, tmp_path=tmp_path, settings=settings)
    spot = make_resource("i-spot", attributes={"instance_type": "t3.large", "lifecycle": "spot"})

    attribute_costs([spot], empty_snapshot(), pricing)

    assert spot.monthly_cost is None
    assert spot.cost_basis is None


def test_unattached_volume_is_priced_from_its_size_and_type(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)
    volume = make_resource(
        "vol-1",
        resource_type="ebs:volume",
        service="EBS",
        state="available",
        attributes={"volume_type": "gp2", "size_gb": 200},
        monthly_cost=None,
    )

    attribute_costs([volume], empty_snapshot(), pricing)

    assert volume.monthly_cost == pytest.approx(200 * FALLBACK_PRICES["ebs:gp2:gb-month"])
    assert volume.cost_basis == "list_price_estimate"


def test_log_group_cost_comes_from_stored_bytes(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)
    log_group = make_resource(
        "/aws/lambda/api",
        resource_type="logs:log-group",
        service="CloudWatch Logs",
        attributes={"stored_gb": 500.0, "never_expires": True},
        monthly_cost=None,
    )

    attribute_costs([log_group], empty_snapshot(), pricing)

    assert log_group.monthly_cost == pytest.approx(
        500.0 * FALLBACK_PRICES["logs:storage-gb-month"], abs=0.01
    )


def test_resources_without_a_defensible_price_stay_unpriced(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)
    unknown = make_resource(
        "some-thing", resource_type="sagemaker:endpoint", service="SageMaker", monthly_cost=None
    )

    attribute_costs([unknown], empty_snapshot(), pricing)

    assert unknown.monthly_cost is None
    assert unknown.cost_basis is None
