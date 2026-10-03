#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import urllib.request
import urllib.error

URL = "https://xnet-offload-scraper.vercel.app/api/data"
OUT = "data/xnet_offload_api.json"

req = urllib.request.Request(
    URL,
    headers={
        "User-Agent": "XNET-Community-Data/1.0"
    },
)

try:
    with urllib.request.urlopen(req, timeout=60) as response:
        status = response.status
        body = response.read()
except urllib.error.HTTPError as e:
    print(f"ERROR: upstream returned HTTP {e.code}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: failed to fetch upstream: {e}")
    sys.exit(1)

if status != 200:
    print(f"ERROR: upstream returned HTTP {status}")
    sys.exit(1)

try:
    payload = json.loads(body)
except Exception as e:
    print(f"ERROR: response is not valid JSON: {e}")
    sys.exit(1)

if not payload:
    print("ERROR: upstream returned empty JSON")
    sys.exit(1)

os.makedirs(os.path.dirname(OUT), exist_ok=True)

fd, tmp = tempfile.mkstemp(
    prefix="xnet_offload_",
    suffix=".json",
    dir=os.path.dirname(OUT),
)

try:
    with os.fdopen(fd, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, OUT)
except Exception:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    raise

print(f"OK: mirrored {len(body):,} bytes to {OUT}")
