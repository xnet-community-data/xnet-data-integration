#!/usr/bin/env python3
"""Build XNET's auditable DeFiLlama fee/revenue accrual feed.

The model has two layers:
1. Settlement reconciliation keeps an auditable mapping between recorded carrier
   payments and service months.
2. Daily accrual uses measured daily offload to shape each service month.
   Confirmed months are scaled exactly to settlement-confirmed revenue.
   Unsettled closed months use the official XNET monthly projection.
   Newer days without an official monthly projection use a conservative
   effective revenue-per-API-GB rate calibrated from the latest complete month.

When a later projection or settlement arrives, historical daily values are
recomputed so the relevant service month reconciles exactly.
"""

import json
from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

INPUT_PATH = Path("data/xnet_revenue_monthly.json")
OFFLOAD_PATH = Path("data/xnet_offload_api.json")
OUTPUT_PATH = Path("data/xnet_defillama_revenue.json")

TOLERANCE = Decimal("0.02")
CENT = Decimal("0.01")

PARTIAL_SETTLEMENT_OVERRIDES = {
    ("2026-07", Decimal("12000.00")): {
        "service_month": "2026-05",
        "reason": (
            "Confirmed partial WiFi payment reported in the July 2026 "
            "source-sheet column and attributed to May 2026 using the "
            "established settlement-month sequence. The unconfirmed "
            "remainder is left unsettled."
        ),
    },
}


def D(value):
    return None if value is None else Decimal(str(value))


def money(value):
    return float(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def month_index(month):
    year, month_num = map(int, month[:7].split("-"))
    return year * 12 + month_num


def month_start(month):
    return f"{month[:7]}-01"


def month_end(month):
    year, month_num = map(int, month[:7].split("-"))
    return date(year, month_num, monthrange(year, month_num)[1])


def expected_days(month):
    year, month_num = map(int, month[:7].split("-"))
    last = monthrange(year, month_num)[1]
    return [f"{year:04d}-{month_num:02d}-{day:02d}" for day in range(1, last + 1)]


def find_candidates(services, amount, payment_date, recognized_by_service):
    candidates = []

    for i, service in enumerate(services):
        remaining = service["amount"] - recognized_by_service.get(
            service["month"], Decimal("0")
        )

        if remaining <= TOLERANCE:
            continue

        if month_end(service["month"]) > payment_date:
            continue

        total = Decimal("0")
        previous_index = None
        months = []

        for j in range(i, len(services)):
            candidate = services[j]

            if month_end(candidate["month"]) > payment_date:
                break

            candidate_remaining = candidate["amount"] - recognized_by_service.get(
                candidate["month"], Decimal("0")
            )

            if candidate_remaining <= TOLERANCE:
                break

            current_index = month_index(candidate["month"])

            if previous_index is not None and current_index != previous_index + 1:
                break

            total += candidate_remaining
            months.append(
                {
                    "month": candidate["month"],
                    "amount": candidate_remaining,
                }
            )
            previous_index = current_index

            if abs(total - amount) <= TOLERANCE:
                candidates.append(list(months))

            if total > amount + TOLERANCE:
                break

    return candidates


def allocate_cents(total_usd, daily_rows):
    """Allocate a monthly USD total proportionally to GB, exactly to the cent."""
    total = Decimal(total_usd).quantize(CENT, rounding=ROUND_HALF_UP)
    total_cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))

    if not daily_rows:
        raise RuntimeError("Cannot allocate a monthly total without daily rows")

    weights = [Decimal(str(row["gigabytes"])) for row in daily_rows]
    weight_total = sum(weights, Decimal("0"))

    if weight_total <= 0:
        weights = [Decimal("1")] * len(daily_rows)
        weight_total = Decimal(len(daily_rows))

    raw = [Decimal(total_cents) * weight / weight_total for weight in weights]
    base = [
        int(value.to_integral_value(rounding=ROUND_FLOOR))
        for value in raw
    ]
    remainder = total_cents - sum(base)

    order = sorted(
        range(len(raw)),
        key=lambda i: (-(raw[i] - Decimal(base[i])), daily_rows[i]["date"]),
    )

    for i in order[:remainder]:
        base[i] += 1

    allocated = [Decimal(cents) / Decimal("100") for cents in base]

    if sum(allocated, Decimal("0")) != total:
        raise RuntimeError("Cent allocation failed to reconcile to monthly total")

    return allocated


def load_offload():
    with open(OFFLOAD_PATH, encoding="utf-8") as f:
        payload = json.load(f)

    rows = payload.get("data", [])
    if not rows:
        raise RuntimeError("Offload cache is empty")

    points = {}
    for row in rows:
        day = str(row["date"])
        gigabytes = D(row["gigabytes"])
        if gigabytes is None or gigabytes < 0:
            raise RuntimeError(f"Invalid offload value for {day}")
        points[day] = gigabytes

    return payload, points


def month_points(points, service_month):
    prefix = service_month[:7] + "-"
    return [
        {"date": day, "gigabytes": points[day]}
        for day in sorted(points)
        if day.startswith(prefix)
    ]


def complete_month(points, service_month):
    return all(day in points for day in expected_days(service_month))


def main():
    with open(INPUT_PATH, encoding="utf-8") as f:
        source = json.load(f)

    offload_source, offload = load_offload()
    rows = source["data"]

    fiat_operator_rows = []
    for row in rows:
        amount = D(row.get("transferred_to_fiat_operators_usd"))
        if amount is not None and abs(amount) > TOLERANCE:
            fiat_operator_rows.append(
                {
                    "source_month": row["month"],
                    "amount_usd": money(amount),
                }
            )

    fiat_operator_transfers = [
        {
            "source_month": row["source_month"][:7],
            "amount_usd": row["amount_usd"],
            "transfer_date": None,
            "status": "reported_month_only",
        }
        for row in fiat_operator_rows
    ]

    services = [
        {
            "month": row["month"],
            "amount": D(row["wifi_revenue_projected_usd"]),
        }
        for row in rows
        if row["wifi_revenue_projected_usd"] is not None
    ]
    services.sort(key=lambda row: row["month"])

    payments = [
        {
            "source_month": row["month"],
            "payment_date": row["wifi_payment_date"],
            "amount": D(row["wifi_payment_received_usd"]),
        }
        for row in rows
        if row["wifi_payment_received_usd"] is not None
    ]
    payments.sort(
        key=lambda row: (
            row["payment_date"] is None,
            row["payment_date"] or "9999-12-31",
            row["source_month"],
        )
    )

    recognized_by_service = {}
    settlements = []
    unattributed = []
    recognized = []

    for payment in payments:
        if payment["payment_date"] is None:
            source_month = payment["source_month"][:7]
            override_key = (
                source_month,
                payment["amount"].quantize(CENT),
            )
            override = PARTIAL_SETTLEMENT_OVERRIDES.get(override_key)

            if override is None:
                unattributed.append(
                    {
                        "source_month": source_month,
                        "payment_received_usd": money(payment["amount"]),
                        "payment_received_date": None,
                        "reason": "Source reports an amount but no payment date.",
                    }
                )
                continue

            target_month = month_start(override["service_month"])
            target_service = next(
                (
                    service
                    for service in services
                    if service["month"] == target_month
                ),
                None,
            )

            if target_service is None:
                raise RuntimeError(
                    "Partial settlement override references an unknown "
                    f"service month: {override['service_month']}"
                )

            already_recognized = recognized_by_service.get(
                target_month, Decimal("0")
            )
            remaining = target_service["amount"] - already_recognized

            if payment["amount"] > remaining + TOLERANCE:
                raise RuntimeError(
                    "Partial settlement override exceeds the remaining "
                    f"service amount for {override['service_month']}: "
                    f"payment={payment['amount']}, remaining={remaining}"
                )

            recognized_by_service[target_month] = (
                already_recognized + payment["amount"]
            )

            settlement_id = (
                f"undated-{source_month}-{money(payment['amount']):.2f}"
            )
            service_month = override["service_month"]

            settlements.append(
                {
                    "settlement_id": settlement_id,
                    "payment_received_date": None,
                    "payment_received_usd": money(payment["amount"]),
                    "source_sheet_column": source_month,
                    "service_months": [
                        {
                            "service_month": service_month,
                            "service_revenue_usd": money(payment["amount"]),
                        }
                    ],
                    "reconciliation_method": "explicit_partial_source_month_lag",
                    "difference_usd": 0.0,
                    "attribution_note": override["reason"],
                }
            )

            recognized.append(
                {
                    "date": month_end(service_month).isoformat(),
                    "service_month": service_month,
                    "fees_usd": money(payment["amount"]),
                    "user_fees_usd": money(payment["amount"]),
                    "settlement_id": settlement_id,
                    "payment_received_date": None,
                    "recognition_basis": (
                        "confirmed_partial_payment_attributed_to_service_month"
                    ),
                }
            )
            continue

        payment_date = date.fromisoformat(payment["payment_date"])
        candidates = find_candidates(
            services,
            payment["amount"],
            payment_date,
            recognized_by_service,
        )

        if len(candidates) != 1:
            unattributed.append(
                {
                    "source_month": payment["source_month"][:7],
                    "payment_received_usd": money(payment["amount"]),
                    "payment_received_date": payment["payment_date"],
                    "reason": (
                        "No unique exact contiguous service-period "
                        f"reconciliation. Candidate count: {len(candidates)}."
                    ),
                }
            )
            continue

        matched = candidates[0]
        settlement_id = (
            f"{payment['payment_date']}-{money(payment['amount']):.2f}"
        )
        settlement = {
            "settlement_id": settlement_id,
            "payment_received_date": payment["payment_date"],
            "payment_received_usd": money(payment["amount"]),
            "source_sheet_column": payment["source_month"][:7],
            "service_months": [],
            "reconciliation_method": "exact_contiguous_sum",
            "difference_usd": 0.0,
        }
        matched_total = Decimal("0")

        for service in matched:
            recognized_by_service[service["month"]] = (
                recognized_by_service.get(service["month"], Decimal("0"))
                + service["amount"]
            )
            matched_total += service["amount"]
            service_month = service["month"][:7]

            settlement["service_months"].append(
                {
                    "service_month": service_month,
                    "service_revenue_usd": money(service["amount"]),
                }
            )

            recognized.append(
                {
                    "date": month_end(service_month).isoformat(),
                    "service_month": service_month,
                    "fees_usd": money(service["amount"]),
                    "user_fees_usd": money(service["amount"]),
                    "settlement_id": settlement_id,
                    "payment_received_date": payment["payment_date"],
                    "recognition_basis": (
                        "confirmed_payment_reconciled_to_service_month"
                    ),
                }
            )

        settlement["difference_usd"] = money(matched_total - payment["amount"])
        settlements.append(settlement)

    recognized.sort(key=lambda row: (row["date"], row["settlement_id"]))

    source_received_total = sum(
        (
            D(row["wifi_payment_received_usd"]) or Decimal("0")
            for row in rows
        ),
        Decimal("0"),
    )
    recognized_total = sum(
        (Decimal(str(row["fees_usd"])) for row in recognized),
        Decimal("0"),
    )
    unattributed_total = sum(
        (
            Decimal(str(row["payment_received_usd"]))
            for row in unattributed
        ),
        Decimal("0"),
    )

    if abs(
        source_received_total
        - recognized_total
        - unattributed_total
    ) > TOLERANCE:
        raise RuntimeError(
            "Payment accounting does not reconcile: "
            f"source={source_received_total}, "
            f"recognized={recognized_total}, "
            f"unattributed={unattributed_total}"
        )

    calibrations = []
    for row in rows:
        service_month = row["month"][:7]
        projected = D(row.get("wifi_revenue_projected_usd"))
        sheet_rate = D(row.get("blended_rate_per_gb_projected_usd"))

        if projected is None or not complete_month(offload, service_month):
            continue

        daily = month_points(offload, service_month)
        api_gb = sum(
            (item["gigabytes"] for item in daily),
            Decimal("0"),
        )

        if api_gb <= 0:
            continue

        effective_rate = projected / api_gb
        conservative_rate = (
            min(effective_rate, sheet_rate)
            if sheet_rate is not None
            else effective_rate
        )

        calibrations.append(
            {
                "service_month": service_month,
                "api_offload_gb": api_gb,
                "billing_gb": D(row.get("gb_per_month")),
                "projected_revenue_usd": projected,
                "sheet_blended_rate_usd_per_billing_gb": sheet_rate,
                "effective_api_rate_usd_per_gb": effective_rate,
                "conservative_api_rate_usd_per_gb": conservative_rate,
            }
        )

    if not calibrations:
        raise RuntimeError("No complete month is available to calibrate live revenue")

    calibrations.sort(key=lambda row: row["service_month"])
    latest_calibration = calibrations[-1]
    live_rate = latest_calibration["conservative_api_rate_usd_per_gb"]

    backtest = []
    for previous, current in zip(calibrations, calibrations[1:]):
        forecast = (
            previous["conservative_api_rate_usd_per_gb"]
            * current["api_offload_gb"]
        )
        actual = current["projected_revenue_usd"]
        error_pct = (
            (forecast - actual) / actual * Decimal("100")
            if actual
            else Decimal("0")
        )
        backtest.append(
            {
                "forecast_month": current["service_month"],
                "rate_source_month": previous["service_month"],
                "forecast_revenue_usd": money(forecast),
                "official_projected_revenue_usd": money(actual),
                "error_pct": float(
                    error_pct.quantize(
                        Decimal("0.001"),
                        rounding=ROUND_HALF_UP,
                    )
                ),
            }
        )

    daily_data = []
    monthly_accrual = []

    for row in rows:
        service_month = row["month"][:7]
        projected = D(row.get("wifi_revenue_projected_usd"))
        recognized_amount = recognized_by_service.get(
            row["month"], Decimal("0")
        )
        daily = month_points(offload, service_month)

        if not daily:
            continue

        full_offload_month = complete_month(offload, service_month)
        target = None
        basis = None

        if projected is not None and recognized_amount >= projected - TOLERANCE:
            target = recognized_amount
            basis = "confirmed_settlement"
        elif projected is not None and full_offload_month:
            target = projected
            basis = (
                "official_projection_partially_confirmed"
                if recognized_amount > TOLERANCE
                else "official_projection"
            )

        if target is not None:
            amounts = allocate_cents(target, daily)
            for item, amount in zip(daily, amounts):
                daily_data.append(
                    {
                        "date": item["date"],
                        "service_month": service_month,
                        "offload_gb": float(item["gigabytes"]),
                        "fees_usd": money(amount),
                        "user_fees_usd": money(amount),
                        "basis": basis,
                    }
                )

            monthly_accrual.append(
                {
                    "service_month": service_month,
                    "basis": basis,
                    "daily_shape": "measured_network_offload",
                    "offload_days": len(daily),
                    "api_offload_gb": float(
                        sum(
                            (item["gigabytes"] for item in daily),
                            Decimal("0"),
                        )
                    ),
                    "accrual_total_usd": money(target),
                    "official_projected_revenue_usd": (
                        money(projected) if projected is not None else None
                    ),
                    "settlement_confirmed_usd": money(recognized_amount),
                }
            )
            continue

        live_total = Decimal("0")
        for item in daily:
            amount = (
                item["gigabytes"] * live_rate
            ).quantize(CENT, rounding=ROUND_HALF_UP)
            live_total += amount
            daily_data.append(
                {
                    "date": item["date"],
                    "service_month": service_month,
                    "offload_gb": float(item["gigabytes"]),
                    "fees_usd": money(amount),
                    "user_fees_usd": money(amount),
                    "basis": "provisional_live_offload",
                    "rate_usd_per_api_gb": float(
                        live_rate.quantize(
                            Decimal("0.000001"),
                            rounding=ROUND_HALF_UP,
                        )
                    ),
                    "rate_source_month": latest_calibration["service_month"],
                }
            )

        monthly_accrual.append(
            {
                "service_month": service_month,
                "basis": "provisional_live_offload",
                "daily_shape": "measured_network_offload",
                "offload_days": len(daily),
                "api_offload_gb": float(
                    sum(
                        (item["gigabytes"] for item in daily),
                        Decimal("0"),
                    )
                ),
                "accrual_total_usd": money(live_total),
                "official_projected_revenue_usd": None,
                "settlement_confirmed_usd": money(recognized_amount),
                "rate_usd_per_api_gb": float(
                    live_rate.quantize(
                        Decimal("0.000001"),
                        rounding=ROUND_HALF_UP,
                    )
                ),
                "rate_source_month": latest_calibration["service_month"],
            }
        )

    daily_data.sort(key=lambda row: row["date"])

    if len({row["date"] for row in daily_data}) != len(daily_data):
        raise RuntimeError("Duplicate dates found in daily DeFiLlama accrual")

    for month in monthly_accrual:
        if month["basis"] == "provisional_live_offload":
            continue
        actual = sum(
            (
                Decimal(str(row["fees_usd"]))
                for row in daily_data
                if row["service_month"] == month["service_month"]
            ),
            Decimal("0"),
        )
        expected = Decimal(str(month["accrual_total_usd"]))
        if actual != expected:
            raise RuntimeError(
                f"Daily accrual does not reconcile for {month['service_month']}: "
                f"{actual} != {expected}"
            )

    unsettled_services = []
    for service in services:
        recognized_amount = recognized_by_service.get(
            service["month"], Decimal("0")
        )
        remaining = service["amount"] - recognized_amount

        if remaining <= TOLERANCE:
            continue

        unsettled_services.append(
            {
                "service_month": service["month"][:7],
                "projected_service_revenue_usd": money(service["amount"]),
                "recognized_service_revenue_usd": money(recognized_amount),
                "remaining_service_revenue_usd": money(remaining),
                "status": (
                    "partially_recognized"
                    if recognized_amount > TOLERANCE
                    else "not_recognized"
                ),
            }
        )

    daily_total = sum(
        (Decimal(str(row["fees_usd"])) for row in daily_data),
        Decimal("0"),
    )
    confirmed_daily_total = sum(
        (
            Decimal(str(row["fees_usd"]))
            for row in daily_data
            if row["basis"] == "confirmed_settlement"
        ),
        Decimal("0"),
    )
    provisional_daily_total = daily_total - confirmed_daily_total

    recent_calibration = calibrations[-4:]
    recent_gap_pct = []
    for item in recent_calibration:
        billing_gb = item["billing_gb"]
        if billing_gb is None or billing_gb <= 0:
            continue
        gap = (
            (item["api_offload_gb"] - billing_gb)
            / billing_gb
            * Decimal("100")
        )
        recent_gap_pct.append(gap)

    recent_backtest = backtest[-3:]
    max_recent_abs_error = max(
        (abs(Decimal(str(row["error_pct"]))) for row in recent_backtest),
        default=Decimal("0"),
    )

    output = {
        "schema_version": 2,
        "accounting_basis": "hybrid_accrual_reconciled",
        "source": source["source"],
        "offload_source": {
            "name": offload_source["source"]["name"],
            "url": offload_source["source"]["url"],
            "first_date": offload_source["first_date"],
            "last_date": offload_source["last_date"],
            "count": offload_source["count"],
        },
        "methodology": {
            "fees": (
                "Carrier WiFi offload service fees are reported on an accrual "
                "basis. Daily values follow measured XNET network offload. "
                "Settlement-confirmed service months are scaled so their daily "
                "values sum exactly to confirmed carrier revenue. Closed "
                "unsettled months use XNET's official monthly projected WiFi "
                "revenue, distributed across days in proportion to measured "
                "offload. Newer days without an official monthly projection "
                "use measured daily offload multiplied by the latest "
                "conservative effective revenue-per-API-GB rate."
            ),
            "live_projection": (
                "The live rate is calibrated as official projected service "
                "revenue divided by total measured daily API offload for the "
                "latest complete month. It is capped at the published blended "
                "billing rate. This is deliberately more conservative than "
                "multiplying network offload by the raw billing rate because "
                "measured network offload has recently run modestly above "
                "revenue/billing GB."
            ),
            "reconciliation": (
                "When an official monthly projection or later carrier "
                "settlement becomes available, the affected historical daily "
                "values are recomputed. The confirmed monthly amount is "
                "distributed in proportion to actual daily offload, preserving "
                "the observed traffic shape while forcing the daily values to "
                "sum exactly to the reconciled service-month revenue."
            ),
            "partial_settlements": (
                "A partial payment confirms part of a service month but is not "
                "added on top of the month's provisional accrual. The confirmed "
                "portion is tracked separately until the service month is fully "
                "settled. The $12,000 receipt reported in the July 2026 source "
                "column is attributed to May 2026; the remaining May balance "
                "stays unsettled."
            ),
            "missing_offload": (
                "Missing offload observations are never filled with zero. If "
                "the upstream API is unavailable, the last valid public cache "
                "is retained and later observations are incorporated when the "
                "API resumes."
            ),
            "revenue": "Same as Fees.",
            "policy_allocation": (
                "Before 2025-05-22, 80% of carrier service revenue is "
                "attributed to Holders Revenue and 20% to Protocol Revenue. "
                "From 2025-05-22 under XIP-12, 60% is attributed to Holders "
                "Revenue and 40% to Protocol Revenue, comprising 20% "
                "protocol-owned liquidity and 20% operations."
            ),
        },
        "projection_model": {
            "name": "measured_offload_times_conservative_effective_api_rate",
            "rate_source_month": latest_calibration["service_month"],
            "effective_rate_usd_per_api_gb": float(
                latest_calibration[
                    "effective_api_rate_usd_per_gb"
                ].quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            ),
            "conservative_rate_usd_per_api_gb": float(
                live_rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            ),
            "published_blended_rate_usd_per_billing_gb": (
                float(latest_calibration["sheet_blended_rate_usd_per_billing_gb"])
                if latest_calibration["sheet_blended_rate_usd_per_billing_gb"]
                is not None
                else None
            ),
            "recent_complete_month_api_vs_billing_gb_gap_pct": [
                float(
                    value.quantize(
                        Decimal("0.001"),
                        rounding=ROUND_HALF_UP,
                    )
                )
                for value in recent_gap_pct
            ],
            "recent_previous_month_rate_backtest": recent_backtest,
            "max_abs_error_pct_recent_backtest": float(
                max_recent_abs_error.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                )
            ),
        },
        "totals": {
            "source_payments_received_usd": money(source_received_total),
            "recognized_service_revenue_usd": money(recognized_total),
            "unattributed_payments_usd": money(unattributed_total),
            "defillama_daily_accrual_usd": money(daily_total),
            "confirmed_daily_accrual_usd": money(confirmed_daily_total),
            "provisional_daily_accrual_usd": money(provisional_daily_total),
        },
        "count": len(recognized),
        "daily_count": len(daily_data),
        "data": recognized,
        "daily_data": daily_data,
        "monthly_accrual": monthly_accrual,
        "settlements": settlements,
        "unattributed_settlements": unattributed,
        "fiat_operator_transfers": fiat_operator_transfers,
        "unsettled_service_months": unsettled_services,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {OUTPUT_PATH}")
    print(
        f"Settlement-confirmed service revenue: "
        f"USD {money(recognized_total):,.2f}"
    )
    print(
        f"Daily DeFiLlama accrual: USD {money(daily_total):,.2f} "
        f"across {len(daily_data)} days"
    )
    print(
        "Live projection rate: "
        f"USD {live_rate:.6f}/API-GB from "
        f"{latest_calibration['service_month']}"
    )
    if recent_backtest:
        print(
            "Recent previous-month-rate max absolute error: "
            f"{max_recent_abs_error:.3f}%"
        )


if __name__ == "__main__":
    main()
