"""Stub AWS clients for testing the cost and pricing layers.

moto does not implement Cost Explorer or the Price List API, so these return canned
responses shaped exactly like the real ones.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError


def client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} raised in test"}}, operation)


class FakeAwsContext:
    """Minimal stand-in for AwsContext that hands out a fixed client."""

    def __init__(self, client: Any, settings: Any = None, account_id: str = "111122223333") -> None:
        self._client = client
        self.settings = settings
        self.account_id = account_id
        self.default_region = "us-east-1"
        self.regions = ["us-east-1"]

    def client(self, service: str, region: str | None = None) -> Any:
        return self._client


def _daily_group(day: str, next_day: str, key: str, amount: float) -> dict[str, Any]:
    return {
        "TimePeriod": {"Start": day, "End": next_day},
        "Groups": [
            {"Keys": [key], "Metrics": {"AmortizedCost": {"Amount": str(amount), "Unit": "USD"}}}
        ],
    }


class FakeCostExplorerClient:
    """Canned Cost Explorer responses covering grouping, pagination, and commitments."""

    def __init__(self, *, fail: set[str] | None = None, resource_costs: bool = True) -> None:
        self.fail = fail or set()
        self.resource_costs = resource_costs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _guard(self, operation: str) -> None:
        self.calls.append((operation, {}))
        if operation in self.fail:
            raise client_error("AccessDeniedException", operation)

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_cost_and_usage")
        group_by = kwargs.get("GroupBy") or []
        key = group_by[0]["Key"] if group_by else None

        if key == "SERVICE":
            # Two pages, to exercise NextPageToken handling.
            if not kwargs.get("NextPageToken"):
                return {
                    "ResultsByTime": [
                        _daily_group(
                            "2026-07-01",
                            "2026-07-02",
                            "Amazon Elastic Compute Cloud - Compute",
                            100.0,
                        ),
                        _daily_group(
                            "2026-07-02",
                            "2026-07-03",
                            "Amazon Elastic Compute Cloud - Compute",
                            110.0,
                        ),
                    ],
                    "NextPageToken": "page-2",
                }
            return {
                "ResultsByTime": [
                    _daily_group("2026-07-01", "2026-07-02", "Amazon Simple Storage Service", 20.0),
                    _daily_group("2026-07-02", "2026-07-03", "Amazon Simple Storage Service", 25.0),
                ]
            }
        if key == "REGION":
            return {
                "ResultsByTime": [
                    _daily_group("2026-07-01", "2026-07-31", "us-east-1", 200.0),
                    _daily_group("2026-07-01", "2026-07-31", "eu-west-1", 55.0),
                ]
            }
        if key == "USAGE_TYPE":
            return {
                "ResultsByTime": [
                    _daily_group("2026-07-01", "2026-07-31", "BoxUsage:t3.large", 180.0),
                    _daily_group("2026-07-01", "2026-07-31", "NatGateway-Hours", 32.85),
                ]
            }
        # Ungrouped total (month to date, previous period).
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-31"},
                    "Total": {"AmortizedCost": {"Amount": "255.0", "Unit": "USD"}},
                    "Groups": [],
                }
            ]
        }

    def get_cost_forecast(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_cost_forecast")
        return {
            "Total": {"Amount": "300.0", "Unit": "USD"},
            "ForecastResultsByTime": [
                {
                    "PredictionIntervalLowerBound": "9.0",
                    "PredictionIntervalUpperBound": "11.0",
                }
                for _ in range(30)
            ],
        }

    def get_cost_and_usage_with_resources(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_cost_and_usage_with_resources")
        if not self.resource_costs:
            return {"ResultsByTime": []}
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-02"},
                    "Groups": [
                        {
                            "Keys": ["i-0123456789abcdef0"],
                            "Metrics": {"AmortizedCost": {"Amount": "7.0", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["NoResourceId"],
                            "Metrics": {"AmortizedCost": {"Amount": "3.0", "Unit": "USD"}},
                        },
                    ],
                }
            ]
        }

    def get_savings_plans_coverage(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_savings_plans_coverage")
        return {
            "SavingsPlansCoverages": [
                {"Coverage": {"CoveragePercentage": "40.0"}},
                {"Coverage": {"CoveragePercentage": "60.0"}},
            ]
        }

    def get_savings_plans_utilization(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_savings_plans_utilization")
        return {"Total": {"Utilization": {"UtilizationPercentage": "97.5"}}}

    def get_reservation_coverage(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_reservation_coverage")
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "22.0"}}}

    def get_reservation_utilization(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_reservation_utilization")
        return {"Total": {"UtilizationPercentage": "88.0"}}

    def get_savings_plans_purchase_recommendation(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_savings_plans_purchase_recommendation")
        return {
            "SavingsPlansPurchaseRecommendation": {
                "SavingsPlansPurchaseRecommendationSummary": {
                    "EstimatedMonthlySavingsAmount": "120.5",
                    "EstimatedSavingsPercentage": "18.4",
                    "HourlyCommitmentToPurchase": "0.75",
                    "CurrentOnDemandSpend": "900.0",
                    "EstimatedROI": "22.1",
                }
            }
        }

    def get_reservation_purchase_recommendation(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("get_reservation_purchase_recommendation")
        if kwargs.get("Service") != "Amazon Relational Database Service":
            return {"Recommendations": []}
        return {
            "Recommendations": [
                {
                    "RecommendationSummary": {
                        "TotalEstimatedMonthlySavingsAmount": "45.0",
                        "CurrencyCode": "USD",
                    }
                }
            ]
        }


def price_list_entry(
    usd: str,
    unit: str = "Hrs",
    *,
    usage_type: str = "TestUsage",
    tiers: dict[str, str] | None = None,
) -> str:
    """One PriceList entry, shaped like the real thing including its usage type.

    ``tiers`` maps beginRange to price for the volume-discounted charges AWS publishes as
    several dimensions on one product.
    """
    dimensions = (
        {
            f"ABC.JRTCKXETXF.TIER{index}": {
                "unit": unit,
                "beginRange": begin,
                "pricePerUnit": {"USD": price},
            }
            for index, (begin, price) in enumerate(tiers.items())
        }
        if tiers
        else {
            "ABC.JRTCKXETXF.6YS6EN2CT7": {
                "unit": unit,
                "beginRange": "0",
                "pricePerUnit": {"USD": usd},
            }
        }
    )
    return json.dumps(
        {
            "product": {
                "productFamily": "Compute Instance",
                "attributes": {"usagetype": usage_type},
            },
            "terms": {"OnDemand": {"ABC.JRTCKXETXF": {"priceDimensions": dimensions}}},
        }
    )


class FakePricingClient:
    """Price List API stub keyed by the most specific filter in the request.

    Prices are given as ``"0.08"``, as ``("0.08", "USW2-EBS:VolumeUsage.gp3")`` when the
    lookup narrows on a usage type, or with the published unit as a third element.
    """

    def __init__(
        self,
        prices: dict[str, str | tuple[str, ...]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.prices = prices or {}
        self.fail = fail
        self.call_count = 0
        self.requests: list[dict[str, str]] = []

    def get_products(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        if self.fail:
            raise client_error("AccessDeniedException", "GetProducts")
        filters = {f["Field"]: f["Value"] for f in kwargs.get("Filters", [])}
        self.requests.append(filters)
        lookup = next(
            (
                filters[field]
                for field in (
                    "instanceType",
                    "volumeApiName",
                    "group",
                    "volumeType",
                    # EFS publishes every storage tier under one product family.
                    "storageClass",
                    # Transit gateway and Client VPN charges have no product family at
                    # all; the operation is the only thing that identifies them.
                    "operation",
                    "productFamily",
                )
                if field in filters
            ),
            "",
        )
        entry = self.prices.get(lookup)
        if entry is None:
            return {"PriceList": []}
        published = (entry,) if isinstance(entry, str) else tuple(entry)
        usage_type = published[1] if len(published) > 1 else "TestUsage"
        unit = published[2] if len(published) > 2 else "Hrs"
        return {"PriceList": [price_list_entry(published[0], unit, usage_type=usage_type)]}
