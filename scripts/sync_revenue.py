#!/usr/bin/env python3

import csv
import hashlib
import io
import json
import re
import urllib.request
from datetime import date, datetime, timedelta

SHEET_ID = "1NebqJ876SNlO4xPihfJWzsH-V0xzgeA-Qw8i5VcHDU4"
REVENUE_GID = "1205842263"
SUMMARY_GID = "1994698195"

SOURCE_HUMAN_URL = (
    f"https://docs.google.com/spreadsheets/u/0/d/{SHEET_ID}"
    f"/htmlview?pli=1#gid={REVENUE_GID}"
)

SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={REVENUE_GID}"
)

EPOCH_SOURCE_HUMAN_URL = (
    f"https://docs.google.com/spreadsheets/u/0/d/{SHEET_ID}"
    f"/htmlview?pli=1#gid={SUMMARY_GID}"
)

EPOCH_SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={SUMMARY_GID}"
)

RAW_PATH = "data/xnet_revenue_sheet_raw.csv"
JSON_PATH = "data/xnet_revenue_monthly.json"
EPOCH_JSON_PATH = "data/xnet_epoch_schedule.json"

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


def fetch_csv(url, required):
    request = urllib.request.Request(
        url,
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

    for alternatives in required:
        if not any(marker in text for marker in alternatives):
            raise RuntimeError(
                "Downloaded CSV is missing required row. "
                f"Expected one of: {alternatives}"
            )

    return content, text


def parse_epoch_date(value, label):
    raw = clean(value)

    if raw == "":
        return None

    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    raise RuntimeError(
        f"Could not parse {label}: {raw!r}"
    )


def parse_epoch_schedule(text):
    rows = list(csv.reader(io.StringIO(text)))

    header = next(
        (
            row
            for row in rows
            if any(
                re.fullmatch(r"Epoch\s+\d+", clean(cell))
                for cell in row
            )
        ),
        None,
    )

    if header is None:
        raise RuntimeError("Epoch header row not found")

    def source_row(label):
        for row in rows:
            if clean(row[0] if row else "") == label:
                return row
        raise RuntimeError(
            f"Required epoch source row not found: {label}"
        )

    start_row = source_row("Epoch Start Date")
    end_row = source_row("Epoch End Date")
    poc_row = source_row("PoC - Dispersal")
    data_row = source_row("Data - Dispersal")
    bonus_row = source_row("Bonus - Dispersal")

    total_row = None
    for row in rows:
        if not row or clean(row[0]) != "":
            continue
        populated = [
            clean(cell)
            for cell in row[2:]
            if clean(cell) != ""
        ]
        if populated and len(populated) >= 3:
            try:
                first = parse_number(
                    row[2],
                    "Epoch total",
                    "summary",
                )
            except RuntimeError:
                continue
            if first is not None:
                total_row = row
                break

    if total_row is None:
        raise RuntimeError("Epoch total row not found")

    fiat_row = next(
        (
            row
            for row in rows
            if clean(row[0] if row else "")
            == "Fiat Operator Burn"
        ),
        None,
    )

    schedule = []

    for col, heading in enumerate(header):
        match = re.fullmatch(
            r"Epoch\s+(\d+)",
            clean(heading),
        )

        if not match:
            continue

        epoch = int(match.group(1))
        start = parse_epoch_date(
            start_row[col] if col < len(start_row) else "",
            f"Epoch {epoch} start date",
        )
        end = parse_epoch_date(
            end_row[col] if col < len(end_row) else "",
            f"Epoch {epoch} end date",
        )

        if start is None or end is None:
            raise RuntimeError(
                f"Epoch {epoch} is missing a start/end date"
            )

        poc = parse_number(
            poc_row[col] if col < len(poc_row) else "",
            "PoC - Dispersal",
            f"Epoch {epoch}",
        ) or 0.0
        data = parse_number(
            data_row[col] if col < len(data_row) else "",
            "Data - Dispersal",
            f"Epoch {epoch}",
        ) or 0.0
        bonus = parse_number(
            bonus_row[col] if col < len(bonus_row) else "",
            "Bonus - Dispersal",
            f"Epoch {epoch}",
        ) or 0.0
        total = parse_number(
            total_row[col] if col < len(total_row) else "",
            "Total epoch dispersal",
            f"Epoch {epoch}",
        )

        if total is None:
            raise RuntimeError(
                f"Epoch {epoch} is missing total dispersal"
            )

        fiat_burn = (
            parse_number(
                fiat_row[col] if fiat_row and col < len(fiat_row) else "",
                "Fiat Operator Burn",
                f"Epoch {epoch}",
            )
            if fiat_row
            else None
        )

        inclusive_days = (end - start).days + 1

        if inclusive_days != 14:
            raise RuntimeError(
                f"Epoch {epoch} spans {inclusive_days} days, expected 14"
            )

        if abs((poc + data + bonus) - total) > 1.0:
            raise RuntimeError(
                f"Epoch {epoch} components do not reconcile to total: "
                f"{poc + data + bonus} != {total}"
            )

        if schedule:
            previous = schedule[-1]

            if epoch != previous["epoch"] + 1:
                raise RuntimeError(
                    f"Epoch numbering gap: {previous['epoch']} -> {epoch}"
                )

            expected_start = (
                date.fromisoformat(previous["end_date"])
                + timedelta(days=1)
            )

            if start != expected_start:
                raise RuntimeError(
                    f"Epoch {epoch} does not start immediately after "
                    f"Epoch {previous['epoch']}: "
                    f"{start.isoformat()} != {expected_start.isoformat()}"
                )

        schedule.append(
            {
                "epoch": epoch,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "poc_dispersal_xnet": round(poc),
                "data_dispersal_xnet": round(data),
                "bonus_dispersal_xnet": round(bonus),
                "total_dispersal_xnet": round(total),
                "fiat_operator_burn_xnet": (
                    round(fiat_burn)
                    if fiat_burn is not None
                    else None
                ),
            }
        )

    if not schedule:
        raise RuntimeError("No epoch records produced")

    return schedule


def main():
    raw_bytes, text = fetch_csv(
        SOURCE_CSV_URL,
        [
            ("GB per month",),
            ("WiFi Revenue (Projected)",),
            ("WiFi Payment (Received)",),
            ("Transferred to Buy & Burn",),
            (
                "Balance Outstanding to Transfer",
                "Balance Due to Buy/Burn",
            ),
        ],
    )

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

    epoch_raw_bytes, epoch_text = fetch_csv(
        EPOCH_SOURCE_CSV_URL,
        [
            ("Epoch Start Date",),
            ("Epoch End Date",),
            ("PoC - Dispersal",),
            ("Data - Dispersal",),
            ("Bonus - Dispersal",),
        ],
    )
    epoch_schedule = parse_epoch_schedule(epoch_text)
    epoch_sha256 = hashlib.sha256(epoch_raw_bytes).hexdigest()

    epoch_output = {
        "schema_version": 1,
        "source": {
            "name": "XNET Reward Tokens by Epoch Summary",
            "human_url": EPOCH_SOURCE_HUMAN_URL,
            "csv_url": EPOCH_SOURCE_CSV_URL,
            "raw_csv_sha256": epoch_sha256,
            "revision_policy": "source_faithful_current_snapshot",
        },
        "notes": {
            "date_policy": (
                "Epoch boundaries are mirrored exactly from the team's "
                "published Summary tab. No future epoch or decay date is "
                "invented beyond the latest published epoch."
            ),
            "validation": (
                "Each published epoch must span exactly 14 inclusive days, "
                "start immediately after the preceding epoch, use consecutive "
                "epoch numbering, and reconcile PoC + Data + Bonus to total "
                "dispersal within one displayed XNET of rounding."
            ),
        },
        "count": len(epoch_schedule),
        "first_epoch": epoch_schedule[0]["epoch"],
        "last_epoch": epoch_schedule[-1]["epoch"],
        "data": epoch_schedule,
    }

    with open(EPOCH_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(
            epoch_output,
            f,
            indent=2,
            ensure_ascii=False,
        )
        f.write("\n")

    print(f"Downloaded {len(raw_bytes):,} revenue CSV bytes")
    print(f"Produced {len(data)} monthly records")
    print(f"Revenue SHA256: {raw_sha256}")
    print(f"Downloaded {len(epoch_raw_bytes):,} epoch Summary CSV bytes")
    print(
        f"Produced {len(epoch_schedule)} source-confirmed epochs "
        f"({epoch_schedule[0]['epoch']}..{epoch_schedule[-1]['epoch']})"
    )
    print(f"Epoch SHA256: {epoch_sha256}")


if __name__ == "__main__":
    main()
