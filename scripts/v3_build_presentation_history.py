#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "presentation"
HISTORY = DATA / "history"

OUT.mkdir(parents=True, exist_ok=True)


def load(path):
    return json.loads(path.read_text())


def save(name, obj):
    (OUT / name).write_text(
        json.dumps(obj, indent=2) + "\n"
    )


def D(value):
    if value in (None, ""):
        return None
    return Decimal(str(value))


def month_of(value):
    return str(value)[:7] + "-01"


def avg(values):
    values = [D(v) for v in values if v is not None]
    if not values:
        return None
    return sum(values) / Decimal(len(values))


# ---------------------------------------------------------
# Shared source maps
# ---------------------------------------------------------
revenue_obj = load(DATA / "xnet_revenue_monthly.json")
revenue_rows = revenue_obj["data"]

revenue_by_month = {
    month_of(row["month"]): row
    for row in revenue_rows
}

offload_obj = load(DATA / "network/offload_monthly.json")
offload_by_month = {
    month_of(row["month"]): D(row["network_offload_gb"])
    for row in offload_obj["data"]
}

device_obj = load(DATA / "network/device_history.json")
device_group = defaultdict(
    lambda: {
        "total": [],
        "operational": [],
    }
)

for row in device_obj["data"]:
    month = month_of(row["observation_date"])
    device_group[month]["total"].append(row["total_devices"])
    device_group[month]["operational"].append(row["total_operational"])

device_monthly = {}
for month, values in device_group.items():
    device_monthly[month] = {
        "total_devices": avg(values["total"]),
        "total_operational": avg(values["operational"]),
    }


# ---------------------------------------------------------
# NETWORK HISTORY
# ---------------------------------------------------------
months = sorted(
    set(offload_by_month)
    | set(device_monthly)
    | set(revenue_by_month)
)

network_rows = []

for month in months:
    offload = offload_by_month.get(month)
    dev = device_monthly.get(month, {})
    total = dev.get("total_devices")
    operational = dev.get("total_operational")

    rev = revenue_by_month.get(month, {})
    projected = D(
        rev.get("wifi_revenue_projected_usd")
    )

    productivity = None
    if (
        offload is not None
        and operational is not None
        and operational > 0
    ):
        productivity = offload / operational

    network_rows.append(
        {
            "month": month,
            "network_offload_gb":
                float(offload) if offload is not None else None,
            "total_devices":
                float(total) if total is not None else None,
            "total_operational":
                float(operational) if operational is not None else None,
            "offload_per_operational_device":
                float(productivity)
                if productivity is not None
                else None,
            "projected_wifi_revenue_usd":
                float(projected)
                if projected is not None
                else None,
            "indexed_operational_devices": None,
            "indexed_offload": None,
            "indexed_revenue": None,
            "unused_series": None,
        }
    )

common = [
    row
    for row in network_rows
    if row["network_offload_gb"]
    and row["total_operational"]
    and row["projected_wifi_revenue_usd"]
]

if common:
    base = common[0]
    for row in network_rows:
        if row["total_operational"] is not None:
            row["indexed_operational_devices"] = (
                row["total_operational"]
                / base["total_operational"]
                * 100.0
            )
        if row["network_offload_gb"] is not None:
            row["indexed_offload"] = (
                row["network_offload_gb"]
                / base["network_offload_gb"]
                * 100.0
            )
        if row["projected_wifi_revenue_usd"] is not None:
            row["indexed_revenue"] = (
                row["projected_wifi_revenue_usd"]
                / base["projected_wifi_revenue_usd"]
                * 100.0
            )

save(
    "network_history.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "index_base_month":
            common[0]["month"] if common else None,
        "data": network_rows,
    },
)


# ---------------------------------------------------------
# COMMERCIAL HISTORY
# ---------------------------------------------------------
defi = load(DATA / "xnet_defillama_revenue.json")

settled_by_service = defaultdict(Decimal)
for row in defi.get("data", []):
    month = month_of(row["service_month"])
    settled_by_service[month] += D(row["fees_usd"]) or Decimal("0")

dated_cash_by_month = defaultdict(Decimal)
for row in defi.get("settlements", []):
    pdate = row.get("payment_received_date")
    amount = D(row.get("payment_received_usd"))
    if pdate and amount is not None:
        dated_cash_by_month[month_of(pdate)] += amount

commercial_months = sorted(
    set(revenue_by_month)
    | set(dated_cash_by_month)
)

cum_projected = Decimal("0")
cum_cash = Decimal("0")
commercial_rows = []

for month in commercial_months:
    src = revenue_by_month.get(month, {})

    projected = D(src.get("wifi_revenue_projected_usd"))
    service_gb = D(src.get("gb_per_month"))
    rate = D(src.get("blended_rate_per_gb_projected_usd"))
    settled = settled_by_service.get(month)
    cash = dated_cash_by_month.get(month)
    projected_bbb = D(src.get("projected_buy_burn_usd"))
    transferred_bbb = D(src.get("transferred_to_buy_burn_usd"))

    if projected is not None:
        cum_projected += projected
    if cash is not None:
        cum_cash += cash

    dev = device_monthly.get(month, {})
    operational = dev.get("total_operational")

    rev_per_device = None
    if (
        projected is not None
        and operational is not None
        and operational > 0
    ):
        rev_per_device = projected / operational

    commercial_rows.append(
        {
            "month": month,
            "wifi_revenue_projected_usd":
                float(projected) if projected is not None else None,
            "wifi_payment_received_usd":
                float(settled) if settled is not None else None,
            "dated_cash_received_usd":
                float(cash) if cash is not None else None,
            "cumulative_projected_wifi_revenue_usd":
                float(cum_projected),
            "cumulative_dated_cash_received_usd":
                float(cum_cash),
            "blended_rate_per_gb_projected_usd":
                float(rate) if rate is not None else None,
            "projected_wifi_revenue_per_operational_device":
                float(rev_per_device)
                if rev_per_device is not None
                else None,
            "projected_buy_burn_usd":
                float(projected_bbb)
                if projected_bbb is not None
                else None,
            "transferred_to_buy_burn_usd":
                float(transferred_bbb)
                if transferred_bbb is not None
                else None,
            "unused_series_1": None,
            "unused_series_2": None,
        }
    )

save(
    "commercial_history.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "data": commercial_rows,
    },
)


# ---------------------------------------------------------
# BURN HISTORY + CURRENT USDC HISTORY
# ---------------------------------------------------------
seed_path = HISTORY / "bbb_burn_history_seed.csv"
burn_by_day = {}

if seed_path.exists():
    with seed_path.open() as f:
        for row in csv.DictReader(f):
            burn_by_day[row["day"]] = {
                "xnet_burned": D(row["xnet_burned"]) or Decimal("0"),
                "burn_transactions": int(row["burn_transactions"] or 0),
            }

# The V2 seed is authoritative through the governed checkpoint.
# Post-checkpoint burns come directly from the same canonical XNET
# transfer file used by v3_reduce_chain.py, so the historical charts
# reconcile to the current chain-state burn total without another
# Dune scan.
checkpoint = load(
    ROOT / "config/xnet_bbb_burn_checkpoint.json"
)

checkpoint_day = str(
    checkpoint["checkpoint_date"]
)[:10]

protocol = load(
    ROOT / "config/xnet_protocol_config.json"
)

bbb_wallet = protocol["primary_bbb_wallet"]

post_checkpoint = defaultdict(
    lambda: {
        "amount": Decimal("0"),
        "txs": set(),
    }
)

transfer_path = DATA / "canonical/xnet_transfers.csv"

with transfer_path.open() as f:
    for row in csv.DictReader(f):
        if (
            str(row.get("action") or "").lower()
            != "burn"
        ):
            continue

        if (
            str(row.get("from_owner") or "")
            != bbb_wallet
        ):
            continue

        day = str(
            row.get("block_date")
            or row.get("block_time")
            or ""
        )[:10]

        if (
            not day
            or day <= checkpoint_day
        ):
            continue

        amount = D(
            row.get("amount_xnet")
        ) or Decimal("0")

        post_checkpoint[day]["amount"] += amount

        tx_id = str(
            row.get("tx_id")
            or row.get("event_id")
            or ""
        )

        if tx_id:
            post_checkpoint[day]["txs"].add(
                tx_id
            )

for day, payload in post_checkpoint.items():
    burn_by_day[day] = {
        "xnet_burned":
            payload["amount"],

        "burn_transactions":
            len(
                payload["txs"]
            ),
    }

chain = load(DATA / "current/xnet_chain_snapshot.json")
current_total = D(chain["verified_bbb_burned_xnet"])
current_circulating = D(chain["circulating_supply_xnet"])
max_supply = D(chain["published_max_supply_xnet"])

usdc_obj = (
    load(HISTORY / "bbb_wallet_usdc_daily.json")
    if (HISTORY / "bbb_wallet_usdc_daily.json").exists()
    else {"data": []}
)

usdc_by_day = {
    str(row["day"])[:10]:
        D(row["bbb_wallet_usdc_balance"])
    for row in usdc_obj.get("data", [])
    if row.get("day")
}

all_days = sorted(set(burn_by_day) | set(usdc_by_day))

running = Decimal("0")
burn_rows = []

for day in all_days:
    burn = burn_by_day.get(
        day,
        {
            "xnet_burned": Decimal("0"),
            "burn_transactions": 0,
        },
    )
    running += burn["xnet_burned"]

    burn_rows.append(
        {
            "day": day,
            "xnet_burned": float(burn["xnet_burned"]),
            "burn_transactions": burn["burn_transactions"],
            "cumulative_xnet_burned": float(running),
            "bbb_wallet_usdc_balance":
                float(usdc_by_day[day])
                if day in usdc_by_day
                else None,
        }
    )

if burn_rows:
    latest_data_day = max(row["day"] for row in burn_rows)
    latest_burn_days = [
        row["day"]
        for row in burn_rows
        if row["xnet_burned"] > 0
    ]
    latest_burn_day = (
        max(latest_burn_days)
        if latest_burn_days
        else None
    )

    end = date.fromisoformat(latest_data_day)
    start7 = end - timedelta(days=6)
    start30 = end - timedelta(days=29)

    burn7 = sum(
        Decimal(str(row["xnet_burned"]))
        for row in burn_rows
        if start7 <= date.fromisoformat(row["day"]) <= end
    )

    burn30 = sum(
        Decimal(str(row["xnet_burned"]))
        for row in burn_rows
        if start30 <= date.fromisoformat(row["day"]) <= end
    )

    total_txs = sum(
        int(row["burn_transactions"])
        for row in burn_rows
    )
else:
    latest_data_day = None
    latest_burn_day = None
    burn7 = Decimal("0")
    burn30 = Decimal("0")
    total_txs = 0

if current_total is not None and abs(running - current_total) > Decimal("0.01"):
    raise RuntimeError(
        "Burn history does not reconcile to current chain total: "
        f"history={running} chain={current_total}"
    )

pct_max = (
    current_total / max_supply * 100
    if max_supply
    else None
)

pct_circ = (
    current_total / current_circulating * 100
    if current_circulating
    else None
)

save(
    "burn_history.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "summary": {
            "latest_data_day": latest_data_day,
            "latest_burn_day": latest_burn_day,
            "burn_last_7d_xnet": float(burn7),
            "burn_last_30d_xnet": float(burn30),
            "total_xnet_burned": float(current_total),
            "total_burn_transactions": total_txs,
            "verified_bbb_pct_max_supply":
                float(pct_max) if pct_max is not None else None,
            "verified_bbb_pct_circulating_supply":
                float(pct_circ) if pct_circ is not None else None,
        },
        "data": burn_rows,
    },
)


# ---------------------------------------------------------
# TOKENOMICS HISTORY
# ---------------------------------------------------------
burn_monthly = defaultdict(Decimal)
for row in burn_rows:
    burn_monthly[month_of(row["day"])] += Decimal(
        str(row["xnet_burned"])
    )

supply_rows = load(DATA / "xnet_supply_history.json")
supply_monthly = {}
supply_change = defaultdict(Decimal)

for row in supply_rows:
    month = month_of(row["day"])
    supply_monthly[month] = D(row["circulating_supply_xnet"])
    supply_change[month] += D(row["circulation_change_xnet"]) or Decimal("0")

token_months = sorted(
    set(revenue_by_month)
    | set(burn_monthly)
    | set(supply_monthly)
)

token_rows = []

for month in token_months:
    src = revenue_by_month.get(month, {})
    emissions = D(src.get("total_emitted_tokens"))
    offload = offload_by_month.get(month)
    efficiency = None

    if (
        emissions is not None
        and offload is not None
        and offload > 0
    ):
        efficiency = emissions / offload

    token_rows.append(
        {
            "month": month,
            "scheduled_emissions_xnet":
                float(emissions) if emissions is not None else None,
            "xnet_burned":
                float(burn_monthly[month])
                if month in burn_monthly
                else None,
            "emissions_per_network_gb":
                float(efficiency)
                if efficiency is not None
                else None,
            "circulating_supply_xnet":
                float(supply_monthly[month])
                if month in supply_monthly
                else None,
            "circulation_change_xnet":
                float(supply_change[month])
                if month in supply_change
                else None,
        }
    )

save(
    "tokenomics_history.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "emissions_source":
            "XNET revenue sheet total_emitted_tokens schedule",
        "data": token_rows,
    },
)


# ---------------------------------------------------------
# HOLDER DISTRIBUTION
# ---------------------------------------------------------
buckets = [
    ("< 1 XNET", Decimal("0"), Decimal("1")),
    ("1–100 XNET", Decimal("1"), Decimal("100")),
    ("100–1,000 XNET", Decimal("100"), Decimal("1000")),
    ("1,000–10,000 XNET", Decimal("1000"), Decimal("10000")),
    ("10,000–100,000 XNET", Decimal("10000"), Decimal("100000")),
    ("≥ 100,000 XNET", Decimal("100000"), None),
]

counts = [0 for _ in buckets]

with (DATA / "derived/xnet_owner_balances.csv").open() as f:
    for row in csv.DictReader(f):
        bal = D(row["xnet_balance"])
        if bal is None or bal <= 0:
            continue

        for i, (_label, lo, hi) in enumerate(buckets):
            if bal >= lo and (hi is None or bal < hi):
                counts[i] += 1
                break

holder_rows = [
    {
        "bucket_order": i + 1,
        "bucket": label,
        "holder_count": counts[i],
    }
    for i, (label, _lo, _hi) in enumerate(buckets)
]

save(
    "holder_distribution.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "data": holder_rows,
    },
)


# ---------------------------------------------------------
# CURRENT DEX POOLS
# ---------------------------------------------------------
pool_file = DATA / "canonical/market_pair_snapshots.csv"
pool_rows = []

if pool_file.exists():
    with pool_file.open() as f:
        raw = list(csv.DictReader(f))

    if raw:
        time_keys = [
            "snapshot_utc",
            "observed_at_utc",
            "fetched_at_utc",
        ]
        tkey = next(
            (key for key in time_keys if key in raw[0]),
            None,
        )

        if tkey:
            latest = max(
                str(row.get(tkey) or "")
                for row in raw
            )
            raw = [
                row
                for row in raw
                if str(row.get(tkey) or "") == latest
            ]

        total_liq = sum(
            D(row.get("liquidity_usd")) or Decimal("0")
            for row in raw
        )

        for row in raw:
            base = row.get("base_symbol") or ""
            quote = row.get("quote_symbol") or ""

            if base.upper() == "XNET":
                pair = f"XNET / {quote}"
            elif quote.upper() == "XNET":
                pair = f"{base} / XNET"
            else:
                pair = f"{base} / {quote}".strip(" /")

            liq = D(row.get("liquidity_usd")) or Decimal("0")

            pool_rows.append(
                {
                    "dex": row.get("dex_id") or row.get("dex") or "",
                    "pair": pair,
                    "pair_address": row.get("pair_address") or "",
                    "liquidity_usd": float(liq),
                    "volume_h24_usd":
                        float(D(row.get("volume_h24_usd") or row.get("volume_24h_usd")) or 0),
                    "buys_h24":
                        int(D(row.get("buys_h24")) or 0),
                    "sells_h24":
                        int(D(row.get("sells_h24")) or 0),
                    "liquidity_share_pct":
                        float(liq / total_liq * 100)
                        if total_liq > 0
                        else None,
                }
            )

pool_rows.sort(
    key=lambda row: row["liquidity_usd"],
    reverse=True,
)

save(
    "market_pools.json",
    {
        "schema_version": 1,
        "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "data": pool_rows,
    },
)

print("Presentation history built:")
for p in sorted(OUT.glob("*.json")):
    obj = load(p)
    print(
        " ",
        p.name,
        len(obj.get("data", [])),
        "rows",
    )
