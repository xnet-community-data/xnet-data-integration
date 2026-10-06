#!/usr/bin/env python3

from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

MONTHLY_PATH = (
    ROOT / "data/xnet_revenue_monthly.json"
)

DEFI_PATH = (
    ROOT / "data/xnet_defillama_revenue.json"
)

OUT = (
    ROOT / "data/current/xnet_revenue_state.json"
)


def D(v):
    if v in (None, ""):
        return Decimal("0")
    return Decimal(str(v))


def dec(v):
    if v is None:
        return None
    return format(D(v), "f")


def utc_now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def month_key(v):
    return str(v)[:7]


def positive(v):
    return (
        v is not None
        and D(v) > 0
    )


monthly = json.loads(
    MONTHLY_PATH.read_text()
)

defi = json.loads(
    DEFI_PATH.read_text()
)

rows = list(monthly["data"])

today = datetime.now(
    timezone.utc
).date()

current_month = (
    f"{today.year:04d}-"
    f"{today.month:02d}"
)

eligible = [
    r for r in rows
    if month_key(r["month"]) <= current_month
]

if not eligible:
    raise RuntimeError(
        "No revenue rows at or before current month"
    )


# --------------------------------------------------
# Latest actual/projected service month
# --------------------------------------------------

service_rows = [
    r for r in eligible
    if r.get("gb_per_month") is not None
    and r.get(
        "wifi_revenue_projected_usd"
    ) is not None
]

if not service_rows:
    raise RuntimeError(
        "No usable service revenue rows"
    )

latest_service = max(
    service_rows,
    key=lambda r: r["month"],
)


# --------------------------------------------------
# Latest payment
# --------------------------------------------------

payment_rows = [
    r for r in eligible
    if positive(
        r.get(
            "wifi_payment_received_usd"
        )
    )
]

latest_payment = (
    max(
        payment_rows,
        key=lambda r: (
            r.get(
                "wifi_payment_date"
            )
            or r["month"]
        ),
    )
    if payment_rows
    else None
)


# --------------------------------------------------
# Latest BBB transfer
# --------------------------------------------------

bbb_transfer_rows = [
    r for r in eligible
    if positive(
        r.get(
            "transferred_to_buy_burn_usd"
        )
    )
]

latest_bbb_transfer = (
    max(
        bbb_transfer_rows,
        key=lambda r: r["month"],
    )
    if bbb_transfer_rows
    else None
)


# --------------------------------------------------
# Latest fiat/operator transfer
# --------------------------------------------------

fiat_transfers = [
    row
    for row in defi.get("fiat_operator_transfers", [])
    if positive(row.get("operator_payout_usd"))
]

latest_fiat_transfer = (
    max(
        fiat_transfers,
        key=lambda r: (
            r.get("source_month") or "",
            r.get("carrier_payment_received_date") or "",
        ),
    )
    if fiat_transfers
    else None
)

latest_fiat_operator_payout = (
    D(latest_fiat_transfer["operator_payout_usd"])
    if latest_fiat_transfer
    else None
)
latest_fiat_gross_allocation = (
    D(latest_fiat_transfer["gross_fiat_allocation_usd"])
    if latest_fiat_transfer
    else None
)
latest_fiat_bbb_allocation = (
    D(latest_fiat_transfer["bbb_allocation_usd"])
    if latest_fiat_transfer
    else None
)
latest_fiat_operations_allocation = (
    D(latest_fiat_transfer["operations_allocation_usd"])
    if latest_fiat_transfer
    else None
)


# --------------------------------------------------
# Current outstanding balance
# --------------------------------------------------

balance_rows = [
    r for r in eligible
    if r.get(
        "balance_outstanding_to_transfer_usd"
    ) is not None
]

latest_balance = max(
    balance_rows,
    key=lambda r: r["month"],
)


# --------------------------------------------------
# Latest source month carrying ANY meaningful value
# --------------------------------------------------

meaningful_fields = [
    "gb_per_month",
    "wifi_revenue_projected_usd",
    "wifi_payment_received_usd",
    "wifi_payment_date",
    "total_emitted_tokens",
    "projected_buy_burn_usd",
    "transferred_to_buy_burn_usd",
    "transferred_to_fiat_operators_usd",
    "balance_outstanding_to_transfer_usd",
]

meaningful_rows = [
    r for r in eligible
    if any(
        r.get(k) is not None
        for k in meaningful_fields
    )
]

latest_source = max(
    meaningful_rows,
    key=lambda r: r["month"],
)


# --------------------------------------------------
# Cumulative source-sheet values
# --------------------------------------------------

def total(field):
    return sum(
        (
            D(r[field])
            for r in eligible
            if r.get(field) is not None
        ),
        Decimal("0"),
    )


cumulative_projected_revenue = total(
    "wifi_revenue_projected_usd"
)

cumulative_payments = total(
    "wifi_payment_received_usd"
)

cumulative_projected_bbb = total(
    "projected_buy_burn_usd"
)

cumulative_bbb_transfers = total(
    "transferred_to_buy_burn_usd"
)

cumulative_fiat_transfers = total(
    "transferred_to_fiat_operators_usd"
)


# --------------------------------------------------
# Latest run-rate economics
# --------------------------------------------------

service_month = datetime.strptime(
    latest_service["month"][:10],
    "%Y-%m-%d",
)

days = calendar.monthrange(
    service_month.year,
    service_month.month,
)[1]

latest_revenue = D(
    latest_service[
        "wifi_revenue_projected_usd"
    ]
)

latest_gb = D(
    latest_service["gb_per_month"]
)

daily_revenue_run_rate = (
    latest_revenue
    / Decimal(days)
)

revenue_30d_run_rate = (
    daily_revenue_run_rate
    * Decimal("30")
)

annualized_revenue_run_rate = (
    daily_revenue_run_rate
    * Decimal("365")
)

revenue_per_gb = (
    latest_revenue / latest_gb
    if latest_gb
    else None
)


# --------------------------------------------------
# Settled-service-period accounting
# --------------------------------------------------

totals = defi["totals"]

recognized_service_revenue = D(
    totals[
        "recognized_service_revenue_usd"
    ]
)

source_payments_received = D(
    totals[
        "source_payments_received_usd"
    ]
)

unattributed_payments = D(
    totals[
        "unattributed_payments_usd"
    ]
)

fiat_operator_payout = D(
    totals.get(
        "fiat_operator_payout_usd"
    )
) or Decimal("0")

fiat_gross_allocation = D(
    totals.get(
        "fiat_gross_allocation_usd"
    )
) or Decimal("0")

fiat_bbb_allocation = D(
    totals.get(
        "fiat_bbb_allocation_usd"
    )
) or Decimal("0")

fiat_operations_allocation = D(
    totals.get(
        "fiat_operations_allocation_usd"
    )
) or Decimal("0")


# --------------------------------------------------
# Rolling daily fee accrual
# --------------------------------------------------

daily_rows = [
    row
    for row in defi.get("daily_data", [])
    if row.get("date")
]

if not daily_rows:
    raise RuntimeError(
        "DeFiLlama daily accrual feed is empty"
    )

daily_rows.sort(
    key=lambda row: row["date"]
)

accrual_as_of = date.fromisoformat(
    daily_rows[-1]["date"]
)


def rolling_total(field, days):
    start = (
        accrual_as_of
        - timedelta(days=days - 1)
    )

    return sum(
        (
            D(row.get(field)) or Decimal("0")
            for row in daily_rows
            if start
            <= date.fromisoformat(row["date"])
            <= accrual_as_of
        ),
        Decimal("0"),
    )


fees_24h = rolling_total("fees_usd", 1)
fees_7d = rolling_total("fees_usd", 7)
fees_30d = rolling_total("fees_usd", 30)

revenue_24h = rolling_total("revenue_usd", 1)
revenue_7d = rolling_total("revenue_usd", 7)
revenue_30d = rolling_total("revenue_usd", 30)

supply_side_24h = rolling_total("supply_side_revenue_usd", 1)
supply_side_7d = rolling_total("supply_side_revenue_usd", 7)
supply_side_30d = rolling_total("supply_side_revenue_usd", 30)

recent_30d = [
    row
    for row in daily_rows
    if (
        accrual_as_of
        - timedelta(days=29)
    )
    <= date.fromisoformat(row["date"])
    <= accrual_as_of
]

recent_30d_is_provisional = any(
    row.get("basis") != "confirmed_settlement"
    or row.get("offload_basis")
        == "imputed_trailing_7d_average"
    for row in recent_30d
)

projection_model = (
    defi.get("projection_model")
    or {}
)

offload_source = (
    defi.get("offload_source")
    or {}
)


# --------------------------------------------------
# Cross-source QA
# --------------------------------------------------

payment_difference = (
    cumulative_payments
    - source_payments_received
)

if abs(payment_difference) > Decimal("0.02"):
    print(
        "WARNING:",
        "monthly payment sum differs from",
        "settled-accounting source by",
        payment_difference,
    )


state = {
    "schema_version": 2,

    "generated_at_utc":
        utc_now(),

    "source": {
        "name":
            "XNET Revenue Sheet",

        "mode":
            "repository_mirror",

        "source_latest_month":
            latest_source["month"],

        "accounting_basis":
            defi["accounting_basis"],
    },

    "latest_service": {
        "month":
            latest_service["month"],

        "gb":
            dec(
                latest_service[
                    "gb_per_month"
                ]
            ),

        "projected_revenue_usd":
            dec(latest_revenue),

        "blended_rate_per_gb_projected_usd":
            dec(
                latest_service[
                    "blended_rate_per_gb_projected_usd"
                ]
            ),

        "derived_revenue_per_gb_usd":
            (
                None
                if revenue_per_gb is None
                else dec(revenue_per_gb)
            ),

        "revenue_30d_run_rate_usd":
            dec(
                revenue_30d_run_rate
            ),

        "annualized_revenue_run_rate_usd":
            dec(
                annualized_revenue_run_rate
            ),
    },

    "latest_payment": {
        "source_month":
            (
                latest_payment["month"]
                if latest_payment
                else None
            ),

        "payment_date":
            (
                latest_payment[
                    "wifi_payment_date"
                ]
                if latest_payment
                else None
            ),

        "amount_usd":
            (
                dec(
                    latest_payment[
                        "wifi_payment_received_usd"
                    ]
                )
                if latest_payment
                else None
            ),
    },

    "latest_bbb_transfer": {
        "source_month":
            (
                latest_bbb_transfer[
                    "month"
                ]
                if latest_bbb_transfer
                else None
            ),

        "amount_usd":
            (
                dec(
                    latest_bbb_transfer[
                        "transferred_to_buy_burn_usd"
                    ]
                )
                if latest_bbb_transfer
                else None
            ),
    },

    "latest_fiat_operator_transfer": {
        "source_month":
            (
                latest_fiat_transfer["source_month"]
                if latest_fiat_transfer
                else None
            ),

        "operator_payout_usd":
            (
                dec(latest_fiat_operator_payout)
                if latest_fiat_transfer
                else None
            ),

        "gross_fiat_allocation_usd":
            (
                dec(latest_fiat_gross_allocation)
                if latest_fiat_transfer
                else None
            ),

        "bbb_allocation_usd":
            (
                dec(latest_fiat_bbb_allocation)
                if latest_fiat_transfer
                else None
            ),

        "operations_allocation_usd":
            (
                dec(latest_fiat_operations_allocation)
                if latest_fiat_transfer
                else None
            ),

        "operator_share_pct":
            75.0,

        "bbb_share_pct":
            5.0,

        "operations_share_pct":
            20.0,

        "service_month":
            (
                latest_fiat_transfer["service_month"]
                if latest_fiat_transfer
                else None
            ),

        "carrier_settlement_source_month":
            (
                latest_fiat_transfer.get(
                    "carrier_settlement_source_month"
                )
                if latest_fiat_transfer
                else None
            ),

        "carrier_settlement_id":
            (
                latest_fiat_transfer.get(
                    "carrier_settlement_id"
                )
                if latest_fiat_transfer
                else None
            ),

        "service_period_attribution":
            (
                latest_fiat_transfer.get(
                    "attribution_basis"
                )
                if latest_fiat_transfer
                else None
            ),

        "token_emissions_treatment":
            (
                "Corresponding fiat-option emissions are burned; "
                "token quantity is not inferred from the USD sheet."
            ),
    },

    "outstanding": {
        "source_month":
            latest_balance["month"],

        "balance_outstanding_to_transfer_usd":
            dec(
                latest_balance[
                    "balance_outstanding_to_transfer_usd"
                ]
            ),
    },

    "cumulative_source_sheet": {
        "projected_wifi_revenue_usd":
            dec(
                cumulative_projected_revenue
            ),

        "wifi_payments_received_usd":
            dec(
                cumulative_payments
            ),

        "projected_buy_burn_usd":
            dec(
                cumulative_projected_bbb
            ),

        "transferred_to_buy_burn_usd":
            dec(
                cumulative_bbb_transfers
            ),

        "transferred_to_fiat_operators_usd":
            dec(
                cumulative_fiat_transfers
            ),

        "fiat_gross_allocation_usd":
            dec(
                fiat_gross_allocation
            ),

        "fiat_bbb_allocation_usd":
            dec(
                fiat_bbb_allocation
            ),

        "fiat_operations_allocation_usd":
            dec(
                fiat_operations_allocation
            ),
    },

    "settled_accounting": {
        "recognized_service_revenue_usd":
            dec(
                recognized_service_revenue
            ),

        "source_payments_received_usd":
            dec(
                source_payments_received
            ),

        "unattributed_payments_usd":
            dec(
                unattributed_payments
            ),

        "unsettled_service_month_count":
            len(
                defi[
                    "unsettled_service_months"
                ]
            ),
    },

    "live_fee_accrual": {
        "as_of":
            accrual_as_of.isoformat(),

        "fees_24h_usd":
            dec(fees_24h),

        "fees_7d_usd":
            dec(fees_7d),

        "fees_30d_usd":
            dec(fees_30d),

        "revenue_24h_usd":
            dec(revenue_24h),

        "revenue_7d_usd":
            dec(revenue_7d),

        "revenue_30d_usd":
            dec(revenue_30d),

        "supply_side_revenue_24h_usd":
            dec(supply_side_24h),

        "supply_side_revenue_7d_usd":
            dec(supply_side_7d),

        "supply_side_revenue_30d_usd":
            dec(supply_side_30d),

        "avg_daily_fees_7d_usd":
            dec(
                fees_7d
                / Decimal("7")
            ),

        "avg_daily_fees_30d_usd":
            dec(
                fees_30d
                / Decimal("30")
            ),

        "provisional":
            recent_30d_is_provisional,

        "accounting_basis":
            defi.get(
                "accounting_basis"
            ),

        "rate_source_month":
            projection_model.get(
                "rate_source_month"
            ),

        "conservative_rate_usd_per_api_gb":
            projection_model.get(
                "conservative_rate_usd_per_api_gb"
            ),

        "last_measured_offload_date":
            offload_source.get(
                "last_measured_date",
                offload_source.get(
                    "last_date"
                ),
            ),

        "methodology":
            (
                "Rolling Fees use the daily offload-shaped accrual "
                "series prepared for DeFiLlama. Revenue excludes "
                "payments to fiat-option deployers. For the fiat "
                "option, 75% of the gross allocation is Supply-Side "
                "Revenue, 5% goes to BBB and 20% to operations. "
                "Provisional source-month attribution is backfilled "
                "if a service period is later published."
            ),
    },

    "freshness": {
        "source_latest_month":
            latest_source["month"],

        "latest_service_month":
            latest_service["month"],

        "latest_payment_date":
            (
                latest_payment[
                    "wifi_payment_date"
                ]
                if latest_payment
                else None
            ),

        "fee_accrual_as_of":
            accrual_as_of.isoformat(),
    },
}


tmp = OUT.with_suffix(
    ".json.tmp"
)

tmp.write_text(
    json.dumps(
        state,
        indent=2,
    ) + "\n"
)

tmp.replace(OUT)


print(
    "=== XNET REVENUE STATE COMPLETE ==="
)

print(
    "Source latest month:",
    state[
        "source"
    ][
        "source_latest_month"
    ],
)

print(
    "Latest service:",
    state[
        "latest_service"
    ][
        "month"
    ],
)

print(
    "Latest service GB:",
    state[
        "latest_service"
    ][
        "gb"
    ],
)

print(
    "Latest projected revenue: $",
    state[
        "latest_service"
    ][
        "projected_revenue_usd"
    ],
    sep="",
)

print(
    "Annualized run rate: $",
    state[
        "latest_service"
    ][
        "annualized_revenue_run_rate_usd"
    ],
    sep="",
)

print(
    "Latest payment: $",
    state[
        "latest_payment"
    ][
        "amount_usd"
    ],
    " on ",
    state[
        "latest_payment"
    ][
        "payment_date"
    ],
    sep="",
)

print(
    "Latest BBB transfer: $",
    state[
        "latest_bbb_transfer"
    ][
        "amount_usd"
    ],
    sep="",
)

print(
    "Outstanding: $",
    state[
        "outstanding"
    ][
        "balance_outstanding_to_transfer_usd"
    ],
    sep="",
)

print(
    "Recognized service revenue: $",
    state[
        "settled_accounting"
    ][
        "recognized_service_revenue_usd"
    ],
    sep="",
)
