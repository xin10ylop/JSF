"""Build 5-minute close series per coin from free Binance 1m klines.

Reads data/binance_alts/<SYM>-1m-<period>.zip (monthly + daily files) and
writes data/altbars/<coin>_5m.parquet with columns [t, close] where t is the
UTC epoch second of the bar CLOSE (so bar t is fully observable at time t).
"""
import glob
import io
import os
import zipfile

import pandas as pd

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "doge": "DOGEUSDT"}
OUT = "data/altbars"


def load_1m(sym):
    fs = sorted(glob.glob(f"data/binance_alts/{sym}-1m-*.zip"))
    parts = []
    for f in fs:
        try:
            with zipfile.ZipFile(f) as z:
                name = z.namelist()[0]
                raw = z.read(name)
        except Exception:  # noqa: BLE001
            continue
        head = raw[:200].decode("utf8", "ignore").lower()
        hdr = 0 if "open_time" in head else None
        d = pd.read_csv(io.BytesIO(raw), header=hdr, usecols=[0, 4],
                        names=None if hdr == 0 else ["ot", "close"])
        d.columns = ["ot", "close"]
        parts.append(d)
    if not parts:
        return None
    d = pd.concat(parts, ignore_index=True)
    d["ot"] = pd.to_numeric(d["ot"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna()
    # binance switched open_time to microseconds partway through history
    d["sec"] = (d["ot"] // 1_000_000).where(d["ot"] > 1e14, d["ot"] // 1000)
    d["sec"] = d["sec"].astype("int64")
    d = d.drop_duplicates("sec").sort_values("sec")
    return d[["sec", "close"]]


def build(coin):
    d = load_1m(SYM[coin])
    if d is None:
        print(f"{coin}: no klines")
        return
    # bar close time = open_time + 60s
    d["t"] = d["sec"] + 60
    d = d[d["t"] % 300 == 0][["t", "close"]]     # keep 5m boundaries
    d = d.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    os.makedirs(OUT, exist_ok=True)
    d.to_parquet(f"{OUT}/{coin}_5m.parquet", index=False)
    print(f"{coin}: {len(d)} 5m bars "
          f"{pd.to_datetime(d.t.min(), unit='s')} -> "
          f"{pd.to_datetime(d.t.max(), unit='s')}")


if __name__ == "__main__":
    for c in SYM:
        build(c)
