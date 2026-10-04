#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

MINT = "xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L"

URL = (
    "https://api.dexscreener.com/"
    f"token-pairs/v1/solana/{MINT}"
)

PAIR_HISTORY = (
    ROOT / "data/canonical/market_pair_snapshots.csv"
)

MARKET_HISTORY = (
    ROOT / "data/derived/market_snapshots.csv"
)

CURRENT_STATE = (
    ROOT / "data/current/xnet_market_state.json"
)


PAIR_FIELDS = [
    "snapshot_utc",
    "fetched_at_utc",
    "pair_address",
    "dex_id",
    "xnet_position",
    "base_symbol",
    "base_mint",
    "quote_symbol",
    "quote_mint",
    "price_usd",
    "price_native",
    "liquidity_usd",
    "liquidity_base",
    "liquidity_quote",
    "volume_m5_usd",
    "volume_h1_usd",
    "volume_h6_usd",
    "volume_h24_usd",
    "buys_m5",
    "sells_m5",
    "buys_h1",
    "sells_h1",
    "buys_h6",
    "sells_h6",
    "buys_h24",
    "sells_h24",
    "price_change_m5_pct",
    "price_change_h1_pct",
    "price_change_h6_pct",
    "price_change_h24_pct",
    "provider_fdv_usd",
    "provider_market_cap_usd",
    "pair_created_at_ms",
]

MARKET_FIELDS = [
    "snapshot_utc",
    "fetched_at_utc",
    "xnet_price_usd",
    "reconstructed_market_cap_usd",
    "provider_market_cap_usd",
    "provider_fdv_usd",
    "total_dex_liquidity_usd",
    "total_dex_volume_h24_usd",
    "observed_buys_h24",
    "observed_sells_h24",
    "liquidity_pool_count",
    "primary_pool_address",
    "primary_pool_dex",
    "primary_pool_quote_symbol",
    "primary_pool_liquidity_usd",
    "primary_pool_liquidity_share",
    "price_change_h1_pct",
    "price_change_h6_pct",
    "price_change_h24_pct",
]


def d(v):
    if v in (None, ""):
        return Decimal("0")

    try:
        return Decimal(str(v))
    except InvalidOperation:
        return Decimal("0")


def clean(v):
    if v is None:
        return ""
    return str(v).replace("\x00", "").strip()


def nested(obj, *keys, default=None):
    cur = obj

    for key in keys:
        if not isinstance(cur, dict):
            return default

        cur = cur.get(key)

        if cur is None:
            return default

    return cur


def utc_now():
    return datetime.now(timezone.utc)


def floor_15m(dt):
    minute = (dt.minute // 15) * 15

    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


def iso(dt):
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fetch_pairs():
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent":
                "xnet-community-data-v3/1.0",
            "Accept":
                "application/json",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=30,
    ) as r:
        if r.status != 200:
            raise RuntimeError(
                f"DexScreener HTTP {r.status}"
            )

        data = json.load(r)

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected DexScreener response type"
        )

    return data


def normalize_pair(
    p,
    snapshot,
    fetched,
):
    if clean(p.get("chainId")).lower() != "solana":
        return None

    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}

    base_mint = clean(base.get("address"))
    quote_mint = clean(quote.get("address"))

    if base_mint == MINT:
        position = "BASE"
    elif quote_mint == MINT:
        position = "QUOTE"
    else:
        return None

    pair_address = clean(
        p.get("pairAddress")
    )

    if not pair_address:
        return None

    return {
        "snapshot_utc": snapshot,
        "fetched_at_utc": fetched,
        "pair_address": pair_address,
        "dex_id": clean(p.get("dexId")),
        "xnet_position": position,

        "base_symbol":
            clean(base.get("symbol")),
        "base_mint":
            base_mint,

        "quote_symbol":
            clean(quote.get("symbol")),
        "quote_mint":
            quote_mint,

        "price_usd":
            clean(p.get("priceUsd")),
        "price_native":
            clean(p.get("priceNative")),

        "liquidity_usd":
            clean(
                nested(
                    p,
                    "liquidity",
                    "usd",
                )
            ),
        "liquidity_base":
            clean(
                nested(
                    p,
                    "liquidity",
                    "base",
                )
            ),
        "liquidity_quote":
            clean(
                nested(
                    p,
                    "liquidity",
                    "quote",
                )
            ),

        "volume_m5_usd":
            clean(
                nested(
                    p,
                    "volume",
                    "m5",
                )
            ),
        "volume_h1_usd":
            clean(
                nested(
                    p,
                    "volume",
                    "h1",
                )
            ),
        "volume_h6_usd":
            clean(
                nested(
                    p,
                    "volume",
                    "h6",
                )
            ),
        "volume_h24_usd":
            clean(
                nested(
                    p,
                    "volume",
                    "h24",
                )
            ),

        "buys_m5":
            clean(
                nested(
                    p,
                    "txns",
                    "m5",
                    "buys",
                )
            ),
        "sells_m5":
            clean(
                nested(
                    p,
                    "txns",
                    "m5",
                    "sells",
                )
            ),
        "buys_h1":
            clean(
                nested(
                    p,
                    "txns",
                    "h1",
                    "buys",
                )
            ),
        "sells_h1":
            clean(
                nested(
                    p,
                    "txns",
                    "h1",
                    "sells",
                )
            ),
        "buys_h6":
            clean(
                nested(
                    p,
                    "txns",
                    "h6",
                    "buys",
                )
            ),
        "sells_h6":
            clean(
                nested(
                    p,
                    "txns",
                    "h6",
                    "sells",
                )
            ),
        "buys_h24":
            clean(
                nested(
                    p,
                    "txns",
                    "h24",
                    "buys",
                )
            ),
        "sells_h24":
            clean(
                nested(
                    p,
                    "txns",
                    "h24",
                    "sells",
                )
            ),

        "price_change_m5_pct":
            clean(
                nested(
                    p,
                    "priceChange",
                    "m5",
                )
            ),
        "price_change_h1_pct":
            clean(
                nested(
                    p,
                    "priceChange",
                    "h1",
                )
            ),
        "price_change_h6_pct":
            clean(
                nested(
                    p,
                    "priceChange",
                    "h6",
                )
            ),
        "price_change_h24_pct":
            clean(
                nested(
                    p,
                    "priceChange",
                    "h24",
                )
            ),

        "provider_fdv_usd":
            clean(p.get("fdv")),
        "provider_market_cap_usd":
            clean(p.get("marketCap")),

        "pair_created_at_ms":
            clean(p.get("pairCreatedAt")),
    }


def merge_csv(
    path,
    new_rows,
    fields,
    key_fn,
):
    merged = {}

    if path.exists():
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                merged[key_fn(row)] = row

    for row in new_rows:
        merged[key_fn(row)] = row

    rows = list(merged.values())

    rows.sort(
        key=lambda r: (
            r["snapshot_utc"],
            r.get("pair_address", ""),
        )
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fields,
        )
        w.writeheader()
        w.writerows(rows)

    tmp.replace(path)

    return rows


def load_circulating_supply():
    p = (
        ROOT
        / "data/current/xnet_supply_state.json"
    )

    obj = json.loads(p.read_text())

    return d(
        obj[
            "latest_circulating_supply_xnet"
        ]
    )


def build_market_snapshot(
    pair_rows,
    snapshot,
    fetched,
):
    if not pair_rows:
        raise RuntimeError(
            "DexScreener returned no XNET pairs"
        )

    # Remove duplicate pair addresses if the API ever
    # returns the same pool more than once.
    unique = {}

    for row in pair_rows:
        unique[row["pair_address"]] = row

    rows = list(unique.values())

    # Primary pool is the largest observed pool by USD
    # liquidity.
    primary = max(
        rows,
        key=lambda r: d(r["liquidity_usd"]),
    )

    total_liquidity = sum(
        (
            d(r["liquidity_usd"])
            for r in rows
        ),
        Decimal("0"),
    )

    total_volume = sum(
        (
            d(r["volume_h24_usd"])
            for r in rows
        ),
        Decimal("0"),
    )

    buys = sum(
        int(d(r["buys_h24"]))
        for r in rows
    )

    sells = sum(
        int(d(r["sells_h24"]))
        for r in rows
    )

    # DexScreener's pair priceUsd describes the base
    # token. Use the largest-liquidity pool where XNET
    # is BASE for the headline XNET price.
    xnet_base_rows = [
        r for r in rows
        if r["xnet_position"] == "BASE"
        and d(r["price_usd"]) > 0
    ]

    if not xnet_base_rows:
        raise RuntimeError(
            "No XNET-as-base pool with USD price"
        )

    price_pool = max(
        xnet_base_rows,
        key=lambda r: d(r["liquidity_usd"]),
    )

    price = d(price_pool["price_usd"])

    circulating = (
        load_circulating_supply()
    )

    reconstructed_market_cap = (
        price * circulating
    )

    primary_liq = d(
        primary["liquidity_usd"]
    )

    share = (
        primary_liq / total_liquidity
        if total_liquidity > 0
        else Decimal("0")
    )

    return {
        "snapshot_utc": snapshot,
        "fetched_at_utc": fetched,

        "xnet_price_usd":
            str(price),

        "reconstructed_market_cap_usd":
            str(reconstructed_market_cap),

        "provider_market_cap_usd":
            price_pool[
                "provider_market_cap_usd"
            ],

        "provider_fdv_usd":
            price_pool[
                "provider_fdv_usd"
            ],

        "total_dex_liquidity_usd":
            str(total_liquidity),

        "total_dex_volume_h24_usd":
            str(total_volume),

        "observed_buys_h24":
            str(buys),

        "observed_sells_h24":
            str(sells),

        "liquidity_pool_count":
            str(len(rows)),

        "primary_pool_address":
            primary["pair_address"],

        "primary_pool_dex":
            primary["dex_id"],

        "primary_pool_quote_symbol":
            (
                primary["quote_symbol"]
                if primary["xnet_position"] == "BASE"
                else primary["base_symbol"]
            ),

        "primary_pool_liquidity_usd":
            str(primary_liq),

        "primary_pool_liquidity_share":
            str(share),

        "price_change_h1_pct":
            price_pool[
                "price_change_h1_pct"
            ],

        "price_change_h6_pct":
            price_pool[
                "price_change_h6_pct"
            ],

        "price_change_h24_pct":
            price_pool[
                "price_change_h24_pct"
            ],
    }


def main():
    now = utc_now()

    snapshot_dt = floor_15m(now)

    snapshot = iso(snapshot_dt)
    fetched = iso(now)

    raw = fetch_pairs()

    rows = []

    for p in raw:
        row = normalize_pair(
            p,
            snapshot,
            fetched,
        )

        if row is not None:
            rows.append(row)

    if not rows:
        raise RuntimeError(
            "No valid Solana XNET pairs returned"
        )

    market = build_market_snapshot(
        rows,
        snapshot,
        fetched,
    )

    pair_history = merge_csv(
        PAIR_HISTORY,
        rows,
        PAIR_FIELDS,
        key_fn=lambda r: (
            r["snapshot_utc"],
            r["pair_address"],
        ),
    )

    market_history = merge_csv(
        MARKET_HISTORY,
        [market],
        MARKET_FIELDS,
        key_fn=lambda r: (
            r["snapshot_utc"],
        ),
    )

    state = {
        "schema_version": 1,
        **market,
        "source":
            "DexScreener token-pairs API",
        "source_pair_rows":
            len(rows),
        "canonical_pair_snapshot_rows":
            len(pair_history),
        "canonical_market_snapshot_rows":
            len(market_history),
    }

    CURRENT_STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = CURRENT_STATE.with_suffix(
        ".json.tmp"
    )

    tmp.write_text(
        json.dumps(
            state,
            indent=2,
        )
        + "\n"
    )

    tmp.replace(CURRENT_STATE)

    print(
        "=== XNET MARKET SNAPSHOT COMPLETE ==="
    )
    print("Snapshot:", snapshot)
    print("Fetched: ", fetched)
    print("Pools:   ", len(rows))
    print(
        "Price:   $",
        market["xnet_price_usd"],
        sep="",
    )
    print(
        "Market cap reconstructed: $",
        market[
            "reconstructed_market_cap_usd"
        ],
        sep="",
    )
    print(
        "Liquidity: $",
        market[
            "total_dex_liquidity_usd"
        ],
        sep="",
    )
    print(
        "24h volume: $",
        market[
            "total_dex_volume_h24_usd"
        ],
        sep="",
    )
    print(
        "Primary:",
        market["primary_pool_dex"],
        market["primary_pool_quote_symbol"],
        market["primary_pool_address"],
    )


if __name__ == "__main__":
    main()
