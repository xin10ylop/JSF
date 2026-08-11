"""Can this host place a new order at all?

Polymarket restricts order placement by IP. Its own documentation splits
this into three tiers:

  * OFAC-sanctioned (IR, SY, CU, KP, and Crimea/Donetsk/Luhansk) — blocked
    on frontend AND API, positions cannot even be closed;
  * close-only on frontend AND API — 34 jurisdictions, **including the
    United States**. Existing positions can be closed; no new orders;
  * close-only on the FRONTEND ONLY, API unrestricted — Ireland, Japan,
    Malta (sports only), Netherlands.

Reading is unaffected everywhere: books, Gamma and the RTDS oracle all work
from a blocked IP, so a paper bot looks perfectly healthy on a host that
could never place a live order. That is worth finding out before renting
the host, not after wiring up credentials.

    python3 src/check_geoblock.py
"""
import json
import sys
import urllib.request

# API unrestricted despite a frontend restriction (Polymarket's own docs).
API_OPEN = {"IE": "Ireland", "JP": "Japan", "NL": "Netherlands",
            "MT": "Malta (sports only)"}
UA = {"User-Agent": "Mozilla/5.0"}


def main():
    req = urllib.request.Request("https://polymarket.com/api/geoblock",
                                 headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"could not reach the geoblock endpoint: {e}")
        return 2
    cc = d.get("country")
    print(f"ip {d.get('ip')}   country {cc}   region {d.get('region')}   "
          f"blocked {d.get('blocked')}")
    if not d.get("blocked"):
        print("\nOK — new orders may be placed from this host.")
        return 0
    if cc in API_OPEN:
        print(f"\nOK for the API. {API_OPEN[cc]} is close-only on the "
              f"WEBSITE only; Polymarket's docs list its API as "
              f"unrestricted. Trade programmatically, not through the site.")
        return 0
    print(f"\nNOT OK — {cc} is close-only on the frontend AND the API. "
          f"Existing positions can be closed; new orders will be rejected.")
    print("Reading works fine from here, so a paper bot will look healthy "
          "and a live one will not fill a single order.")
    print("\nHosts whose API is unrestricted: "
          + ", ".join(f"{v} ({k})" for k, v in sorted(API_OPEN.items())))
    print("Nearest to the matching engine (AWS eu-west-2, London): "
          "Amsterdam ~8ms, Dublin ~11ms.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
