#!/usr/bin/env python3

import csv
import hashlib
import io
import json
import re
import urllib.request
from datetime import datetime

SHEET_ID = "1NebqJ876SNlO4xPihfJWzsH-V0xzgeA-Qw8i5VcHDU4"
GID = "1205842263"

SOURCE_HUMAN_URL = (
    f"https://docs.google.com/spreadsheets/u/0/d/{SHEET_ID}"
    f"/htmlview?pli=1#gid={GID}"
)

SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={GID}"
)

RAW_PATH = "data/xnet_revenue_sheet_raw.csv"
JSON_PATH = "data/xnet_revenue_monthly.json"

RAW_MIRROR_URL = (
    "https://raw.githubusercontent.com/"
    "xnet-community-data/xnet-data-integration/main/"
    "data/xnet_revenue_sheet_raw.csv"
)

MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}

METRICS = {
    "gb_per_month": "GB per month",
    "wifi_revenue_projected_usd": "WiFi Revenue (Projected)",
    "blended_rate_per_gb_projected_usd": "Blended Rate per GB (Projected)",
    "wifi_payment_received_usd": "WiFi Payment (Received)",
    "wifi_payment_date": "WiFi Payment Date",
    "total_emitted_tokens": "Total Emitted Tokens",
    "projected_buy_burn_usd": "Projected Buy & Burn",
    "transferred_to_buy_burn_usd": "Transferred to Buy & Burn",
    "transferred_to_fiat_operators_usd": "Transferred to Fiat Operators",
    "balance_outstanding_to_transfer_usd": "Balance Outstanding to Transfer",
}

METRIC_ALIASES = {
    "transferred_to_fiat_operators_usd": (
        "Payment Sent to Fiat Operators",
        "Transferred to Fiat Operators",
    ),
    "balance_outstanding_to_transfer_usd": (
        "Balance Outstanding to Transfer",
        "Balance Due to Buy/Burn",
    ),
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_number(value, metric, month):
    raw = clean(value)

    if raw == "":
        return None

    cleaned = (
        raw.replace("$", "")
        .replace(",", "")
        .replace(" ", "")
    )

    try:
        return float(cleaned)
    except ValueError:
        raise RuntimeError(
            f"Could not parse numeric value for {metric} "
            f"at {month}: {raw!r}"
        )


def parse_month(value):
    match = re.fullmatch(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
        clean(value),
    )

    if not match:
        return None

    return f"{match.group(2)}-{MONTHS[match.group(1)]}-01"


def parse_payment_date(value):
    raw = clean(value)

    if raw == "":
        return None

    for fmt in ("%b %d, %y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass

    raise RuntimeError(f"Could not parse WiFi Payment Date: {raw!r}")


def fetch_csv():
    request = urllib.request.Request(
        SOURCE_CSV_URL,
        headers={
            "User-Agent": "XNET-Community-Data/1.0",
            "Accept": "text/csv,text/plain,*/*",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Google Sheet returned HTTP {response.status}"
            )

        content = response.read()

    text = content.decode("utf-8-sig")

    required = [
        ("GB per month",),
        ("WiFi Revenue (Projected)",),
        ("WiFi Payment (Received)",),
        ("Transferred to Buy & Burn",),
        (
            "Balance Outstanding to Transfer",
            "Balance Due to Buy/Burn",
        ),
    ]

    for alternatives in required:
        if not any(marker in text for marker in alternatives):
            raise RuntimeError(
                "Downloaded CSV is missing required row. "
                f"Expected one of: {alternatives}"
            )

    return content, text


def main():
    raw_bytes, text = fetch_csv()

    rows = list(csv.reader(io.StringIO(text)))

    header_index = None

    for i, row in enumerate(rows):
        if any(clean(cell) == "Month" for cell in row):
            header_index = i
            break

    if header_index is None:
        raise RuntimeError("Month header row not found")

    header = rows[header_index]

    metric_rows = {}

    for key, source_label in METRICS.items():
        aliases = METRIC_ALIASES.get(key, (source_label,))
        targets = {clean(label) for label in aliases}

        for row in rows:
            labels = [
                clean(row[0]) if len(row) > 0 else "",
                clean(row[1]) if len(row) > 1 else "",
            ]

            if any(target in labels for target in targets):
                metric_rows[key] = row
                break

        if key not in metric_rows:
            raise RuntimeError(
                "Required source row not found. "
                f"Expected one of: {aliases}"
            )

    data = []

    for col, heading in enumerate(header):
        month = parse_month(heading)

        # BFWD and other non-calendar columns are intentionally skipped.
        if month is None:
            continue

        record = {"month": month}

        for key, source_label in METRICS.items():
            row = metric_rows[key]
            value = row[col] if col < len(row) else ""

            if key == "wifi_payment_date":
                record[key] = parse_payment_date(value)
            else:
                record[key] = parse_number(
                    value,
                    source_label,
                    month,
                )

        data.append(record)

    if not data:
        raise RuntimeError("No monthly records produced")

    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    output = {
        "schema_version": 1,
        "source": {
            "name": "XNET Revenue Sheet",
            "human_url": SOURCE_HUMAN_URL,
            "csv_url": SOURCE_CSV_URL,
            "raw_mirror_url": RAW_MIRROR_URL,
            "raw_csv_sha256": raw_sha256,
            "revision_policy": "source_faithful_current_snapshot",
        },
        "notes": {
            "gb_per_month": (
                "Revenue/billing GB from the XNET revenue sheet. "
                "It is distinct from daily network offload GB."
            ),
            "blank_cells": (
                "Blank source cells are represented as null."
            ),
            "historical_restatements": (
                "This community analytics mirror is source-faithful: each "
                "refresh reflects the XNET team's currently published revenue "
                "sheet, including revisions to historical months. Prior values "
                "are not frozen when the source accounting is restated."
            ),
            "brought_forward": (
                "The non-calendar BFWD column remains available in "
                "the raw CSV but is excluded from monthly records."
            ),
            "token_clearing_price": (
                "Token Clearing Price is intentionally excluded "
                "from the normalized public feed."
            ),
            "transferred_to_fiat_operators_usd": (
                "Normalized from the current 'Payment Sent to Fiat Operators' "
                "row or the equivalent 'Transferred to Fiat Operators' row. "
                "If both are present, the first matching source row is used; "
                "the duplicate route is not double-counted."
            ),
            "balance_outstanding_to_transfer_usd": (
                "Normalized from the current 'Balance Due to Buy/Burn' "
                "source row or its historical 'Balance Outstanding to "
                "Transfer' label. The normalized field name is retained "
                "for schema continuity."
            ),
        },
        "count": len(data),
        "data": data,
    }

    with open(RAW_PATH, "wb") as f:
        f.write(raw_bytes)

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Downloaded {len(raw_bytes):,} CSV bytes")
    print(f"Produced {len(data)} monthly records")
    print(f"SHA256: {raw_sha256}")


if __name__ == "__main__":
    main()
