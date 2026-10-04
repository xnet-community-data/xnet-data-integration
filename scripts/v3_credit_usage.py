#!/usr/bin/env python3
"""Record account credit usage using Dune's unmetered metadata endpoint."""
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = Path(os.environ.get("DUNE_USAGE_STATE_PATH", ROOT / "data/current/v3_credit_usage.json"))

def summarize(previous, periods, observed_at):
    counters = dict(previous.get("period_counters", {}))
    for period in periods:
        value = float(period["credits_used"])
        if value < 0 or not math.isfinite(value):
            raise ValueError("Invalid credit counter")
        counters[period["start_date"]] = value
    total = sum(counters.values())
    samples = previous.get("samples", []) + [{"utc": observed_at.isoformat(), "credits": total}]
    cutoff = observed_at - timedelta(days=3)
    older = [s for s in samples if datetime.fromisoformat(s["utc"]) <= cutoff]
    baseline = older[-1] if older else samples[0]
    elapsed_days = (observed_at - datetime.fromisoformat(baseline["utc"])).total_seconds() / 86400
    delta = total - baseline["credits"]
    ready = elapsed_days >= 23 / 24 and delta >= 0
    average = delta / elapsed_days if ready else None
    return {"schema_version": 1, "observed_at_utc": observed_at.isoformat(),
        "period_counters": counters, "samples": [s for s in samples if datetime.fromisoformat(s["utc"]) >= observed_at - timedelta(days=8)],
        "window_start_utc": baseline["utc"], "window_days": elapsed_days,
        "credits_in_window": delta, "average_credits_per_day": average,
        "threshold_credits_per_day": 100, "alert": ready and average > 100,
        "status": "ready" if ready else "collecting_baseline"}

def main():
    now = datetime.now(timezone.utc)
    payload = {"start_date": (now.date() - timedelta(days=120)).isoformat(),
        "end_date": (now.date() + timedelta(days=1)).isoformat()}
    request = urllib.request.Request("https://api.dune.com/api/v1/usage",
        data=json.dumps(payload).encode(), headers={"X-Dune-Api-Key": os.environ["DUNE_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        usage = json.load(response)
    periods = usage.get("billing_periods", usage.get("billingPeriods", []))
    if not periods:
        raise RuntimeError("No billing counters returned")
    previous = json.loads(PATH.read_text()) if PATH.exists() else {}
    result = summarize(previous, periods, now)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("status", "average_credits_per_day", "alert")}))

if __name__ == "__main__":
    main()
