"""The published price list files, which price an account whose role lacks the API."""

from __future__ import annotations

import json
from datetime import date

import pytest
from tests.factories import make_resource
from tests.fakes import FakeAwsContext, FakePricingClient

from finops.attribution import attribute_costs
from finops.aws.costs import CostSnapshot
from finops.aws.errors import NoteCollector
from finops.aws.price_list import PriceListUnavailable, PublicPriceList, extract_rates
from finops.aws.pricing import PricingClient

# A published file, in the shape and style AWS writes it: one key per line, a space
# either side of every colon, products first, then on-demand terms, then the reserved
# terms that make up most of the bytes.
PUBLISHED = """{
  "formatVersion" : "v1.0",
  "disclaimer" : "This file is not intended to be read by humans.",
  "offerCode" : "AmazonEC2",
  "products" : {
    "AAAA1111BBBB2222" : {
      "sku" : "AAAA1111BBBB2222",
      "productFamily" : "Compute Instance",
      "attributes" : {
        "servicecode" : "AmazonEC2",
        "location" : "US West (Oregon)",
        "regionCode" : "us-west-2",
        "instanceType" : "m5.large",
        "operatingSystem" : "Linux",
        "tenancy" : "Shared",
        "preInstalledSw" : "NA",
        "capacitystatus" : "Used",
        "marketoption" : "OnDemand",
        "licenseModel" : "No License required",
        "usagetype" : "USW2-BoxUsage:m5.large",
        "operation" : "RunInstances"
      }
    },
    "CCCC3333DDDD4444" : {
      "sku" : "CCCC3333DDDD4444",
      "productFamily" : "Compute Instance",
      "attributes" : {
        "regionCode" : "us-west-2",
        "instanceType" : "m5.large",
        "operatingSystem" : "Linux",
        "tenancy" : "Dedicated",
        "preInstalledSw" : "NA",
        "capacitystatus" : "Used",
        "marketoption" : "OnDemand",
        "licenseModel" : "No License required",
        "usagetype" : "USW2-DedicatedUsage:m5.large"
      }
    },
    "EEEE5555FFFF6666" : {
      "sku" : "EEEE5555FFFF6666",
      "productFamily" : "Storage",
      "attributes" : {
        "regionCode" : "us-west-2",
        "volumeApiName" : "gp3",
        "usagetype" : "USW2-EBS:VolumeUsage.gp3"
      }
    }
  },
  "terms" : {
    "OnDemand" : {
      "AAAA1111BBBB2222" : {
        "AAAA1111BBBB2222.JRTCKXETXF" : {
          "offerTermCode" : "JRTCKXETXF",
          "sku" : "AAAA1111BBBB2222",
          "priceDimensions" : {
            "AAAA1111BBBB2222.JRTCKXETXF.6YS6EN2CT7" : {
              "unit" : "Hrs",
              "endRange" : "Inf",
              "description" : "$0.096 per On Demand Linux m5.large Instance Hour",
              "appliesTo" : [ ],
              "beginRange" : "0",
              "pricePerUnit" : {
                "USD" : "0.0960000000"
              }
            }
          }
        }
      },
      "CCCC3333DDDD4444" : {
        "CCCC3333DDDD4444.JRTCKXETXF" : {
          "priceDimensions" : {
            "CCCC3333DDDD4444.JRTCKXETXF.6YS6EN2CT7" : {
              "unit" : "Hrs",
              "beginRange" : "0",
              "pricePerUnit" : {
                "USD" : "1.2340000000"
              }
            }
          }
        }
      },
      "EEEE5555FFFF6666" : {
        "EEEE5555FFFF6666.JRTCKXETXF" : {
          "priceDimensions" : {
            "EEEE5555FFFF6666.JRTCKXETXF.6YS6EN2CT7" : {
              "unit" : "GB-Mo",
              "beginRange" : "0",
              "pricePerUnit" : {
                "USD" : "0.0800000000"
              }
            }
          }
        }
      }
    },
    "Reserved" : {
      "AAAA1111BBBB2222" : {
        "AAAA1111BBBB2222.4NA7Y494T4" : {
          "priceDimensions" : {
            "AAAA1111BBBB2222.4NA7Y494T4.2TG2D8R56U" : {
              "unit" : "Hrs",
              "pricePerUnit" : {
                "USD" : "0.0610000000"
              }
            }
          }
        }
      }
    }
  }
}
"""

M5_LARGE = {
    "regionCode": "us-west-2",
    "instanceType": "m5.large",
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "marketoption": "OnDemand",
    "licenseModel": "No License required",
}


def lines(document: str):
    return [f"{line}\n".encode() for line in document.splitlines()]


def price_list(document: str = PUBLISHED, cache_dir=None, calls=None):
    """A price list whose downloads are served from a string instead of the network."""

    def opener(url):
        if calls is not None:
            calls.append(url)
        yield from lines(document)

    return PublicPriceList(cache_dir, opener=opener)


# ------------------------------------------------------------------- extraction


def test_a_published_file_yields_its_on_demand_rates(tmp_path):
    rates = extract_rates(lines(PUBLISHED))

    published = {rate.attributes["usagetype"]: (rate.amount, rate.unit) for rate in rates}
    assert published["USW2-BoxUsage:m5.large"] == (0.096, "Hrs")
    assert published["USW2-EBS:VolumeUsage.gp3"] == (0.08, "GB-Mo")


def test_reserved_terms_are_never_read_as_an_on_demand_rate():
    rates = extract_rates(lines(PUBLISHED))

    # $0.061 is the reserved rate for the same instance, and reading it as on-demand
    # would understate the cost of every instance in the account.
    assert 0.061 not in [rate.amount for rate in rates]


def test_the_reader_stops_before_the_reserved_terms():
    read: list[str] = []

    def counted():
        for line in lines(PUBLISHED):
            read.append(line.decode())
            yield line

    extract_rates(counted())

    # The reserved section is the bulk of a real file; not reading it is the whole reason
    # a 450MB download takes seconds.
    assert not any("4NA7Y494T4" in line for line in read)


def test_a_layout_we_cannot_read_is_reported_rather_than_buffered():
    compact = json.dumps({"products": {"SKU": {"attributes": {}}}, "terms": {}})

    assert extract_rates(lines(compact)) == []


def test_an_enormous_block_is_refused_instead_of_swallowing_the_file():
    runaway = ['  "products" : {\n', '    "SKU" : {\n'] + ['      "x" : 1,\n'] * 20_000

    with pytest.raises(PriceListUnavailable):
        extract_rates(runaway)


# ----------------------------------------------------------------------- lookups


def test_a_rate_is_found_by_the_same_filters_the_api_uses(tmp_path):
    prices = price_list(cache_dir=tmp_path)

    assert prices.rate("AmazonEC2", "us-west-2", M5_LARGE, None) == (0.096, "Hrs")


def test_a_usage_type_narrows_the_match_through_its_region_prefix(tmp_path):
    prices = price_list(cache_dir=tmp_path)
    filters = {"regionCode": "us-west-2", "productFamily": "Storage", "volumeApiName": "gp3"}

    gp3 = prices.rate("AmazonEC2", "us-west-2", filters, r"EBS:VolumeUsage(\.\S+)?")

    assert gp3 == (0.08, "GB-Mo")
    assert prices.rate("AmazonEC2", "us-west-2", filters, "EBS:SnapshotUsage") is None


def test_dedicated_tenancy_is_dropped_so_the_ec2_cache_stays_small(tmp_path):
    prices = price_list(cache_dir=tmp_path)
    dedicated = M5_LARGE | {"tenancy": "Dedicated"}

    assert prices.rate("AmazonEC2", "us-west-2", dedicated, None) is None


def test_filters_that_match_nothing_are_no_rate_rather_than_a_wrong_one(tmp_path):
    prices = price_list(cache_dir=tmp_path)

    assert (
        prices.rate("AmazonEC2", "us-west-2", M5_LARGE | {"instanceType": "m5.24xlarge"}, None)
        is None
    )


def test_a_file_is_downloaded_once_per_service_and_region(tmp_path):
    calls: list[str] = []
    prices = price_list(cache_dir=tmp_path, calls=calls)

    prices.rate("AmazonEC2", "us-west-2", M5_LARGE, None)
    prices.rate("AmazonEC2", "us-west-2", M5_LARGE | {"instanceType": "m5.large"}, None)

    assert len(calls) == 1


def test_the_cache_survives_into_the_next_scan(tmp_path):
    calls: list[str] = []
    price_list(cache_dir=tmp_path, calls=calls).rate("AmazonEC2", "us-west-2", M5_LARGE, None)

    def refuse(url):
        raise AssertionError(f"downloaded {url} again instead of reading the cache")
        yield  # pragma: no cover

    fresh = PublicPriceList(tmp_path, opener=refuse)
    assert fresh.rate("AmazonEC2", "us-west-2", M5_LARGE, None) == (0.096, "Hrs")


def test_a_cache_from_an_older_layout_is_read_again_rather_than_trusted(tmp_path):
    stale = {"service": "AmazonEC2", "region": "us-west-2", "rates": [], "version": 1}
    (tmp_path / "AmazonEC2-us-west-2.json").write_text(json.dumps(stale), encoding="utf-8")
    calls: list[str] = []

    rate = price_list(cache_dir=tmp_path, calls=calls).rate(
        "AmazonEC2", "us-west-2", M5_LARGE, None
    )

    assert rate == (0.096, "Hrs")
    assert len(calls) == 1


def test_an_unreachable_endpoint_leaves_the_resource_unpriced(tmp_path):
    calls: list[str] = []

    def broken(url):
        calls.append(url)
        raise PriceListUnavailable("connection reset")
        yield  # pragma: no cover

    prices = PublicPriceList(tmp_path, opener=broken)

    assert prices.rate("AmazonEC2", "us-west-2", M5_LARGE, None) is None
    # A second lookup must not queue up another doomed download.
    assert prices.rate("AmazonEC2", "us-west-2", M5_LARGE, None) is None
    assert len(calls) == 1


# ------------------------------------------------------- alongside the pricing client


def build_client(tmp_path, *, api_fails: bool, published=PUBLISHED):
    api = FakePricingClient(
        {"m5.large": "0.096", "gp3": ("0.08", "USW2-EBS:VolumeUsage.gp3")}, fail=api_fails
    )
    client = PricingClient(
        aws=FakeAwsContext(api),
        notes=NoteCollector(),
        cache_path=tmp_path / "pricing.json",
        public_price_list=price_list(published, cache_dir=tmp_path / "published"),
    )
    return client, api


def test_a_denied_api_falls_back_to_the_published_file(tmp_path):
    client, _ = build_client(tmp_path, api_fails=True)

    price = client.ec2_instance_hourly("us-west-2", "m5.large")

    assert price is not None
    assert price.amount == 0.096
    assert price.source == "public-price-list"
    assert client.used_public_price_list


def test_the_published_file_is_left_alone_while_the_api_answers(tmp_path):
    def refuse(url):
        raise AssertionError(f"downloaded {url} when the API was available")
        yield  # pragma: no cover

    api = FakePricingClient({"m5.large": "0.096"})
    client = PricingClient(
        aws=FakeAwsContext(api),
        notes=NoteCollector(),
        cache_path=tmp_path / "pricing.json",
        public_price_list=PublicPriceList(tmp_path / "published", opener=refuse),
    )

    price = client.ec2_instance_hourly("us-west-2", "m5.large")

    assert price is not None and price.source == "pricing-api"
    assert not client.used_public_price_list


def test_the_scan_says_prices_came_from_the_published_files(tmp_path):
    client, _ = build_client(tmp_path, api_fails=True)
    resource = make_resource(
        region="us-west-2",
        attributes={"instance_type": "m5.large", "platform": "linux"},
        monthly_cost=None,
    )
    snapshot = CostSnapshot(period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))

    attribute_costs([resource], snapshot, client)

    note = next(n for n in client.notes.notes if n.capability == "pricing:list-price-estimates")
    assert note.status == "partial"
    assert "publishes" in note.message
    assert resource.monthly_cost == pytest.approx(0.096 * 730)
