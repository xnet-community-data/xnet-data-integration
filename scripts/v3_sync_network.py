#!/usr/bin/env python3
"""Refresh XNET V3 network state from the public offload and device APIs.

Fail-safe rules:
- only completed UTC offload days are admitted
- missing offload dates are never filled with zero
- device history is merged into the last-known-good history
- network outputs are replaced only after both upstreams validate
- upstream failure preserves the current network files
- cadence is read from config/v3_pipeline.json
"""
from __future__ import annotations

import calendar
import json
import math
import os
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/v3_pipeline.json"
REFRESH_STATE = ROOT / "data/current/v3_refresh_state.json"
NETWORK_STATE = ROOT / "data/current/xnet_network_state.json"
DEVICE_HISTORY = ROOT / "data/network/device_history.json"
OFFLOAD_MONTHLY = ROOT / "data/network/offload_monthly.json"

OFFLOAD_URL = "https://xnet-offload-scraper.vercel.app/api/data"
DEVICE_LATEST_URL = "https://xnet-total-devices-api.vercel.app/api/latest"
DEVICE_HISTORY_URL = "https://xnet-total-devices-api.vercel.app/api/history?limit=100"

USER_AGENT = "XNET-Community-Data/1.0"
REQUEST_TIMEOUT_SECONDS = 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    value = (value or utc_now()).astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,*/*",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.load(response)


def due(last_attempt: str | None, cadence_minutes: int) -> bool:
    if not last_attempt:
        return True
    previous = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
    elapsed = (utc_now() - previous).total_seconds()
    return elapsed >= cadence_minutes * 60 - 60


def network_cadence_minutes() -> int:
    config = load_json(PIPELINE, {})
    network = config.get("sources", {}).get("network", {})
    return int(network.get("cadence_minutes", 1440))


def offload_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "records", "points"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("points"), list):
                return value["points"]
    raise RuntimeError("Could not locate offload records in API response")


def normalize_offload(payload) -> list[dict]:
    today = utc_now().date()
    by_day: dict[date, float] = {}

    for row in offload_rows(payload):
        if not isinstance(row, dict):
            continue
        raw_day = row.get("day", row.get("date"))
        raw_gb = row.get("gigabytes", row.get("value"))
        if raw_day is None or raw_gb is None:
            continue
        try:
            day = date.fromisoformat(str(raw_day).strip()[:10])
            gb = float(raw_gb)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid offload row: {row!r}") from exc
        if not math.isfinite(gb) or gb < 0:
            raise RuntimeError(f"Invalid offload gigabytes: {raw_gb!r}")
        if day >= today:
            continue
        by_day[day] = gb

    if len(by_day) < 30:
        raise RuntimeError("Offload API returned fewer than 30 completed days")

    days = sorted(by_day)
    latest = days[-1]
    if latest < today - timedelta(days=2):
        raise RuntimeError(
            f"Offload API is stale: latest completed day is {latest.isoformat()}"
        )

    for previous, current in zip(days, days[1:]):
        if current != previous + timedelta(days=1):
            raise RuntimeError(
                "Offload API contains a calendar gap between "
                f"{previous.isoformat()} and {current.isoformat()}"
            )

    return [
        {"date": day.isoformat(), "gigabytes": by_day[day]}
        for day in days
    ]


def unwrap_latest_device(payload) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected device latest response type")
    for key in ("data", "latest", "record", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def device_history_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "history", "records", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise RuntimeError("Could not locate device history records in API response")


def parse_datetime(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith(" UTC"):
        value = value[:-4] + "+00:00"
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid device timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_observed_at(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    milliseconds = value.microsecond // 1000
    if milliseconds:
        return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{milliseconds:03d} UTC"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_device_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise RuntimeError("Invalid device record type")

    total_raw = row.get("totalDevices", row.get("total_devices"))
    operational_raw = row.get(
        "totalOperational",
        row.get("total_operational", row.get("operationalDevices")),
    )
    if total_raw is None or operational_raw is None:
        raise RuntimeError(f"Device counts missing from record: {row!r}")

    try:
        total = int(total_raw)
        operational = int(operational_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid device counts: {row!r}") from exc

    if total < 0 or operational < 0 or operational > total:
        raise RuntimeError(f"Impossible device counts: {row!r}")
    if total > 10_000_000:
        raise RuntimeError(f"Implausible device count: {total}")

    created_raw = row.get("createdAt", row.get("created_at"))
    scrape_date_raw = row.get("scrapeDate", row.get("scrape_date"))
    scrape_time_raw = row.get("scrapeTime", row.get("scrape_time"))

    observed: datetime | None = None
    if created_raw:
        observed = parse_datetime(str(created_raw))
    elif scrape_date_raw:
        day = date.fromisoformat(str(scrape_date_raw).strip()[:10])
        if scrape_time_raw:
            raw_time = str(scrape_time_raw).strip()
            parsed_time = time.fromisoformat(raw_time.replace("Z", ""))
        else:
            parsed_time = time.min
        observed = datetime.combine(day, parsed_time, tzinfo=timezone.utc)

    if observed is None:
        raise RuntimeError(f"Device timestamp missing from record: {row!r}")

    if observed > utc_now() + timedelta(minutes=10):
        raise RuntimeError(f"Device observation is in the future: {observed!s}")

    if scrape_date_raw:
        observation_day = date.fromisoformat(str(scrape_date_raw).strip()[:10])
    else:
        observation_day = observed.date()

    return {
        "observation_date": observation_day.isoformat(),
        "observed_at_utc": format_observed_at(observed),
        "total_devices": total,
        "total_operational": operational,
        "_sort_utc": observed,
    }


def normalize_devices(latest_payload, history_payload, existing_payload):
    live = [
        normalize_device_row(row)
        for row in device_history_rows(history_payload)
        if isinstance(row, dict)
    ]
    latest = normalize_device_row(unwrap_latest_device(latest_payload))
    live.append(latest)

    newest_live = max(live, key=lambda row: row["_sort_utc"])
    if newest_live["_sort_utc"].date() < utc_now().date() - timedelta(days=2):
        raise RuntimeError(
            "Device API is stale: latest observation is "
            f"{newest_live['observation_date']}"
        )

    merged: dict[tuple[str, str], dict] = {}

    for row in existing_payload.get("data", []):
        if not isinstance(row, dict):
            continue
        required = (
            row.get("observation_date"),
            row.get("observed_at_utc"),
            row.get("total_devices"),
            row.get("total_operational"),
        )
        if any(value is None for value in required):
            continue
        merged[
            (str(row["observation_date"]), str(row["observed_at_utc"]))
        ] = {
            "observation_date": str(row["observation_date"])[:10],
            "observed_at_utc": str(row["observed_at_utc"]),
            "total_devices": int(row["total_devices"]),
            "total_operational": int(row["total_operational"]),
        }

    for row in live:
        clean = {key: value for key, value in row.items() if key != "_sort_utc"}
        merged[(clean["observation_date"], clean["observed_at_utc"])] = clean

    def sort_key(row: dict):
        try:
            return parse_datetime(row["observed_at_utc"])
        except RuntimeError:
            return datetime.combine(
                date.fromisoformat(row["observation_date"]),
                time.min,
                tzinfo=timezone.utc,
            )

    rows = sorted(merged.values(), key=sort_key)
    if not rows:
        raise RuntimeError("Device history is empty after merge")

    return rows, {key: value for key, value in newest_live.items() if key != "_sort_utc"}


def nearest_prior(rows: list[dict], target: date) -> dict | None:
    eligible = [
        row for row in rows
        if date.fromisoformat(row["observation_date"]) <= target
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda row: (
            row["observation_date"],
            row["observed_at_utc"],
        ),
    )


def growth(current: dict, prior: dict | None, field: str):
    if prior is None:
        return None, None
    c = float(current[field])
    p = float(prior[field])
    absolute = c - p
    pct = absolute / p * 100.0 if p else None
    return absolute, pct


def build_outputs(offload_rows_normalized, device_rows, latest_device):
    today = utc_now().date()

    offload = [
        (date.fromisoformat(row["date"]), float(row["gigabytes"]))
        for row in offload_rows_normalized
    ]
    latest_day, latest_gb = offload[-1]
    last_30 = offload[-30:]
    avg_30 = sum(gb for _, gb in last_30) / len(last_30)
    all_time = sum(gb for _, gb in offload)

    monthly: dict[date, dict] = {}
    for day, gb in offload:
        month = day.replace(day=1)
        entry = monthly.setdefault(month, {"gb": 0.0, "days": 0})
        entry["gb"] += gb
        entry["days"] += 1

    current_month = today.replace(day=1)
    complete_months = []
    first_month = offload[0][0].replace(day=1)
    for month in sorted(monthly):
        if month >= current_month:
            continue
        expected_days = calendar.monthrange(month.year, month.month)[1]
        # The API begins 2024-07-29, so only the first source month can be partial.
        if monthly[month]["days"] != expected_days:
            if month == first_month:
                continue
            raise RuntimeError(
                f"Offload month {month.isoformat()} has "
                f"{monthly[month]['days']} of {expected_days} days"
            )
        complete_months.append(month)

    if len(complete_months) < 2:
        raise RuntimeError("Need at least two complete offload months")

    latest_month = complete_months[-1]
    previous_month = complete_months[-2]
    latest_month_gb = monthly[latest_month]["gb"]
    previous_month_gb = monthly[previous_month]["gb"]
    mom = (
        (latest_month_gb - previous_month_gb)
        / previous_month_gb
        * 100.0
        if previous_month_gb
        else None
    )

    latest_device_day = date.fromisoformat(latest_device["observation_date"])
    prior_7 = nearest_prior(device_rows, latest_device_day - timedelta(days=7))
    prior_30 = nearest_prior(device_rows, latest_device_day - timedelta(days=30))

    dev_7_abs, dev_7_pct = growth(
        latest_device, prior_7, "total_devices"
    )
    dev_30_abs, dev_30_pct = growth(
        latest_device, prior_30, "total_devices"
    )
    op_30_abs, op_30_pct = growth(
        latest_device, prior_30, "total_operational"
    )

    total_devices = latest_device["total_devices"]
    operational = latest_device["total_operational"]
    operational_ratio = (
        operational / total_devices * 100.0 if total_devices else 0.0
    )
    productivity = latest_gb / operational if operational else None

    collected = stamp()

    device_history = {
        "schema_version": 1,
        "source": DEVICE_HISTORY_URL,
        "status": "live",
        "count": len(device_rows),
        "data_as_of": latest_device["observation_date"],
        "observed_at_utc": latest_device["observed_at_utc"],
        "collected_at_utc": collected,
        "data": device_rows,
    }

    monthly_rows = [
        {
            "month": month.isoformat(),
            "network_offload_gb": monthly[month]["gb"],
        }
        for month in complete_months
    ]
    offload_monthly = {
        "schema_version": 1,
        "source": OFFLOAD_URL,
        "status": "live",
        "count": len(monthly_rows),
        "data_as_of": latest_month.isoformat(),
        "collected_at_utc": collected,
        "data": monthly_rows,
    }

    network_state = {
        "schema_version": 1,
        "status": "live",
        "status_reason": (
            "Live offload and device API responses validated. "
            "Current-day offload is excluded until the UTC day closes."
        ),
        "collected_at_utc": collected,
        "offload": {
            "status": "live",
            "data_as_of": latest_day.isoformat(),
            "latest_daily_offload_gb": latest_gb,
            "avg_daily_offload_gb_30d": avg_30,
            "all_time_network_offload_gb": all_time,
            "latest_complete_month": latest_month.isoformat(),
            "latest_complete_month_offload_gb": latest_month_gb,
            "latest_month_mom_growth_pct": mom,
            "source": OFFLOAD_URL,
        },
        "devices": {
            "status": "live",
            "data_as_of": latest_device["observation_date"],
            "observed_at_utc": latest_device["observed_at_utc"],
            "total_devices": total_devices,
            "operational_devices": operational,
            "operational_ratio_pct": operational_ratio,
            "device_growth_7d_absolute": dev_7_abs,
            "device_growth_7d_pct": dev_7_pct,
            "device_growth_30d_absolute": dev_30_abs,
            "device_growth_30d_pct": dev_30_pct,
            "operational_growth_30d_absolute": op_30_abs,
            "operational_growth_30d_pct": op_30_pct,
            "source": DEVICE_LATEST_URL,
        },
        "productivity": {
            "latest_offload_gb_per_operational_device": productivity,
        },
        "freshness": {
            "offload_data_date": latest_day.isoformat(),
            "device_data_date": latest_device["observation_date"],
            "source_mode": "measured_live",
            "upstream_health": "healthy",
            "collected_at_utc": collected,
        },
    }

    return network_state, device_history, offload_monthly


def replace_network_outputs(network_state, device_history, offload_monthly):
    outputs = {
        NETWORK_STATE: network_state,
        DEVICE_HISTORY: device_history,
        OFFLOAD_MONTHLY: offload_monthly,
    }
    backups = {
        path: path.read_bytes() if path.exists() else None
        for path in outputs
    }
    try:
        for path, payload in outputs.items():
            save_json(path, payload)
    except Exception:
        for path, content in backups.items():
            if content is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                path.write_bytes(content)
        raise


def mark_attempt(status: str, error: str | None = None, success=None):
    state = load_json(REFRESH_STATE, {"schema_version": 1, "queries": {}})
    external = state.setdefault("external_sources", {})
    previous = external.get("network", {})
    record = {
        **previous,
        "last_attempt_utc": stamp(),
        "status": status,
    }
    record["last_error"] = error
    if success:
        record.update(success)
        record["last_success_utc"] = record["last_attempt_utc"]
    external["network"] = record
    save_json(REFRESH_STATE, state)


def main() -> int:
    refresh_state = load_json(
        REFRESH_STATE,
        {"schema_version": 1, "queries": {}},
    )
    previous = (
        refresh_state.get("external_sources", {})
        .get("network", {})
    )
    cadence = network_cadence_minutes()

    if not due(previous.get("last_attempt_utc"), cadence):
        print(
            "Network API refresh not due; preserving current network state "
            f"(cadence {cadence} minutes)."
        )
        return 0

    existing_devices = load_json(DEVICE_HISTORY, {"data": []})

    try:
        offload_payload = fetch_json(OFFLOAD_URL)
        latest_payload = fetch_json(DEVICE_LATEST_URL)
        history_payload = fetch_json(DEVICE_HISTORY_URL)

        normalized_offload = normalize_offload(offload_payload)
        device_rows, latest_device = normalize_devices(
            latest_payload,
            history_payload,
            existing_devices,
        )

        network_state, device_history, offload_monthly = build_outputs(
            normalized_offload,
            device_rows,
            latest_device,
        )

        replace_network_outputs(
            network_state,
            device_history,
            offload_monthly,
        )

        mark_attempt(
            "live",
            success={
                "offload_data_as_of":
                    network_state["offload"]["data_as_of"],
                "device_data_as_of":
                    network_state["devices"]["data_as_of"],
                "device_observed_at_utc":
                    network_state["devices"]["observed_at_utc"],
            },
        )

        print("=== XNET NETWORK LIVE SYNC COMPLETE ===")
        print(
            "Offload:",
            network_state["offload"]["latest_daily_offload_gb"],
            "GB on",
            network_state["offload"]["data_as_of"],
        )
        print(
            "30d avg:",
            f"{network_state['offload']['avg_daily_offload_gb_30d']:.2f}",
            "GB/day",
        )
        print(
            "All-time completed-day offload:",
            network_state["offload"]["all_time_network_offload_gb"],
            "GB",
        )
        print(
            "Devices:",
            network_state["devices"]["total_devices"],
            "total /",
            network_state["devices"]["operational_devices"],
            "operational",
        )
        print(
            "Device observation:",
            network_state["devices"]["observed_at_utc"],
        )
        return 0

    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        message = str(exc)
        mark_attempt("preserved_last_good", error=message)
        print(
            "WARNING: network API refresh unavailable or invalid; "
            "preserving last-good network files:",
            message,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
