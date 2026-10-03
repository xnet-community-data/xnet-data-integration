#!/usr/bin/env python3

import json
from calendar import monthrange
from datetime import date
from decimal import Decimal

INPUT_PATH = "data/xnet_revenue_monthly.json"
OUTPUT_PATH = "data/xnet_defillama_revenue.json"

TOLERANCE = Decimal("0.02")


def D(value):
    return None if value is None else Decimal(str(value))


def money(value):
    return float(value.quantize(Decimal("0.01")))


def month_index(month):
    year, month_num, _ = map(int, month.split("-"))
    return year * 12 + month_num


def month_end(month):
    year, month_num, _ = map(int, month.split("-"))
    last_day = monthrange(year, month_num)[1]
    return date(year, month_num, last_day)


def find_candidates(services, amount, payment_date, assigned):
    candidates = []

    for i, service in enumerate(services):
        if service["month"] in assigned:
            continue

        if month_end(service["month"]) > payment_date:
            continue

        total = Decimal("0")
        previous_index = None
        months = []

        for j in range(i, len(services)):
            candidate = services[j]

            if candidate["month"] in assigned:
                break

            if month_end(candidate["month"]) > payment_date:
                break

            current_index = month_index(candidate["month"])

            if (
                previous_index is not None
                and current_index != previous_index + 1
            ):
                break

            total += candidate["amount"]
            months.append(candidate)
            previous_index = current_index

            if abs(total - amount) <= TOLERANCE:
                candidates.append(list(months))

            if total > amount + TOLERANCE:
                break

    return candidates


def main():
    with open(INPUT_PATH, encoding="utf-8") as f:
        source = json.load(f)

    rows = source["data"]

    # DeFiLlama Revenue = Fees - Supply-Side Revenue.
    # Do not silently claim Fees == Revenue if source-attributed
    # fiat operator payouts become non-zero.
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

    # Fiat-operator transfers are preserved separately.
    # The source currently provides the accounting-month column but
    # not enough information to assign an exact DeFiLlama day or
    # underlying service period safely.
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

    assigned = set()
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
                    "reason": "Source reports an amount but no payment date.",
                }
            )
            continue

        payment_date = date.fromisoformat(payment["payment_date"])

        candidates = find_candidates(
            services,
            payment["amount"],
            payment_date,
            assigned,
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
            f"{payment['payment_date']}-"
            f"{money(payment['amount']):.2f}"
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
            assigned.add(service["month"])
            matched_total += service["amount"]

            service_month = service["month"][:7]
            recognition_date = month_end(service["month"]).isoformat()

            settlement["service_months"].append(
                {
                    "service_month": service_month,
                    "service_revenue_usd": money(service["amount"]),
                }
            )

            recognized.append(
                {
                    "date": recognition_date,
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

        settlement["difference_usd"] = money(
            matched_total - payment["amount"]
        )

        settlements.append(settlement)

    recognized.sort(key=lambda row: row["date"])

    unsettled_services = [
        {
            "service_month": service["month"][:7],
            "projected_service_revenue_usd": money(service["amount"]),
            "status": "not_recognized",
        }
        for service in services
        if service["month"] not in assigned
    ]

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

    if len({row["date"] for row in recognized}) != len(recognized):
        raise RuntimeError(
            "More than one recognized service row uses the same date."
        )

    output = {
        "schema_version": 1,
        "accounting_basis": "settled_service_period",
        "source": source["source"],
        "policy_regimes": [
            {
                "effective_from": "2024-09-30",
                "effective_to": "2025-05-21",
                "holders_revenue_pct": 80,
                "protocol_revenue_pct": 20,
                "protocol_revenue_breakdown_pct": {
                    "operations": 20,
                    "protocol_owned_liquidity": 0,
                },
                "description": (
                    "Historical allocation: 80% of carrier service revenue "
                    "allocated to XNET market buyback-and-burn and 20% to "
                    "operations."
                ),
            },
            {
                "effective_from": "2025-05-22",
                "effective_to": None,
                "holders_revenue_pct": 60,
                "protocol_revenue_pct": 40,
                "protocol_revenue_breakdown_pct": {
                    "operations": 20,
                    "protocol_owned_liquidity": 20,
                },
                "description": (
                    "XIP-12 allocation: 60% of carrier service revenue "
                    "continues to XNET market buyback-and-burn, 20% is "
                    "allocated to protocol-owned liquidity to bolster XNET "
                    "liquidity, and 20% remains allocated to operations."
                ),
            },
        ],
        "methodology": {
            "fees": (
                "Carrier WiFi service revenue is recognized only after "
                "a recorded payment can be uniquely reconciled to the "
                "underlying service month or contiguous service months."
            ),
            "recognition_date": (
                "Recognized revenue is booked on the final calendar day "
                "of its underlying service month."
            ),
            "projected_revenue": (
                "Unsettled projected WiFi revenue is excluded from "
                "DeFiLlama metrics."
            ),
            "unattributed_payments": (
                "Payments without sufficient date or service-period "
                "evidence remain unattributed and are excluded."
            ),
            "policy_allocation": (
                "For DeFiLlama policy-based allocation, recognized service "
                "periods before 2025-05-22 use the historical 80% "
                "Holders Revenue / 20% Protocol Revenue split. Recognized "
                "service periods from 2025-05-22 use the XIP-12 60% "
                "Holders Revenue / 40% Protocol Revenue split, with the "
                "40% Protocol Revenue comprising 20% protocol-owned "
                "liquidity and 20% operations."
            ),
            "protocol_revenue": (
                "Policy-derived Protocol Revenue is 20% of carrier "
                "service revenue under the historical allocation. From "
                "2025-05-22 under XIP-12 it is 40%: 20% for operations "
                "and 20% for protocol-owned liquidity. The liquidity "
                "allocation remains Protocol Revenue even when part of it "
                "is used to acquire XNET for the XNET side of the "
                "protocol-owned liquidity position."
            ),
            "supply_side_revenue": (
                "No Supply-side Revenue is estimated from the policy "
                "allocation used by the covered adapter history. Reported "
                "fiat-operator transfers are preserved separately and are "
                "not assigned to service periods until their timing and "
                "service attribution can be independently established."
            ),
            "holders_revenue": (
                "Policy-derived Holders Revenue is historically 80% "
                "of carrier service revenue allocated to XNET market "
                "buyback-and-burn. From 2025-05-22 under XIP-12, 60% "
                "continues to fund XNET buyback-and-burn while 20 "
                "percentage points were redirected to protocol-owned "
                "liquidity to bolster XNET liquidity. These percentages "
                "represent policy allocation rather than measured "
                "on-chain execution amounts."
            ),
        },
        "totals": {
            "source_payments_received_usd": money(
                source_received_total
            ),
            "recognized_service_revenue_usd": money(
                recognized_total
            ),
            "unattributed_payments_usd": money(
                unattributed_total
            ),
        },
        "count": len(recognized),
        "data": recognized,
        "settlements": settlements,
        "unattributed_settlements": unattributed,
        "fiat_operator_transfers": fiat_operator_transfers,
        "unsettled_service_months": unsettled_services,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Recognized service months: {len(recognized)}")
    print(
        "Recognized revenue: "
        f"${money(recognized_total):,.2f}"
    )
    print(
        "Unattributed payments: "
        f"${money(unattributed_total):,.2f}"
    )
    print(
        "Source payment total: "
        f"${money(source_received_total):,.2f}"
    )
    print(
        "Unsettled projected service months: "
        f"{len(unsettled_services)}"
    )


if __name__ == "__main__":
    main()
