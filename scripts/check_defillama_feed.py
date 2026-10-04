#!/usr/bin/env python3
"""Validate the published feed and report DeFiLlama ingestion independently."""
import json
import urllib.request
from decimal import Decimal

FEED = "https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json"

def main():
    with urllib.request.urlopen(FEED, timeout=45) as response:
        feed = json.load(response)
    total = sum((Decimal(str(row["fees_usd"])) for row in feed["data"]), Decimal(0))
    assert total == Decimal(str(feed["totals"]["recognized_service_revenue_usd"]))
    assert feed["count"] == len(feed["data"]) == len({row["date"] for row in feed["data"]})
    print(f"Published feed: {feed['count']} service months; USD {total}")
    for slug in ("xnet", "xnet-mobile"):
        for metric in ("dailyFees", "dailyRevenue"):
            url = f"https://api.llama.fi/summary/fees/{slug}?dataType={metric}"
            try:
                with urllib.request.urlopen(url, timeout=45) as response:
                    data = json.load(response)
                print(json.dumps({"slug": slug, "metric": metric, "name": data.get("name"), "module": data.get("module"),
                    "totalAllTime": data.get("totalAllTime"), "total24h": data.get("total24h"), "total30d": data.get("total30d")}))
            except urllib.error.HTTPError as error:
                print(json.dumps({"slug": slug, "metric": metric, "http_status": error.code, "response": error.read().decode()[:400]}))

if __name__ == "__main__":
    main()
