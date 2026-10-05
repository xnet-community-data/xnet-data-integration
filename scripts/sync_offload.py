#!/usr/bin/env python3
"""Mirror XNET's daily offload feed into a stable, fail-safe public cache."""

import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

URL = "https://xnet-offload-scraper.vercel.app/api/data"
OUT = "data/xnet_offload_api.json"


def normalize_date(value):
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Invalid offload date: {raw!r}") from exc


def normalize_gigabytes(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid offload gigabytes value: {value!r}") from exc

    if not math.isfinite(number) or number < 0:
        raise RuntimeError(f"Invalid offload gigabytes value: {value!r}")

    return number


def candidate_rows(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected offload API response type")

    for key in ("data", "records", "points"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("points"), list):
            return value["points"]

    raise RuntimeError("Could not locate offload records in API response")


def normalize_payload(payload):
    today_utc = datetime.now(timezone.utc).date().isoformat()
    normalized = {}

    for row in candidate_rows(payload):
        if not isinstance(row, dict):
            continue

        raw_day = row.get("day", row.get("date"))
        raw_gb = row.get("gigabytes", row.get("value"))

        if raw_day is None or raw_gb is None:
            continue

        day = normalize_date(raw_day)

        # The current calendar day can still be accumulating. DeFiLlama's
        # 24h/7d/30d flow metrics should only use completed observations.
        if day >= today_utc:
            continue

        normalized[day] = normalize_gigabytes(raw_gb)

    if not normalized:
        raise RuntimeError("Offload API produced no completed daily records")

    return normalized


def load_existing():
    if not os.path.exists(OUT):
        return {}

    with open(OUT, encoding="utf-8") as f:
        payload = json.load(f)

    rows = payload.get("data", [])
    existing = {}

    for row in rows:
        existing[normalize_date(row["date"])] = normalize_gigabytes(
            row["gigabytes"]
        )

    return existing


def fetch_live():
    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "XNET-Community-Data/1.0",
            "Accept": "application/json,*/*",
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Offload API returned HTTP {response.status}"
            )
        payload = json.load(response)

    return normalize_payload(payload)


def write_cache(records):
    data = [
        {"date": day, "gigabytes": records[day]}
        for day in sorted(records)
    ]

    output = {
        "schema_version": 1,
        "source": {
            "name": "XNET daily offload API",
            "url": URL,
        },
        "notes": {
            "metric": (
                "Daily network offload in GB. This is not identical to "
                "revenue-sheet billing GB."
            ),
            "completeness": (
                "Only completed calendar days are cached for DeFiLlama "
                "accrual. Missing days are never filled with zero."
            ),
            "cache_policy": (
                "Live sync merges completed-day observations into this "
                "cache. If the upstream API is unavailable, the last valid "
                "cache is preserved."
            ),
        },
        "count": len(data),
        "first_date": data[0]["date"],
        "last_date": data[-1]["date"],
        "data": data,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        prefix="xnet_offload_",
        suffix=".json",
        dir=os.path.dirname(OUT),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    return output


def main():
    existing = load_existing()

    try:
        live = fetch_live()
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        if not existing:
            print(f"ERROR: offload API unavailable and no cache exists: {exc}")
            sys.exit(1)

        print(
            "WARNING: offload API unavailable; preserving the last valid "
            f"cache through {max(existing)}: {exc}"
        )
        # Exit successfully. Downstream revenue generation can continue from
        # the last trustworthy observations and will reconcile when the API
        # resumes.
        return

    merged = dict(existing)
    merged.update(live)

    if not merged:
        raise RuntimeError("No offload observations available after merge")

    output = write_cache(merged)
    print(
        "OK: cached "
        f"{output['count']} completed offload days "
        f"({output['first_date']} through {output['last_date']})"
    )


if __name__ == "__main__":
    main()
