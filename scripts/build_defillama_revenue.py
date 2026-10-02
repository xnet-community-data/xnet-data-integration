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
            "protocol_revenue": (
                "Not emitted in DeFiLlama v1 because the source now "
                "reports fiat-operator transfers and their exact "
                "service-period attribution and transfer dates are not "
                "yet established."
            ),
            "supply_side_revenue": (
                "Fiat-operator transfers are preserved separately but "
                "are not assigned to a DeFiLlama day until their timing "
                "and service attribution can be verified."
            ),
            "holders_revenue": (
                "Not estimated from policy percentages. XNET token "
                "buybacks, burns and liquidity purchases should be "
                "measured from actual on-chain execution."
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
