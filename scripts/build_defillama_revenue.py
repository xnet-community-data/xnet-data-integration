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

    fiat_operator_transfers = []
    for row in rows:
        payout = D(row.get("transferred_to_fiat_operators_usd"))
        if payout is None or payout <= TOLERANCE:
            continue

        gross = (
            payout / FIAT_OPERATOR_SHARE
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        bbb = (
            gross * FIAT_BBB_SHARE
        ).quantize(CENT, rounding=ROUND_HALF_UP)

        # Derive operations as the exact cent-level residual so that:
        # gross = operator payout + BBB + operations.
        operations = gross - payout - bbb

        if operations < 0:
            raise RuntimeError(
                "Invalid fiat-deployer allocation: negative operations share"
            )

        fiat_operator_transfers.append(
            {
                "source_month": row["month"][:7],
                "service_month": None,
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
                    "source_month_pending_service_period_attribution"
                ),
                "status": "reported_source_month_only",
                "token_emissions_treatment": (
                    "Corresponding fiat-option token emissions are burned; "
                    "the USD source does not identify a token amount, so no "
                    "token quantity is inferred here."
                ),
            }
        )

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
                    # A payment below the remaining projection is a valid
                    # partial settlement under the established two-month lag.
                    # Do not reject it merely because the projection has not
                    # yet been paid in full.
                    if payment["amount"] <= expected_remaining + TOLERANCE:
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
                        # A final settlement is allowed to exceed the earlier
                        # projection only within a conservative bound. Larger
                        # overages remain unattributed for manual review.
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
            if lag_fallback["method"] == "two_month_lag_partial":
                settlement["attribution_note"] = (
                    "No unique exact amount match was available. The payment "
                    "is treated as a partial settlement of the service month "
                    "two calendar months before the source/payment month. "
                    "Only the amount actually received is marked confirmed; "
                    "the remaining projected balance stays unsettled."
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

        settlement["difference_usd"] = (
            0.0
            if lag_fallback is not None
            else money(matched_total - payment["amount"])
        )
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
    # Fees are the gross carrier service fees. Most fees follow XNET's normal
    # policy allocation. A fiat-deployer allocation is different: 75% is paid
    # to the deployer (supply-side revenue), 5% goes to BBB, and 20% goes to
    # XNET operations. Because the current public sheet reports the fiat payout
    # by source month but not by underlying service period, the split is booked
    # provisionally to that source month and will be backfilled if a service
    # period is later supplied.

    for row in daily_data:
        fee = Decimal(str(row["fees_usd"]))
        holder_share = (
            Decimal("0.60")
            if row["date"] >= "2025-05-22"
            else Decimal("0.80")
        )

        holders = (
            fee * holder_share
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        protocol = fee - holders

        row["fiat_gross_allocation_usd"] = 0.0
        row["supply_side_revenue_usd"] = 0.0
        row["holders_revenue_usd"] = money(holders)
        row["protocol_revenue_usd"] = money(protocol)
        row["revenue_usd"] = money(fee)
        row["fiat_allocation_basis"] = None

    fiat_by_month = {}
    for transfer in fiat_operator_transfers:
        source_month = transfer["source_month"]
        bucket = fiat_by_month.setdefault(
            source_month,
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

    for source_month, bucket in sorted(fiat_by_month.items()):
        matching = [
            row
            for row in daily_data
            if row["service_month"] == source_month
        ]

        if not matching:
            for transfer in fiat_operator_transfers:
                if transfer["source_month"] == source_month:
                    transfer["daily_allocation_status"] = (
                        "pending_no_daily_rows"
                    )
            continue

        weights = [
            {
                "date": row["date"],
                "gigabytes": row["offload_gb"],
            }
            for row in matching
        ]

        operator_daily = allocate_cents(
            bucket["operator_payout"],
            weights,
        )
        bbb_daily = allocate_cents(
            bucket["bbb"],
            weights,
        )
        operations_daily = allocate_cents(
            bucket["operations"],
            weights,
        )

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
                f"{source_month}: gross={month_gross_fiat}, "
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

            holder_share = (
                Decimal("0.60")
                if row["date"] >= "2025-05-22"
                else Decimal("0.80")
            )
            holders_non_fiat = (
                non_fiat_fee * holder_share
            ).quantize(CENT, rounding=ROUND_HALF_UP)
            protocol_non_fiat = non_fiat_fee - holders_non_fiat

            holders = holders_non_fiat + bbb
            protocol = protocol_non_fiat + operations
            supply = operator
            revenue = holders + protocol

            if abs(fee - revenue - supply) > TOLERANCE:
                raise RuntimeError(
                    "Daily Fees != Revenue + SupplySideRevenue on "
                    f"{row['date']}"
                )

            row["fiat_gross_allocation_usd"] = money(fiat_gross)
            row["supply_side_revenue_usd"] = money(supply)
            row["holders_revenue_usd"] = money(holders)
            row["protocol_revenue_usd"] = money(protocol)
            row["revenue_usd"] = money(revenue)
            row["fiat_allocation_basis"] = (
                "source_month_pending_service_period_attribution"
            )

        for transfer in fiat_operator_transfers:
            if transfer["source_month"] == source_month:
                transfer["daily_allocation_status"] = (
                    "provisionally_distributed_over_source_month_offload"
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
            "source_month_pending_service_period_attribution"
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
        "schema_version": 3,
        "accounting_basis": "hybrid_accrual_reconciled",
        "source": source["source"],
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
                "values are recomputed. Exact amount reconciliation is "
                "preferred. If no unique exact match exists, the established "
                "two-month payment cadence may be used: payments below the "
                "remaining official projection are recorded as partial "
                "settlements, while a final payment above the projection is "
                "accepted automatically only when the overage is within 15%. "
                "Anything outside those rules remains unattributed for review. "
                "Confirmed amounts never get added on top of provisional "
                "revenue; they replace confirmation status and, once a service "
                "month is fully settled, its daily series is rescaled to the "
                "actual confirmed total in proportion to daily offload."
            ),
            "partial_settlements": (
                "A partial carrier payment confirms only the amount actually "
                "received for its reconciled service month and is not added "
                "on top of that month's provisional accrual. The remaining "
                "official projected balance stays unsettled. Source revisions "
                "are respected on each rebuild, so a later dated payment can "
                "supersede an earlier provisional or ambiguous source entry."
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
                "Fees retained within the XNET ecosystem after payments to "
                "deployers who choose fiat compensation. When no fiat-deployer "
                "payout applies, Revenue equals Fees."
            ),
            "supply_side_revenue": (
                "Payments to deployers who choose fiat compensation for "
                "carrying mobile traffic. For the fiat option, 75% of the "
                "gross fiat allocation is paid to the deployer."
            ),
            "fiat_deployer_option": (
                "For deployers who choose fiat, the corresponding token "
                "emissions are burned and the gross fiat allocation is split "
                "75% to the deployer, 5% to BBB, and 20% to XNET operations. "
                "The public sheet currently reports the fiat payout by source "
                "month but not the underlying service period, so the daily "
                "split is provisionally shaped across that source month's "
                "offload and will be backfilled if service-period attribution "
                "is later provided."
            ),
            "holders_revenue": (
                "For ordinary carrier revenue, the holder allocation is 80% "
                "before 2025-05-22 and 60% from XIP-12 onward. For the "
                "fiat-deployer slice, 5% of the gross fiat allocation goes "
                "to BBB."
            ),
            "protocol_revenue": (
                "For ordinary carrier revenue, Protocol Revenue is 20% before "
                "2025-05-22 and 40% from XIP-12 onward. For the fiat-deployer "
                "slice, 20% of the gross fiat allocation goes to XNET "
                "operations."
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
