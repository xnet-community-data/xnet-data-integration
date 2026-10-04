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

VIS_STATE = (
    ROOT
    / "state/v3_headline_visuals.json"
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

    options[
        "counterLabel"
    ] = label

    run_json([
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
    ])


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
        VIS_STATE.read_text()
    )[
        "visualizations"
    ]

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
