#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REVENUE_STATE = (
    ROOT
    / "data/current/xnet_revenue_state.json"
)

DEFI = (
    ROOT
    / "data/xnet_defillama_revenue.json"
)

VIS_CONFIG = (
    ROOT
    / "config/v3_headline_visual_ids.json"
)


def month_label(value):
    if not value:
        return None

    dt = datetime.strptime(
        str(value)[:7],
        "%Y-%m",
    )

    return dt.strftime(
        "%b %Y"
    )


def date_label(value):
    if not value:
        return None

    dt = datetime.strptime(
        str(value)[:10],
        "%Y-%m-%d",
    )

    return (
        f"{dt.day} "
        f"{dt.strftime('%b %Y')}"
    )


def D(value):
    if value in (
        None,
        "",
    ):
        return None

    return Decimal(
        str(value)
    )


def collect_service_months(node):
    months = set()

    if isinstance(
        node,
        dict,
    ):
        value = node.get(
            "service_month"
        )

        if value:
            months.add(
                str(value)[:7]
            )

        for child in node.values():
            months.update(
                collect_service_months(
                    child
                )
            )

    elif isinstance(
        node,
        list,
    ):
        for child in node:
            months.update(
                collect_service_months(
                    child
                )
            )

    return months


def payment_service_months(
    defi,
    payment_date,
    amount,
):
    matches = []

    def walk(node):
        if isinstance(
            node,
            dict,
        ):
            node_date = node.get(
                "payment_received_date"
            )

            node_amount = node.get(
                "payment_received_usd"
            )

            if (
                node_date
                and str(node_date)[:10]
                == str(payment_date)[:10]
                and node_amount is not None
            ):
                try:
                    same_amount = (
                        abs(
                            D(node_amount)
                            - D(amount)
                        )
                        <= Decimal(
                            "0.01"
                        )
                    )
                except Exception:
                    same_amount = False

                if same_amount:
                    months = (
                        collect_service_months(
                            node
                        )
                    )

                    if months:
                        matches.append(
                            months
                        )

            for child in node.values():
                walk(child)

        elif isinstance(
            node,
            list,
        ):
            for child in node:
                walk(child)

    walk(defi)

    merged = set()

    for match in matches:
        merged.update(
            match
        )

    return sorted(
        merged
    )


def latest_fiat_transfer(defi):
    rows = [
        row
        for row in defi.get(
            "fiat_operator_transfers",
            [],
        )
        if row.get(
            "operator_payout_usd"
        ) is not None
        and row.get(
            "service_month"
        )
    ]

    if not rows:
        return None

    return max(
        rows,
        key=lambda row: (
            str(
                row.get(
                    "source_month"
                )
                or ""
            ),
            str(
                row.get(
                    "service_month"
                )
                or ""
            ),
        ),
    )


def compact_months(months):
    labels = [
        month_label(
            m
        )
        for m in months
    ]

    labels = [
        x
        for x in labels
        if x
    ]

    if not labels:
        return None

    if len(labels) == 1:
        return labels[0]

    return " + ".join(
        labels
    )


def run_json(cmd):
    p = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    if p.returncode != 0:
        print(
            p.stdout
        )
        print(
            p.stderr
        )
        raise RuntimeError(
            "Command failed: "
            + " ".join(
                cmd
            )
        )

    return json.loads(
        p.stdout
    )


def update_counter(
    viz_id,
    label,
    description,
    expected_column,
    name=None,
):
    viz = run_json([
        "dune",
        "viz",
        "get",
        str(viz_id),
        "-o",
        "json",
    ])

    options = viz.get(
        "options"
    )

    if isinstance(
        options,
        str,
    ):
        options = json.loads(
            options
        )

    options = dict(
        options
        or {}
    )

    if options.get("counterColName") != expected_column:
        raise RuntimeError(
            f"Visualization {viz_id} uses an unexpected counter column"
        )

    if (options.get("counterLabel") == label and viz.get("description") == description
            and (name is None or viz.get("name") == name)):
        print(f"Counter {viz_id} label unchanged.")
        return

    options["counterLabel"] = label

    args = [
        "dune",
        "viz",
        "update",
        str(viz_id),
        "--description",
        description,
        "--options",
        json.dumps(
            options,
            separators=(
                ",",
                ":",
            ),
        ),
        "-o",
        "json",
    ]
    if name is not None:
        args.extend(["--name", name])
    run_json(args)


def sync_foundation_labels(revenue, vis):
    """Relabel existing widgets only; never alter query SQL, columns or layout."""
    foundation = revenue.get("foundation_accounting")
    if not foundation:
        return
    month = month_label(foundation["source_month"])
    inc = foundation.get("inc_payable_usd")
    description = (
        f"Foundation booked balance for {month}; not BBB wallet cash. "
        + (f"The separate XNET Inc. payable is ${Decimal(inc):,.2f}; " if inc is not None else "")
        + "the two ledgers have different posting timelines. Future-month balances are excluded."
    )
    for key, label, detail, name in (
        ("foundation_booked_balance", f"{month} Foundation ledger", description,
         "Foundation Booked Balance"),
        ("foundation_transfers", "Cumulative Foundation transfers",
         "Source-recorded transfers to the XNET Foundation, counted once across mirrored ledger rows. "
         "Funding can support Foundation allocations; this is not proof of DEX buys or token burns.",
         "Transferred to Foundation"),
    ):
        if key in vis:
            spec = vis[key]
            update_counter(spec["id"], label, detail, spec["column"], name=name)

    config = ROOT / "config/v3_history_visual_ids.json"
    if not config.exists():
        return
    chart_id = json.loads(config.read_text())["bbb_allocation_transfers"]
    chart = run_json(["dune", "viz", "get", str(chart_id), "-o", "json"])
    options = chart.get("options") or {}
    if isinstance(options, str):
        options = json.loads(options)
    fields = {"projected_buy_burn_usd": "Foundation Receivables",
              "transferred_to_buy_burn_usd": "Transferred to Foundation"}
    if chart.get("query_id") != 8895089 or not set(fields).issubset(options.get("columnMapping", {})):
        raise RuntimeError("Foundation chart identity/columns changed; refusing an unrelated visualization update")
    before = json.dumps(options, sort_keys=True)
    for column, label in fields.items():
        options.setdefault("seriesOptions", {}).setdefault(column, {})["name"] = label
    name = "Foundation Receivables, Transfers & Fiat Operator Payouts"
    detail = "Foundation-ledger receivables and transfers, plus recorded fiat payouts. " \
             "Legacy BBB-named columns are compatibility identifiers, not on-chain purchases/burns."
    if before != json.dumps(options, sort_keys=True) or chart.get("name") != name or chart.get("description") != detail:
        run_json(["dune", "viz", "update", str(chart_id), "--name", name, "--description", detail,
                  "--options", json.dumps(options, separators=(",", ":")), "-o", "json"])


def main():
    if not os.environ.get(
        "DUNE_API_KEY"
    ):
        raise SystemExit(
            "DUNE_API_KEY is not set"
        )

    revenue = json.loads(
        REVENUE_STATE.read_text()
    )

    defi = json.loads(
        DEFI.read_text()
    )

    vis = json.loads(
        VIS_CONFIG.read_text()
    )[
        "visualizations"
    ]
    sync_foundation_labels(revenue, vis)

    latest_service = (
        revenue.get(
            "latest_service"
        )
        or {}
    )

    service_month = (
        latest_service.get(
            "month"
        )
        or (
            revenue.get(
                "summary"
            )
            or {}
        ).get(
            "latest_service_month"
        )
    )

    projected_label = month_label(
        service_month
    )

    if not projected_label:
        raise RuntimeError(
            "Could not determine latest "
            "projected service month"
        )

    projected_description = (
        "Projected WiFi revenue for "
        f"{projected_label}, the latest "
        "service month in the revenue sheet. "
        "Service-month reporting is delayed, "
        "so this can lag the current month."
    )

    update_counter(
        vis[
            "latest_projected_revenue"
        ][
            "id"
        ],
        projected_label,
        projected_description,
        vis["latest_projected_revenue"]["column"],
    )

    latest_payment = (
        revenue.get(
            "latest_payment"
        )
        or {}
    )

    payment_date = latest_payment.get(
        "payment_date"
    )

    payment_amount = latest_payment.get(
        "amount_usd"
    )

    service_months = (
        payment_service_months(
            defi,
            payment_date,
            payment_amount,
        )
    )

    payment_label = compact_months(
        service_months
    )

    if not payment_label:
        print(
            "WARNING: no service-month mapping "
            "found for latest payment; "
            "preserving existing payment label."
        )

    else:
        received_label = date_label(
            payment_date
        )

        payment_description = (
            "Carrier payment received "
            f"{received_label} for "
            f"{payment_label} service. "
            "Carrier payments settle after "
            "the service month, so cash "
            "receipts lag network activity."
        )

        update_counter(
            vis[
                "latest_payment"
            ][
                "id"
            ],
            payment_label,
            payment_description,
            vis["latest_payment"]["column"],
        )

    fiat = latest_fiat_transfer(
        defi
    )

    if fiat and fiat.get("service_month"):
        # An unrelated later carrier receipt must not relabel an older payout.
        # Use THIS payout's reconciled service month, not latest_payment's.
        fiat_service_label = month_label(fiat["service_month"])

        fiat_source_label = month_label(
            fiat.get(
                "source_month"
            )
        )

        fiat_label = (
            f"{fiat_service_label} service"
        )

        payout_description = (
            "Cash paid to operators that "
            "chose the XIP-13.1 fiat option "
            f"for {fiat_service_label} service. "
            "The service period follows this payout's "
            "reconciled carrier settlement, not the latest unrelated receipt. "
            + (
                f"The fiat transfer is reported "
                f"in the {fiat_source_label} source row."
                if fiat_source_label
                else
                "The fiat transfer is reported separately "
                "in the revenue source."
            )
        )

        share_description = (
            "Share of "
            f"{fiat_service_label} service revenue "
            "routed through the fiat option. "
            "The service period follows this payout's "
            "reconciled carrier settlement and "
            "uses the full fiat allocation before "
            "the 75% operator / 5% BBB / "
            "20% operations split."
        )

        update_counter(
            vis[
                "latest_fiat_operator_payout"
            ][
                "id"
            ],
            fiat_label,
            payout_description,
            vis[
                "latest_fiat_operator_payout"
            ][
                "column"
            ],
        )

        update_counter(
            vis[
                "fiat_routed_share"
            ][
                "id"
            ],
            fiat_label,
            share_description,
            vis[
                "fiat_routed_share"
            ][
                "column"
            ],
        )

    else:
        print(
            "WARNING: no fiat operator payout or "
            "latest payment service mapping found; "
            "preserving existing fiat labels."
        )

    print(
        "Projected revenue label:",
        projected_label,
    )

    print(
        "Latest payment service label:",
        payment_label
        or "PRESERVED LAST GOOD",
    )


if __name__ == "__main__":
    main()
