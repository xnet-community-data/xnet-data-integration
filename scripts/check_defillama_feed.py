#!/usr/bin/env python3
"""Validate XNET's published accrual feed and report DeFiLlama ingestion."""

import json
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

FEED_PATH = Path("data/xnet_defillama_revenue.json")


def dec(value):
    return Decimal(str(value))


def main():
    with open(FEED_PATH, encoding="utf-8") as f:
        feed = json.load(f)

    assert feed["schema_version"] >= 4

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

    revenue_total = sum(
        (dec(row["revenue_usd"]) for row in daily),
        Decimal("0"),
    )
    supply_total = sum(
        (dec(row["supply_side_revenue_usd"]) for row in daily),
        Decimal("0"),
    )
    holders_total = sum(
        (dec(row["holders_revenue_usd"]) for row in daily),
        Decimal("0"),
    )
    protocol_total = sum(
        (dec(row["protocol_revenue_usd"]) for row in daily),
        Decimal("0"),
    )

    assert daily_total == revenue_total + supply_total
    assert revenue_total == holders_total + protocol_total
    assert revenue_total == dec(
        feed["totals"]["defillama_daily_revenue_usd"]
    )
    assert supply_total == dec(
        feed["totals"]["defillama_daily_supply_side_revenue_usd"]
    )
    assert holders_total == dec(
        feed["totals"]["defillama_daily_holders_revenue_usd"]
    )
    assert protocol_total == dec(
        feed["totals"]["defillama_daily_protocol_revenue_usd"]
    )

    for row in daily:
        assert dec(row["fees_usd"]) == (
            dec(row["revenue_usd"])
            + dec(row["supply_side_revenue_usd"])
        )
        assert dec(row["revenue_usd"]) == (
            dec(row["holders_revenue_usd"])
            + dec(row["protocol_revenue_usd"])
        )

        fiat_gross = dec(row.get("fiat_gross_allocation_usd", 0))
        ordinary_protocol = (
            dec(row["ordinary_operations_revenue_usd"])
            + dec(row["ordinary_protocol_owned_liquidity_usd"])
        )
        assert ordinary_protocol == dec(
            row["ordinary_protocol_revenue_usd"]
        )

        assert dec(row["holders_revenue_usd"]) == (
            dec(row["ordinary_holders_revenue_usd"])
            + dec(row["fiat_bbb_allocation_usd"])
        )
        assert dec(row["protocol_revenue_usd"]) == (
            dec(row["ordinary_protocol_revenue_usd"])
            + dec(row["fiat_operations_allocation_usd"])
        )

        if fiat_gross:
            assert row["fiat_allocation_basis"] == (
                "xip_13_1_net60_two_month_service_lag"
            )
            assert fiat_gross == (
                dec(row["fiat_operator_payout_usd"])
                + dec(row["fiat_bbb_allocation_usd"])
                + dec(row["fiat_operations_allocation_usd"])
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

    fiat = feed.get("fiat_operator_transfers", [])
    operator_total = sum(
        (dec(row["operator_payout_usd"]) for row in fiat),
        Decimal("0"),
    )
    gross_total = sum(
        (dec(row["gross_fiat_allocation_usd"]) for row in fiat),
        Decimal("0"),
    )
    bbb_total = sum(
        (dec(row["bbb_allocation_usd"]) for row in fiat),
        Decimal("0"),
    )
    operations_total = sum(
        (dec(row["operations_allocation_usd"]) for row in fiat),
        Decimal("0"),
    )

    assert gross_total == operator_total + bbb_total + operations_total
    assert operator_total == dec(feed["totals"]["fiat_operator_payout_usd"])
    assert gross_total == dec(feed["totals"]["fiat_gross_allocation_usd"])
    assert bbb_total == dec(feed["totals"]["fiat_bbb_allocation_usd"])
    assert operations_total == dec(
        feed["totals"]["fiat_operations_allocation_usd"]
    )

    capped_months = {"2026-04", "2026-05", "2026-06"}
    monthly_by_key = {
        row["service_month"]: row
        for row in feed["monthly_accrual"]
    }
    unsettled_keys = {
        row["service_month"]
        for row in feed["unsettled_service_months"]
    }

    for service_month in capped_months:
        row = monthly_by_key[service_month]
        assert row["basis"] == "confirmed_settlement"
        assert dec(row["accrual_total_usd"]) == Decimal("15000.00")
        assert dec(row["settlement_confirmed_usd"]) == Decimal("15000.00")
        assert service_month not in unsettled_keys

    capped_settlements = [
        row
        for row in feed["settlements"]
        if row["reconciliation_method"]
        == "two_month_lag_final_historical_cap"
    ]
    assert {
        item["service_month"]
        for row in capped_settlements
        for item in row["service_months"]
    } == capped_months

    for transfer in fiat:
        source_y, source_m = map(int, transfer["source_month"].split("-"))
        absolute = source_y * 12 + (source_m - 1) - 2
        service_y, service_zero_m = divmod(absolute, 12)
        expected_service_month = (
            f"{service_y:04d}-{service_zero_m + 1:02d}"
        )
        assert transfer["service_month"] == expected_service_month
        assert transfer["attribution_basis"] == (
            "xip_13_1_net60_two_month_service_lag"
        )

    assert daily_total == (
        recognized
        + dec(feed["totals"]["unconfirmed_accrual_component_usd"])
    )

    print(
        "Validated local feed: "
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
