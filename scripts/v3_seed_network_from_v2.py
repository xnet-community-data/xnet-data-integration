#!/usr/bin/env python3

from __future__ import annotations

import json
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

RECOVERY = ROOT / "state/v2_network_recovery"

DEVICE_Q = RECOVERY / "query_8876224_cached.json"
LATEST_Q = RECOVERY / "query_8876668_cached.json"
MONTHLY_Q = RECOVERY / "query_8876739_cached.json"


def dune_rows(path: Path) -> list[dict]:
    d = json.loads(path.read_text())

    result = d.get("result") or {}

    rows = (
        result.get("rows")
        or d.get("rows")
        or []
    )

    if not rows:
        raise RuntimeError(
            f"No cached rows found in {path}"
        )

    return rows


def iso_now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_day(v):
    return date.fromisoformat(
        str(v)[:10]
    )


def nearest_prior(
    rows: list[dict],
    target: date,
) -> dict | None:

    eligible = [
        r
        for r in rows
        if parse_day(
            r["observation_date"]
        ) <= target
    ]

    if not eligible:
        return None

    return max(
        eligible,
        key=lambda r:
            r["observation_date"],
    )


devices = dune_rows(DEVICE_Q)
latest_rows = dune_rows(LATEST_Q)
monthly = dune_rows(MONTHLY_Q)

if len(latest_rows) != 1:
    raise RuntimeError(
        "Expected exactly one cached network snapshot"
    )

latest = latest_rows[0]

# --------------------------------------------------
# Normalize device history oldest -> newest
# --------------------------------------------------

devices = sorted(
    devices,
    key=lambda r: (
        r["observation_date"],
        r["observed_at_utc"],
    ),
)

latest_device = devices[-1]

if int(
    latest_device["total_devices"]
) != int(
    latest["latest_total_devices"]
):
    raise RuntimeError(
        "Device QA mismatch between history and snapshot"
    )

if int(
    latest_device["total_operational"]
) != int(
    latest["latest_operational_devices"]
):
    raise RuntimeError(
        "Operational-device QA mismatch"
    )


# --------------------------------------------------
# Device growth
# --------------------------------------------------

latest_date = parse_day(
    latest_device["observation_date"]
)

prior_7 = nearest_prior(
    devices,
    latest_date - timedelta(days=7),
)

prior_30 = nearest_prior(
    devices,
    latest_date - timedelta(days=30),
)


def growth(current, prior, field):
    if prior is None:
        return None, None

    c = Decimal(str(current[field]))
    p = Decimal(str(prior[field]))

    absolute = c - p

    pct = (
        absolute / p * Decimal("100")
        if p != 0
        else None
    )

    return absolute, pct


dev_7_abs, dev_7_pct = growth(
    latest_device,
    prior_7,
    "total_devices",
)

dev_30_abs, dev_30_pct = growth(
    latest_device,
    prior_30,
    "total_devices",
)

op_30_abs, op_30_pct = growth(
    latest_device,
    prior_30,
    "total_operational",
)


# --------------------------------------------------
# Monthly offload oldest -> newest
# --------------------------------------------------

monthly = sorted(
    monthly,
    key=lambda r: r["month"],
)

latest_month = monthly[-1]

prev_month = (
    monthly[-2]
    if len(monthly) >= 2
    else None
)

monthly_mom_pct = None

if prev_month:
    a = Decimal(
        str(
            latest_month[
                "network_offload_gb"
            ]
        )
    )

    b = Decimal(
        str(
            prev_month[
                "network_offload_gb"
            ]
        )
    )

    if b != 0:
        monthly_mom_pct = (
            (a - b)
            / b
            * Decimal("100")
        )


# --------------------------------------------------
# Current metrics
# --------------------------------------------------

total_devices = Decimal(
    str(
        latest[
            "latest_total_devices"
        ]
    )
)

operational = Decimal(
    str(
        latest[
            "latest_operational_devices"
        ]
    )
)

latest_daily = Decimal(
    str(
        latest[
            "latest_daily_offload_gb"
        ]
    )
)

operational_ratio = (
    operational
    / total_devices
    * Decimal("100")
    if total_devices
    else Decimal("0")
)

offload_per_operational_device = (
    latest_daily
    / operational
    if operational
    else None
)


device_history = {
    "schema_version": 1,
    "source":
        "recovered_v2_dune_cache_query_8876224",
    "status":
        "stale_fallback",
    "count":
        len(devices),
    "data_as_of":
        latest_device[
            "observation_date"
        ],
    "observed_at_utc":
        latest_device[
            "observed_at_utc"
        ],
    "data":
        devices,
}

offload_monthly = {
    "schema_version": 1,
    "source":
        "recovered_v2_dune_cache_query_8876739",
    "status":
        "stale_fallback",
    "count":
        len(monthly),
    "data_as_of":
        latest_month["month"],
    "data":
        monthly,
}

state = {
    "schema_version": 1,

    "status":
        "stale_fallback",

    "status_reason":
        (
            "Upstream offload and device APIs "
            "currently return HTTP 402. "
            "Last known-good values were recovered "
            "from cached V2 dashboard results."
        ),

    "recovered_at_utc":
        iso_now(),

    "offload": {
        "status":
            "stale_fallback",

        "data_as_of":
            latest[
                "latest_offload_day"
            ],

        "latest_daily_offload_gb":
            latest[
                "latest_daily_offload_gb"
            ],

        "avg_daily_offload_gb_30d":
            latest[
                "avg_daily_offload_gb_30d"
            ],

        "all_time_network_offload_gb":
            latest[
                "all_time_network_offload_gb"
            ],

        "latest_complete_month":
            latest_month["month"],

        "latest_complete_month_offload_gb":
            latest_month[
                "network_offload_gb"
            ],

        "latest_month_mom_growth_pct":
            (
                None
                if monthly_mom_pct is None
                else float(monthly_mom_pct)
            ),

        "source":
            "V2 cached Dune results"
    },

    "devices": {
        "status":
            "stale_fallback",

        "data_as_of":
            latest_device[
                "observation_date"
            ],

        "observed_at_utc":
            latest_device[
                "observed_at_utc"
            ],

        "total_devices":
            latest[
                "latest_total_devices"
            ],

        "operational_devices":
            latest[
                "latest_operational_devices"
            ],

        "operational_ratio_pct":
            float(operational_ratio),

        "device_growth_7d_absolute":
            (
                None
                if dev_7_abs is None
                else float(dev_7_abs)
            ),

        "device_growth_7d_pct":
            (
                None
                if dev_7_pct is None
                else float(dev_7_pct)
            ),

        "device_growth_30d_absolute":
            (
                None
                if dev_30_abs is None
                else float(dev_30_abs)
            ),

        "device_growth_30d_pct":
            (
                None
                if dev_30_pct is None
                else float(dev_30_pct)
            ),

        "operational_growth_30d_absolute":
            (
                None
                if op_30_abs is None
                else float(op_30_abs)
            ),

        "operational_growth_30d_pct":
            (
                None
                if op_30_pct is None
                else float(op_30_pct)
            ),

        "source":
            "V2 cached Dune results"
    },

    "productivity": {
        "latest_offload_gb_per_operational_device":
            (
                None
                if offload_per_operational_device
                is None
                else float(
                    offload_per_operational_device
                )
            )
    },

    "freshness": {
        "offload_data_date":
            latest[
                "latest_offload_day"
            ],

        "device_data_date":
            latest_device[
                "observation_date"
            ],

        "source_mode":
            "cached_last_known_good",

        "upstream_health":
            "unavailable_http_402"
    }
}


out_device = (
    ROOT
    / "data/network/device_history.json"
)

out_offload = (
    ROOT
    / "data/network/offload_monthly.json"
)

out_state = (
    ROOT
    / "data/current/xnet_network_state.json"
)

out_device.write_text(
    json.dumps(
        device_history,
        indent=2,
    ) + "\n"
)

out_offload.write_text(
    json.dumps(
        offload_monthly,
        indent=2,
    ) + "\n"
)

out_state.write_text(
    json.dumps(
        state,
        indent=2,
    ) + "\n"
)


print(
    "=== NETWORK FALLBACK SEEDED ==="
)

print(
    "Latest offload:",
    latest["latest_daily_offload_gb"],
    "GB on",
    latest["latest_offload_day"],
)

print(
    "30d avg:",
    latest["avg_daily_offload_gb_30d"],
    "GB/day",
)

print(
    "All-time:",
    latest[
        "all_time_network_offload_gb"
    ],
    "GB",
)

print(
    "Devices:",
    latest["latest_total_devices"],
)

print(
    "Operational:",
    latest[
        "latest_operational_devices"
    ],
)

print(
    "Device data date:",
    latest_device["observation_date"],
)

print(
    "Device history rows:",
    len(devices),
)

print(
    "Monthly offload rows:",
    len(monthly),
)
