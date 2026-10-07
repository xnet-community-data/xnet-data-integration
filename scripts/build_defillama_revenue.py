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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

INPUT_PATH = Path("data/xnet_revenue_monthly.json")
OFFLOAD_PATH = Path("data/xnet_offload_api.json")
OUTPUT_PATH = Path("data/xnet_defillama_revenue.json")

TOLERANCE = Decimal("0.02")
CENT = Decimal("0.01")
MAX_OUTAGE_FALLBACK_DAYS = 14
OUTAGE_FALLBACK_LOOKBACK_DAYS = 7
MAX_LAG_FALLBACK_VARIANCE_PCT = Decimal("15")

# Fiat-deployer option. The fiat payout itself is 75% of the gross allocation.
# The remaining 25% is split 5% to BBB and 20% to XNET operations. The
# corresponding token emissions are burned, but the USD revenue sheet does not
# provide enough information to derive a token amount safely.
FIAT_OPERATOR_SHARE = Decimal("0.75")
FIAT_BBB_SHARE = Decimal("0.05")
FIAT_OPERATIONS_SHARE = Decimal("0.20")

# XIP-13.1 is listed Passed in the official XIP index. Fiat payouts are
# recorded after the carrier settlement that funds them. Attribute each payout
# to the service month already reconciled for the immediately preceding carrier
# source month rather than subtracting months from the fiat row itself.
FIAT_CARRIER_SOURCE_OFFSET_MONTHS = 1
XIP_12_EFFECTIVE_DATE = "2025-05-22"
XIP_INDEX_URL = "https://github.com/XNET-Foundation/XIP"
XIP_12_URL = "https://github.com/XNET-Foundation/XIP/blob/main/XIP-12.md"
XIP_13_1_URL = (
    "https://github.com/XNET-Foundation/XIP/blob/main/xip-13-1.md"
)

# Project accounting guidance identifies these three $15k receipts as final
# carrier settlements under the historical payment cap, not partial payments.
# Scope the override narrowly to the affected service months; do not extrapolate
# the old cap after June 2026 because July settled at its full projected amount.
HISTORICAL_FINAL_CAPPED_SETTLEMENTS = {
    "2026-04": Decimal("15000.00"),
    "2026-05": Decimal("15000.00"),
    "2026-06": Decimal("15000.00"),
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


def shift_month(month, delta):
    year, month_num = map(int, month[:7].split("-"))
    absolute = year * 12 + (month_num - 1) + delta
    shifted_year, shifted_zero_month = divmod(absolute, 12)
    return f"{shifted_year:04d}-{shifted_zero_month + 1:02d}"


def ordinary_policy_split(amount, day):
    """Split ordinary retained revenue exactly to cents under XIP policy."""
    amount = Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)
    operations = (
        amount * Decimal("0.20")
    ).quantize(CENT, rounding=ROUND_HALF_UP)

    if day >= XIP_12_EFFECTIVE_DATE:
        liquidity = (
            amount * Decimal("0.20")
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        holders = amount - operations - liquidity
    else:
        liquidity = Decimal("0")
        holders = amount - operations

    if min(holders, operations, liquidity) < 0:
        raise RuntimeError(
            f"Invalid ordinary policy split for {day}: {amount}"
        )

    if holders + operations + liquidity != amount:
        raise RuntimeError(
            f"Ordinary policy split does not reconcile for {day}"
        )

    return holders, operations, liquidity


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


def apply_short_outage_fallback(points):
    """Impute only a short trailing API outage, never internal missing days."""
    if not points:
        raise RuntimeError("Cannot build outage fallback without measured offload")

    measured_dates = sorted(points)
    last_measured = date.fromisoformat(measured_dates[-1])
    latest_completed = datetime.now(timezone.utc).date() - timedelta(days=1)
    gap_days = (latest_completed - last_measured).days

    result = dict(points)
    imputed_dates = set()
    metadata = {
        "method": "trailing_measured_7d_average",
        "lookback_days": OUTAGE_FALLBACK_LOOKBACK_DAYS,
        "max_fallback_days": MAX_OUTAGE_FALLBACK_DAYS,
        "active": False,
        "last_measured_date": last_measured.isoformat(),
        "latest_completed_date": latest_completed.isoformat(),
        "imputed_dates": [],
        "imputed_daily_offload_gb": None,
    }

    if gap_days <= 0:
        return result, metadata, imputed_dates

    # Fail closed if the source is stale for too long. A short operational
    # outage can be projected and later reconciled; an open-ended outage
    # should not silently turn into an indefinite synthetic series.
    if gap_days > MAX_OUTAGE_FALLBACK_DAYS:
        metadata["reason"] = (
            "Source gap exceeds the short-outage fallback limit; no "
            "additional days were imputed."
        )
        return result, metadata, imputed_dates

    trailing_days = measured_dates[-OUTAGE_FALLBACK_LOOKBACK_DAYS:]
    trailing_values = [points[day] for day in trailing_days]

    if not trailing_values:
        return result, metadata, imputed_dates

    fallback_gb = (
        sum(trailing_values, Decimal("0"))
        / Decimal(len(trailing_values))
    )

    for offset in range(1, gap_days + 1):
        day = (last_measured + timedelta(days=offset)).isoformat()
        result[day] = fallback_gb
        imputed_dates.add(day)

    metadata.update(
        {
            "active": True,
            "imputed_dates": sorted(imputed_dates),
            "imputed_daily_offload_gb": float(
                fallback_gb.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                )
            ),
            "reason": (
                "The upstream daily offload feed is temporarily stale. "
                "Missing trailing completed days are provisionally estimated "
                "from the average of the latest measured seven days and are "
                "replaced when measured observations resume."
            ),
        }
    )

    return result, metadata, imputed_dates


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

    offload_source, measured_offload = load_offload()
    offload, outage_fallback, imputed_dates = apply_short_outage_fallback(
        measured_offload
    )
    rows = source["data"]

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
    final_settled_by_service = {}
    settlements = []
    unattributed = []
    recognized = []

    for payment in payments:
        if payment["payment_date"] is None:
            unattributed.append(
                {
                    "source_month": payment["source_month"][:7],
                    "payment_received_usd": money(payment["amount"]),
                    "payment_received_date": None,
                    "reason": (
                        "Source reports a payment amount without a payment "
                        "date. It remains unattributed rather than relying on "
                        "a manual historical override."
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

        lag_fallback = None
        if len(candidates) != 1:
            source_month = payment["source_month"][:7]
            target_service_month = shift_month(source_month, -2)
            target_key = month_start(target_service_month)
            target_service = next(
                (
                    service
                    for service in services
                    if service["month"] == target_key
                ),
                None,
            )

            if target_service is not None:
                already_recognized = recognized_by_service.get(
                    target_key, Decimal("0")
                )
                expected_remaining = (
                    target_service["amount"] - already_recognized
                )

                if (
                    expected_remaining > TOLERANCE
                    and payment["amount"] > TOLERANCE
                ):
                    historical_cap = (
                        HISTORICAL_FINAL_CAPPED_SETTLEMENTS.get(
                            target_service_month
                        )
                    )

                    if (
                        historical_cap is not None
                        and abs(payment["amount"] - historical_cap)
                        <= TOLERANCE
                    ):
                        # These three receipts were final settlements under
                        # the old carrier payment cap. The earlier higher
                        # projections are superseded, not left receivable.
                        lag_fallback = {
                            "month": target_key,
                            "amount": payment["amount"],
                            "expected_remaining": expected_remaining,
                            "remaining_after": Decimal("0"),
                            "method": (
                                "two_month_lag_final_historical_cap"
                            ),
                            "overage_pct": Decimal("0"),
                            "projection_write_down": max(
                                Decimal("0"),
                                expected_remaining - payment["amount"],
                            ),
                        }
                    elif payment["amount"] <= expected_remaining + TOLERANCE:
                        remaining_after = max(
                            Decimal("0"),
                            expected_remaining - payment["amount"],
                        )
                        lag_fallback = {
                            "month": target_key,
                            "amount": payment["amount"],
                            "expected_remaining": expected_remaining,
                            "remaining_after": remaining_after,
                            "method": "two_month_lag_partial",
                            "overage_pct": Decimal("0"),
                        }
                    else:
                        overage_pct = (
                            (payment["amount"] - expected_remaining)
                            / expected_remaining
                            * Decimal("100")
                        )

                        if overage_pct <= MAX_LAG_FALLBACK_VARIANCE_PCT:
                            lag_fallback = {
                                "month": target_key,
                                "amount": payment["amount"],
                                "expected_remaining": expected_remaining,
                                "remaining_after": Decimal("0"),
                                "method": "two_month_lag_bounded_variance",
                                "overage_pct": overage_pct,
                            }

            if lag_fallback is None:
                unattributed.append(
                    {
                        "source_month": payment["source_month"][:7],
                        "payment_received_usd": money(payment["amount"]),
                        "payment_received_date": payment["payment_date"],
                        "reason": (
                            "No unique exact contiguous service-period "
                            f"reconciliation and no bounded two-month-lag "
                            f"fallback. Candidate count: {len(candidates)}."
                        ),
                    }
                )
                continue

            candidates = [[
                {
                    "month": lag_fallback["month"],
                    "amount": lag_fallback["amount"],
                }
            ]]

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
            "reconciliation_method": (
                lag_fallback["method"]
                if lag_fallback is not None
                else "exact_contiguous_sum"
            ),
            "difference_usd": 0.0,
        }

        if lag_fallback is not None:
            method = lag_fallback["method"]

            if method == "two_month_lag_partial":
                settlement["attribution_note"] = (
                    "No unique exact amount match was available. The payment "
                    "is treated as a partial settlement of the service month "
                    "two calendar months before the source/payment month. "
                    "Only the amount actually received is marked confirmed; "
                    "the remaining projected balance stays unsettled."
                )
            elif method == "two_month_lag_final_historical_cap":
                settlement["attribution_note"] = (
                    "This is a final $15,000 carrier settlement under the "
                    "historical carrier payment cap. It reconciles the service "
                    "month two calendar months earlier and supersedes the "
                    "higher provisional projection; the projection difference "
                    "is written down rather than left outstanding."
                )
                settlement["projection_reconciled_down_usd"] = money(
                    lag_fallback["projection_write_down"]
                )
            else:
                settlement["attribution_note"] = (
                    "No unique exact amount match was available. The payment "
                    "is attributed to the service month two calendar months "
                    "before the source/payment month. A final amount above the "
                    "earlier projection is accepted only because the overage "
                    f"is within the {MAX_LAG_FALLBACK_VARIANCE_PCT}% "
                    "automatic-reconciliation bound."
                )

            settlement["projection_remaining_before_usd"] = money(
                lag_fallback["expected_remaining"]
            )
            settlement["projection_remaining_after_usd"] = money(
                lag_fallback["remaining_after"]
            )
            settlement["projection_overage_pct"] = float(
                lag_fallback["overage_pct"].quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                )
            )
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

        if (
            lag_fallback is not None
            and lag_fallback["method"]
            == "two_month_lag_final_historical_cap"
        ):
            final_settled_by_service[
                lag_fallback["month"]
            ] = payment["amount"]

        settlement["difference_usd"] = (
            0.0
            if lag_fallback is not None
            else money(matched_total - payment["amount"])
        )
        settlements.append(settlement)

    # Fiat payouts are posted in the source sheet after the carrier settlement
    # that funds them. Bind each fiat row to the immediately preceding carrier
    # source month, then inherit the service month from that settlement's
    # reconciliation. This keeps the attribution stable when later carrier
    # payments arrive and fails closed if the expected settlement is missing.
    fiat_operator_transfers = []
    for row in rows:
        payout = D(row.get("transferred_to_fiat_operators_usd"))
        if payout is None or payout <= TOLERANCE:
            continue

        source_month = row["month"][:7]
        carrier_source_month = shift_month(
            source_month,
            -FIAT_CARRIER_SOURCE_OFFSET_MONTHS,
        )
        carrier_settlements = [
            settlement
            for settlement in settlements
            if settlement["source_sheet_column"] == carrier_source_month
        ]

        if len(carrier_settlements) != 1:
            raise RuntimeError(
                "Expected exactly one carrier settlement for fiat payout "
                f"{source_month} via carrier source month "
                f"{carrier_source_month}; found {len(carrier_settlements)}"
            )

        carrier_settlement = carrier_settlements[0]
        service_months = carrier_settlement.get("service_months") or []
        if len(service_months) != 1:
            raise RuntimeError(
                "Fiat payout requires one reconciled carrier service month: "
                f"{carrier_settlement['settlement_id']} has "
                f"{len(service_months)}"
            )

        service_month = service_months[0]["service_month"]

        gross = (
            payout / FIAT_OPERATOR_SHARE
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        bbb = (
            gross * FIAT_BBB_SHARE
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        operations = gross - payout - bbb

        if operations < 0:
            raise RuntimeError(
                "Invalid fiat-deployer allocation: negative operations share"
            )

        fiat_operator_transfers.append(
            {
                "source_month": source_month,
                "carrier_settlement_source_month": carrier_source_month,
                "carrier_settlement_id": carrier_settlement["settlement_id"],
                "carrier_payment_received_date":
                    carrier_settlement["payment_received_date"],
                "service_month": service_month,
                "operator_payout_usd": money(payout),
                "amount_usd": money(payout),
                "gross_fiat_allocation_usd": money(gross),
                "bbb_allocation_usd": money(bbb),
                "operations_allocation_usd": money(operations),
                "operator_share_pct": 75.0,
                "bbb_share_pct": 5.0,
                "operations_share_pct": 20.0,
                "transfer_date": None,
                "attribution_basis": (
                    "xip_13_1_carrier_settlement_reconciliation"
                ),
                "status": (
                    "service_month_inherited_from_carrier_settlement"
                ),
                "policy_source": {
                    "xip": "XIP-13.1",
                    "status": "Passed in official XIP index",
                    "url": XIP_13_1_URL,
                    "index_url": XIP_INDEX_URL,
                },
                "token_emissions_treatment": (
                    "XIP-13.1 assigns the corresponding fiat-option token "
                    "emissions to the Burn facility. The USD revenue sheet "
                    "does not identify a token amount, so no token quantity "
                    "is inferred in this feed."
                ),
            }
        )

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

        if projected is None or not complete_month(measured_offload, service_month):
            continue

        daily = month_points(measured_offload, service_month)
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

        final_settlement = final_settled_by_service.get(
            row["month"]
        )

        if final_settlement is not None:
            target = final_settlement
            basis = "confirmed_settlement"
        elif projected is not None and recognized_amount >= projected - TOLERANCE:
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
                        "offload_basis": (
                            "imputed_trailing_7d_average"
                            if item["date"] in imputed_dates
                            else "measured"
                        ),
                        "fees_usd": money(amount),
                        "user_fees_usd": money(amount),
                        "basis": basis,
                    }
                )

            monthly_accrual.append(
                {
                    "service_month": service_month,
                    "basis": basis,
                    "daily_shape": (
                        "measured_network_offload_with_short_outage_imputation"
                        if any(
                            item["date"] in imputed_dates
                            for item in daily
                        )
                        else "measured_network_offload"
                    ),
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
                    "offload_basis": (
                        "imputed_trailing_7d_average"
                        if item["date"] in imputed_dates
                        else "measured"
                    ),
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
                "daily_shape": (
                    "measured_network_offload_with_short_outage_imputation"
                    if any(
                        item["date"] in imputed_dates
                        for item in daily
                    )
                    else "measured_network_offload"
                ),
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

    processed_months = {
        row["service_month"] for row in monthly_accrual
    }
    future_offload_months = sorted(
        {
            day[:7]
            for day in offload
            if day[:7] not in processed_months
            and month_index(day[:7])
            > month_index(latest_calibration["service_month"])
        }
    )

    for service_month in future_offload_months:
        daily = month_points(offload, service_month)
        if not daily:
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
                    "offload_basis": (
                        "imputed_trailing_7d_average"
                        if item["date"] in imputed_dates
                        else "measured"
                    ),
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
                "daily_shape": (
                    "measured_network_offload_with_short_outage_imputation"
                    if any(
                        item["date"] in imputed_dates
                        for item in daily
                    )
                    else "measured_network_offload"
                ),
                "offload_days": len(daily),
                "api_offload_gb": float(
                    sum(
                        (item["gigabytes"] for item in daily),
                        Decimal("0"),
                    )
                ),
                "accrual_total_usd": money(live_total),
                "official_projected_revenue_usd": None,
                "settlement_confirmed_usd": 0.0,
                "rate_usd_per_api_gb": float(
                    live_rate.quantize(
                        Decimal("0.000001"),
                        rounding=ROUND_HALF_UP,
                    )
                ),
                "rate_source_month": latest_calibration["service_month"],
            }
        )

    monthly_accrual.sort(key=lambda row: row["service_month"])
    daily_data.sort(key=lambda row: row["date"])

    # --------------------------------------------------
    # Revenue split and fiat-deployer accounting
    # --------------------------------------------------
    #
    # Fees are gross carrier WiFi offload fees. Ordinary fees follow the
    # applicable XNET allocation policy. XIP-13.1 creates a fiat-operator
    # exception: 75% to the operator, 5% facilitation/BBB, and 20% operations.
    # XIP-13.1 uses the carrier settlement timeline. Fiat payouts inherit the
    # service month from the reconciled carrier settlement that funds them.

    for row in daily_data:
        fee = Decimal(str(row["fees_usd"]))
        (
            ordinary_holders,
            ordinary_operations,
            ordinary_liquidity,
        ) = ordinary_policy_split(fee, row["date"])
        ordinary_protocol = (
            ordinary_operations + ordinary_liquidity
        )

        row["fiat_gross_allocation_usd"] = 0.0
        row["fiat_operator_payout_usd"] = 0.0
        row["fiat_bbb_allocation_usd"] = 0.0
        row["fiat_operations_allocation_usd"] = 0.0
        row["ordinary_holders_revenue_usd"] = money(ordinary_holders)
        row["ordinary_operations_revenue_usd"] = money(
            ordinary_operations
        )
        row["ordinary_protocol_owned_liquidity_usd"] = money(
            ordinary_liquidity
        )
        row["ordinary_protocol_revenue_usd"] = money(ordinary_protocol)
        row["supply_side_revenue_usd"] = 0.0
        row["holders_revenue_usd"] = money(ordinary_holders)
        row["protocol_revenue_usd"] = money(ordinary_protocol)
        row["revenue_usd"] = money(fee)
        row["fiat_allocation_basis"] = None

    fiat_by_service_month = {}
    for transfer in fiat_operator_transfers:
        service_month = transfer["service_month"]
        bucket = fiat_by_service_month.setdefault(
            service_month,
            {
                "operator_payout": Decimal("0"),
                "bbb": Decimal("0"),
                "operations": Decimal("0"),
            },
        )
        bucket["operator_payout"] += Decimal(
            str(transfer["operator_payout_usd"])
        )
        bucket["bbb"] += Decimal(
            str(transfer["bbb_allocation_usd"])
        )
        bucket["operations"] += Decimal(
            str(transfer["operations_allocation_usd"])
        )

    for service_month, bucket in sorted(fiat_by_service_month.items()):
        matching = [
            row
            for row in daily_data
            if row["service_month"] == service_month
        ]

        if not matching:
            for transfer in fiat_operator_transfers:
                if transfer["service_month"] == service_month:
                    transfer["daily_allocation_status"] = (
                        "pending_no_daily_rows"
                    )
            continue

        weights = [
            {"date": row["date"], "gigabytes": row["offload_gb"]}
            for row in matching
        ]
        operator_daily = allocate_cents(bucket["operator_payout"], weights)
        bbb_daily = allocate_cents(bucket["bbb"], weights)
        operations_daily = allocate_cents(bucket["operations"], weights)

        month_fee_total = sum(
            (Decimal(str(row["fees_usd"])) for row in matching),
            Decimal("0"),
        )
        month_gross_fiat = (
            bucket["operator_payout"]
            + bucket["bbb"]
            + bucket["operations"]
        )
        if month_gross_fiat > month_fee_total + TOLERANCE:
            raise RuntimeError(
                "Fiat-deployer gross allocation exceeds fees for "
                f"{service_month}: gross={month_gross_fiat}, "
                f"fees={month_fee_total}"
            )

        for row, operator, bbb, operations in zip(
            matching,
            operator_daily,
            bbb_daily,
            operations_daily,
        ):
            fee = Decimal(str(row["fees_usd"]))
            fiat_gross = operator + bbb + operations
            non_fiat_fee = fee - fiat_gross

            if non_fiat_fee < -TOLERANCE:
                raise RuntimeError(
                    "Daily fiat allocation exceeds daily fees on "
                    f"{row['date']}"
                )
            if non_fiat_fee < 0:
                non_fiat_fee = Decimal("0")

            (
                ordinary_holders,
                ordinary_operations,
                ordinary_liquidity,
            ) = ordinary_policy_split(
                non_fiat_fee,
                row["date"],
            )
            ordinary_protocol = (
                ordinary_operations + ordinary_liquidity
            )

            holders = ordinary_holders + bbb
            protocol = ordinary_protocol + operations
            supply = operator
            revenue = fee - supply

            if abs(fee - revenue - supply) > TOLERANCE:
                raise RuntimeError(
                    "Daily Fees != Revenue + SupplySideRevenue on "
                    f"{row['date']}"
                )
            if abs(revenue - holders - protocol) > TOLERANCE:
                raise RuntimeError(
                    "Daily Revenue != HoldersRevenue + ProtocolRevenue on "
                    f"{row['date']}"
                )

            row["fiat_gross_allocation_usd"] = money(fiat_gross)
            row["fiat_operator_payout_usd"] = money(operator)
            row["fiat_bbb_allocation_usd"] = money(bbb)
            row["fiat_operations_allocation_usd"] = money(operations)
            row["ordinary_holders_revenue_usd"] = money(ordinary_holders)
            row["ordinary_operations_revenue_usd"] = money(
                ordinary_operations
            )
            row["ordinary_protocol_owned_liquidity_usd"] = money(
                ordinary_liquidity
            )
            row["ordinary_protocol_revenue_usd"] = money(ordinary_protocol)
            row["supply_side_revenue_usd"] = money(supply)
            row["holders_revenue_usd"] = money(holders)
            row["protocol_revenue_usd"] = money(protocol)
            row["revenue_usd"] = money(revenue)
            row["fiat_allocation_basis"] = (
                "xip_13_1_carrier_settlement_reconciliation"
            )

        for transfer in fiat_operator_transfers:
            if transfer["service_month"] == service_month:
                transfer["daily_allocation_status"] = (
                    "distributed_over_reconciled_service_offload"
                )
                transfer["daily_allocation_first_date"] = matching[0]["date"]
                transfer["daily_allocation_last_date"] = matching[-1]["date"]

    for month in monthly_accrual:
        rows_for_month = [
            row
            for row in daily_data
            if row["service_month"] == month["service_month"]
        ]
        if not rows_for_month:
            continue

        for field in (
            "fiat_gross_allocation_usd",
            "fiat_operator_payout_usd",
            "fiat_bbb_allocation_usd",
            "fiat_operations_allocation_usd",
            "ordinary_holders_revenue_usd",
            "ordinary_operations_revenue_usd",
            "ordinary_protocol_owned_liquidity_usd",
            "ordinary_protocol_revenue_usd",
            "supply_side_revenue_usd",
            "revenue_usd",
            "holders_revenue_usd",
            "protocol_revenue_usd",
        ):
            month[field] = money(
                sum(
                    (
                        Decimal(str(row[field]))
                        for row in rows_for_month
                    ),
                    Decimal("0"),
                )
            )

        month["fiat_allocation_basis"] = (
            "xip_13_1_carrier_settlement_reconciliation"
            if month["fiat_gross_allocation_usd"] > 0
            else None
        )

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
        if service["month"] in final_settled_by_service:
            continue

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
    daily_revenue_total = sum(
        (Decimal(str(row["revenue_usd"])) for row in daily_data),
        Decimal("0"),
    )
    daily_supply_side_total = sum(
        (
            Decimal(str(row["supply_side_revenue_usd"]))
            for row in daily_data
        ),
        Decimal("0"),
    )
    daily_holders_total = sum(
        (
            Decimal(str(row["holders_revenue_usd"]))
            for row in daily_data
        ),
        Decimal("0"),
    )
    daily_protocol_total = sum(
        (
            Decimal(str(row["protocol_revenue_usd"]))
            for row in daily_data
        ),
        Decimal("0"),
    )
    fiat_operator_payout_total = sum(
        (
            Decimal(str(row["operator_payout_usd"]))
            for row in fiat_operator_transfers
        ),
        Decimal("0"),
    )
    fiat_gross_total = sum(
        (
            Decimal(str(row["gross_fiat_allocation_usd"]))
            for row in fiat_operator_transfers
        ),
        Decimal("0"),
    )
    fiat_bbb_total = sum(
        (
            Decimal(str(row["bbb_allocation_usd"]))
            for row in fiat_operator_transfers
        ),
        Decimal("0"),
    )
    fiat_operations_total = sum(
        (
            Decimal(str(row["operations_allocation_usd"]))
            for row in fiat_operator_transfers
        ),
        Decimal("0"),
    )

    if abs(daily_total - daily_revenue_total - daily_supply_side_total) > TOLERANCE:
        raise RuntimeError(
            "Daily aggregate Fees != Revenue + SupplySideRevenue"
        )
    if abs(daily_revenue_total - daily_holders_total - daily_protocol_total) > TOLERANCE:
        raise RuntimeError(
            "Daily aggregate Revenue != HoldersRevenue + ProtocolRevenue"
        )
    fully_settled_daily_total = sum(
        (
            Decimal(str(row["fees_usd"]))
            for row in daily_data
            if row["basis"] == "confirmed_settlement"
        ),
        Decimal("0"),
    )
    partial_confirmed_total = sum(
        (
            recognized_by_service.get(
                month_start(row["service_month"]),
                Decimal("0"),
            )
            for row in monthly_accrual
            if row["basis"] == "official_projection_partially_confirmed"
        ),
        Decimal("0"),
    )
    unconfirmed_accrual_component = daily_total - recognized_total

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
        "schema_version": 4,
        "accounting_basis": "hybrid_accrual_reconciled",
        "source": source["source"],
        "source_revision_policy": {
            "mode": "source_faithful_restatement",
            "publisher": "XNET team revenue sheet",
            "treatment": (
                "This community analytics feed reflects the team's currently "
                "published accounting. If the source revenue sheet revises a "
                "historical month, the revised value is intentionally carried "
                "forward on the next rebuild and affected daily accrual and "
                "settlement history are recomputed. This is a source mirror, "
                "not an independent restatement by the community."
            ),
        },
        "policy_sources": {
            "xip_index": XIP_INDEX_URL,
            "xip_12": {
                "status": "Passed in official XIP index",
                "url": XIP_12_URL,
                "allocation": (
                    "60% Buy & Burn, 20% protocol-owned liquidity, "
                    "20% operations"
                ),
            },
            "xip_13_1": {
                "status": "Passed in official XIP index",
                "url": XIP_13_1_URL,
                "fiat_operator_share_pct": 75.0,
                "facilitation_bbb_share_pct": 5.0,
                "operations_share_pct": 20.0,
                "settlement_timing": "monthly in arrears on NET60+ timeline",
            },
        },
        "historical_settlement_policy": {
            "source": "project accounting guidance",
            "final_carrier_cap_usd": 15000.0,
            "service_months": sorted(
                HISTORICAL_FINAL_CAPPED_SETTLEMENTS
            ),
            "treatment": (
                "The April-June 2026 $15,000 carrier settlements are final "
                "under the historical carrier cap. Their earlier higher "
                "projections are reconciled down to actual settlement rather "
                "than left as outstanding receivables. The cap is not "
                "extrapolated beyond those explicitly scoped months."
            ),
        },
        "offload_source": {
            "name": offload_source["source"]["name"],
            "url": offload_source["source"]["url"],
            "first_date": offload_source["first_date"],
            "last_measured_date": offload_source["last_date"],
            "accrual_last_date": max(offload),
            "count": offload_source["count"],
        },
        "methodology": {
            "fees": (
                "Carriers pay XNET for mobile data offloaded onto WiFi. Until "
                "carrier payment arrives, Fees are conservatively estimated "
                "from daily offload. Payments typically arrive about two "
                "months later, and historical estimates are then reconciled "
                "to the amount actually paid."
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
                "values are recomputed. Exact amount reconciliation is "
                "preferred. If no unique exact match exists, the observed "
                "two-month payment cadence may be used. April-June 2026 are "
                "a narrowly scoped historical exception: each later $15,000 "
                "receipt was the final settlement under the old carrier cap, "
                "so the earlier higher projection is written down to the "
                "actual $15,000 rather than left as an outstanding balance. "
                "Outside that explicit cap period, a below-projection receipt "
                "remains partial; a final payment above projection is accepted "
                "automatically only within the 15% bounded-variance rule. "
                "Confirmed amounts replace provisional accrual; they are never "
                "added on top."
            ),
            "partial_settlements": (
                "Outside the explicitly scoped historical $15,000 cap, a "
                "partial carrier payment confirms only the amount actually "
                "received for its reconciled service month and is not added "
                "on top of that month's provisional accrual. The remaining "
                "official projected balance stays unsettled. Source revisions "
                "are respected on each rebuild."
            ),
            "missing_offload": (
                "Missing offload observations are never filled with zero. If "
                "the upstream API is temporarily stale by no more than 14 "
                "completed days, trailing missing days are provisionally "
                "estimated using the average of the latest seven measured "
                "offload days. Those imputed days are explicitly flagged and "
                "are replaced when measured observations resume. If the gap "
                "exceeds 14 days, the model fails closed and stops extending "
                "the synthetic series."
            ),
            "revenue": (
                "Fees minus payments to deployers who choose fiat "
                "compensation. When no fiat-deployer payout applies, Revenue "
                "equals Fees."
            ),
            "supply_side_revenue": (
                "Payments to deployers who choose fiat compensation for "
                "carrying mobile traffic. For the fiat option, 75% of the "
                "gross fiat allocation is paid to the deployer."
            ),
            "fiat_deployer_option": (
                "The official XIP index lists XIP-13.1 as Passed. The "
                "ratified mechanism lets designated operators choose fiat in "
                "lieu of token distributions. Its 80% BuyBack Revenue "
                "Percentage less a 5% facilitation fee produces a 75% "
                "operator cash share; for DeFiLlama accounting that fiat "
                "slice is 75% Supply-Side Revenue, 5% BBB/Holders Revenue and "
                "20% operations/Protocol Revenue. XIP-13.1 states that fiat "
                "operators are paid monthly in arrears on the NET60+ timeline. "
                "A fiat payout therefore inherits the service month from the "
                "carrier settlement that funds it; the allocation is then "
                "shaped across that reconciled service month's offload. Assigned "
                "fiat-option token emissions go to the Burn facility, but no "
                "token quantity is inferred from the USD sheet."
            ),
            "holders_revenue": (
                "Holder Revenue is accrued in the same service period as Fees "
                "so 24h/7d/30d comparisons remain economically comparable. "
                "Before XIP-12, the ordinary holder allocation is 80%; from "
                "XIP-12 onward it is 60%. For the XIP-13.1 fiat slice, 5% is "
                "the BBB/facilitation allocation. Values remain provisional "
                "where underlying Fees are provisional and are reconciled "
                "with those Fees when settlement arrives. This is accrual "
                "attribution, not a claim that the on-chain buyback or burn "
                "executed on the same calendar day."
            ),
            "protocol_revenue": (
                "Protocol Revenue is the portion retained within XNET after "
                "Supply-Side Revenue. Before XIP-12 the ordinary retained "
                "share is 20%; from XIP-12 onward it is 40%, comprising 20% "
                "operations and 20% protocol-owned liquidity. For the "
                "XIP-13.1 fiat slice, 20% is retained for XNET operations."
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
            "outage_fallback": outage_fallback,
        },
        "totals": {
            "source_payments_received_usd": money(source_received_total),
            "recognized_service_revenue_usd": money(recognized_total),
            "unattributed_payments_usd": money(unattributed_total),
            "defillama_daily_accrual_usd": money(daily_total),
            "defillama_daily_revenue_usd": money(daily_revenue_total),
            "defillama_daily_supply_side_revenue_usd": money(
                daily_supply_side_total
            ),
            "defillama_daily_holders_revenue_usd": money(
                daily_holders_total
            ),
            "defillama_daily_protocol_revenue_usd": money(
                daily_protocol_total
            ),
            "fiat_operator_payout_usd": money(
                fiat_operator_payout_total
            ),
            "fiat_gross_allocation_usd": money(
                fiat_gross_total
            ),
            "fiat_bbb_allocation_usd": money(
                fiat_bbb_total
            ),
            "fiat_operations_allocation_usd": money(
                fiat_operations_total
            ),
            "fully_settled_daily_accrual_usd": money(
                fully_settled_daily_total
            ),
            "partially_confirmed_service_revenue_usd": money(
                partial_confirmed_total
            ),
            "unconfirmed_accrual_component_usd": money(
                unconfirmed_accrual_component
            ),
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
