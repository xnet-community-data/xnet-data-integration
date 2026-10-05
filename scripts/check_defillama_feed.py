#!/usr/bin/env python3
"""Validate XNET's published accrual feed and report DeFiLlama ingestion."""

import json
import urllib.error
import urllib.request
from decimal import Decimal

FEED = (
    "https://raw.githubusercontent.com/"
    "xnet-community-data/xnet-data-integration/main/"
    "data/xnet_defillama_revenue.json"
)


def dec(value):
    return Decimal(str(value))


def main():
    with urllib.request.urlopen(FEED, timeout=45) as response:
        feed = json.load(response)

    recognized = sum(
        (dec(row["fees_usd"]) for row in feed["data"]),
        Decimal("0"),
    )
    assert recognized == dec(
        feed["totals"]["recognized_service_revenue_usd"]
    )

    daily = feed["daily_data"]
    assert feed["daily_count"] == len(daily)
    assert len({row["date"] for row in daily}) == len(daily)

    daily_total = sum(
        (dec(row["fees_usd"]) for row in daily),
        Decimal("0"),
    )
    assert daily_total == dec(
        feed["totals"]["defillama_daily_accrual_usd"]
    )

    for month in feed["monthly_accrual"]:
        if month["basis"] == "provisional_live_offload":
            continue
        month_total = sum(
            (
                dec(row["fees_usd"])
                for row in daily
                if row["service_month"] == month["service_month"]
            ),
            Decimal("0"),
        )
        assert month_total == dec(month["accrual_total_usd"]), (
            month["service_month"],
            month_total,
            month["accrual_total_usd"],
        )

    conservative_rate = feed["projection_model"][
        "conservative_rate_usd_per_api_gb"
    ]
    published_rate = feed["projection_model"].get(
        "published_blended_rate_usd_per_billing_gb"
    )
    assert conservative_rate > 0
    if published_rate is not None:
        assert conservative_rate <= published_rate

    print(
        "Published feed: "
        f"{feed['daily_count']} daily accrual rows through "
        f"{daily[-1]['date']}; "
        f"USD {daily_total} total accrual; "
        f"USD {recognized} settlement-confirmed"
    )

    for slug in ("xnet", "xnet-mobile"):
        for metric in ("dailyFees", "dailyRevenue"):
            url = (
                f"https://api.llama.fi/summary/fees/{slug}"
                f"?dataType={metric}"
            )
            try:
                with urllib.request.urlopen(url, timeout=45) as response:
                    data = json.load(response)
                print(
                    json.dumps(
                        {
                            "slug": slug,
                            "metric": metric,
                            "name": data.get("name"),
                            "module": data.get("module"),
                            "totalAllTime": data.get("totalAllTime"),
                            "total24h": data.get("total24h"),
                            "total7d": data.get("total7d"),
                            "total30d": data.get("total30d"),
                        }
                    )
                )
            except urllib.error.HTTPError as error:
                print(
                    json.dumps(
                        {
                            "slug": slug,
                            "metric": metric,
                            "http_status": error.code,
                            "response": error.read().decode()[:400],
                        }
                    )
                )
            except (urllib.error.URLError, TimeoutError) as error:
                print(
                    json.dumps(
                        {
                            "slug": slug,
                            "metric": metric,
                            "ingestion_status": "unavailable",
                            "error": str(error)[:400],
                        }
                    )
                )
                print(
                    "::warning::DeFiLlama ingestion check unavailable for "
                    f"{slug}/{metric}; published feed validation passed."
                )


if __name__ == "__main__":
    main()
