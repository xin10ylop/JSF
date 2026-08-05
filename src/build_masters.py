"""Build market master tables and download manifests from the markets dataset.

Outputs:
- data/telonex/markets.parquet (raw metadata download)
- data/master_updown.parquet  (5m/15m/4h: window times, oracle strike/settle, result)
- data/master_hourly.parquet  (hourly up-or-down: times, Binance 1H open/close, result)
- data/master_above.parquet   (hourly above-strike: strike, end time, close, result)
- data/manifests/*.csv        (download manifests per family/channel)

Settlement conventions (verified empirically):
- updown: start = first Chainlink round ts >= t0, end = first round >= t1,
  Up iff end >= start. Match 99.996% on clean data.
- hourly updown: Up iff Binance 1H candle close >= open (100% on 7,385).
- above: Yes iff Binance 1H candle close > strike; title hour = candle END
  (100% on 46,320).
"""
import glob
import os
import re

import numpy as np
import pandas as pd

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
DUR = {"5m": 300, "15m": 900, "4h": 14400}


def download_markets():
    if not os.path.exists("data/telonex/markets.parquet"):
        df = pd.read_parquet("https://api.telonex.io/v1/datasets/polymarket/markets")
        df.to_parquet("data/telonex/markets.parquet")
    return pd.read_parquet("data/telonex/markets.parquet")


def build_updown(mk, cp_path="data/telonex/chainlink_btcusd.parquet"):
    ud = mk[mk.slug.str.match(r"btc-updown-(5m|15m|4h)-\d+", na=False)].copy()
    ud["horizon"] = ud.slug.str.extract(r"btc-updown-(\d+[mh])-")[0]
    ud["t0_us"] = ud.slug.str.extract(r"-(\d+)$")[0].astype("int64") * 1_000_000
    ud["t1_us"] = ud.t0_us + ud.horizon.map(DUR).astype("int64") * 1_000_000
    ud["result"] = ud.result_id.replace("", "-1").astype(int)
    if os.path.exists(cp_path):
        cp = pd.read_parquet(cp_path, columns=["timestamp_us", "price_f"])
        ts = cp.timestamp_us.values
        pf = cp.price_f.values
        i0 = np.searchsorted(ts, ud.t0_us.values, "left").clip(0, len(ts) - 1)
        i1 = np.searchsorted(ts, ud.t1_us.values, "left").clip(0, len(ts) - 1)
        ok0 = (ts[i0] - ud.t0_us.values) <= 5e6
        ok1 = (ts[i1] - ud.t1_us.values) <= 5e6
        ud["strike"] = np.where(ok0, pf[i0], np.nan)
        ud["settle_px"] = np.where(ok1, pf[i1], np.nan)
    else:
        ud["strike"] = np.nan
        ud["settle_px"] = np.nan
    keep = ["slug", "horizon", "t0_us", "t1_us", "result", "strike", "settle_px",
            "market_id", "asset_id_0", "asset_id_1", "settled_at_us",
            "trades_from", "trades_to"]
    ud[keep].to_parquet("data/master_updown.parquet", compression="zstd")
    print("master_updown:", len(ud), "strike coverage:",
          int(ud.strike.notna().sum()))
    return ud


def _binance_1s():
    fs = sorted(glob.glob("data/binance/parquet/*.parquet"))
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "open", "close"])
                    for f in fs], ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    return bn.sort_values("sec").drop_duplicates("sec").reset_index(drop=True)


def build_hourly(mk):
    hr = mk[mk.slug.str.match(
        r"bitcoin-up-or-down-[a-z]+-\d+(-\d+)?-\d+[ap]m-et$", na=False)].copy()

    def t0_us(row):
        m = re.match(r"bitcoin-up-or-down-([a-z]+)-(\d+)(?:-(\d+))?-(\d+)([ap])m-et$",
                     row.slug)
        mon, day, yr, hh, ap = m.groups()
        day = int(day)
        h = int(hh) % 12 + (12 if ap == "p" else 0)
        end_dt = pd.to_datetime(row.end_date_us, unit="us")
        year = int(yr) if yr else end_dt.year
        for y in (year, year - 1, year + 1):
            try:
                t = (pd.Timestamp(year=y, month=MONTHS[mon], day=day, hour=h,
                                  tz="America/New_York")
                     .tz_convert("UTC").tz_localize(None))
            except Exception:
                continue
            if abs((end_dt - (t + pd.Timedelta(hours=1))).total_seconds()) <= 3700:
                return int(t.value // 1000)
        return -1

    hr["t0_us"] = hr.apply(t0_us, axis=1)
    hr = hr[hr.t0_us > 0].copy()
    hr["t1_us"] = hr.t0_us + 3600 * 1_000_000
    hr["result"] = hr.result_id.replace("", "-1").astype(int)
    bn = _binance_1s()
    sec = bn.sec.values
    s0 = (hr.t0_us.values // 1_000_000).astype("int64")
    s1 = s0 + 3599
    i0 = np.searchsorted(sec, s0, "left").clip(0, len(sec) - 1)
    i1 = np.searchsorted(sec, s1, "left").clip(0, len(sec) - 1)
    ok = (np.abs(sec[i0] - s0) < 60) & (np.abs(sec[i1] - s1) < 60)
    hr["h_open"] = np.where(ok, bn.open.values[i0], np.nan)
    hr["h_close"] = np.where(ok, bn.close.values[i1], np.nan)
    hr[["slug", "t0_us", "t1_us", "result", "h_open", "h_close", "market_id",
        "asset_id_0", "asset_id_1", "trades_from", "trades_to"]].to_parquet(
        "data/master_hourly.parquet", compression="zstd")
    val = hr[(hr.result >= 0) & hr.h_open.notna()]
    pred = np.where(val.h_close >= val.h_open, 0, 1)
    print("master_hourly:", len(hr), "settle check:",
          round((pred == val.result.values).mean(), 5), "n=", len(val))
    return hr


def build_above(mk):
    ab = mk[mk.slug.str.match(
        r"bitcoin-above-\d+-on-[a-z]+-\d+-\d+-\d+[ap]m-et$", na=False)].copy()

    def parse(row):
        m = re.match(
            r"bitcoin-above-(\d+)-on-([a-z]+)-(\d+)-(\d+)-(\d+)([ap])m-et$", row.slug)
        K, mon, day, yr, hh, ap = m.groups()
        h = int(hh) % 12 + (12 if ap == "p" else 0)
        try:
            t = (pd.Timestamp(year=int(yr), month=MONTHS[mon], day=int(day), hour=h,
                              tz="America/New_York").tz_convert("UTC")
                 .tz_localize(None))
        except Exception:
            return (int(K), -1)
        return (int(K), int(t.value // 1000))

    pk = ab.apply(parse, axis=1)
    ab["strike"] = [x[0] for x in pk]
    ab["t1_us"] = [x[1] for x in pk]
    ab = ab[ab.t1_us > 0].copy()
    ab["result"] = ab.result_id.replace("", "-1").astype(int)
    bn = _binance_1s()
    sec = bn.sec.values
    s1 = (ab.t1_us.values // 1_000_000).astype("int64") - 1
    i1 = np.searchsorted(sec, s1, "left").clip(0, len(sec) - 1)
    ok = np.abs(sec[i1] - s1) < 60
    ab["h_close"] = np.where(ok, bn.close.values[i1], np.nan)
    ab[["slug", "strike", "t1_us", "result", "h_close", "market_id",
        "asset_id_0", "asset_id_1", "trades_from", "trades_to"]].to_parquet(
        "data/master_above.parquet", compression="zstd")
    val = ab[(ab.result >= 0) & ab.h_close.notna()]
    pred = np.where(val.h_close > val.strike, 0, 1)
    print("master_above:", len(ab), "settle check:",
          round((pred == val.result.values).mean(), 5), "n=", len(val))
    return ab


def build_manifests(mk):
    os.makedirs("data/manifests", exist_ok=True)

    def trade_rows(df, outcome_col=None):
        rows = []
        for _, m in df[df.trades_from != ""].iterrows():
            d0 = pd.Timestamp(m.trades_from)
            d1 = pd.Timestamp(m.trades_to)
            out = m[outcome_col] if outcome_col else "Up"
            for d in pd.date_range(d0, d1 - pd.Timedelta(days=1)):
                ds = d.strftime("%Y-%m-%d")
                rows.append(dict(channel="trades", date=ds, slug=m.slug,
                                 outcome=out, asset_id="",
                                 out_name=f"{m.slug}__{ds}.parquet"))
        return pd.DataFrame(rows)

    ud = mk[mk.slug.str.match(r"btc-updown-\d+[mh]-\d+", na=False)]
    for h in ["15m", "5m", "4h"]:
        sub = trade_rows(ud[ud.slug.str.contains(f"-{h}-")])
        sub.to_csv(f"data/manifests/updown_{h}_trades.csv", index=False)
        print(f"manifest updown_{h}: {len(sub)}")
    hr = mk[mk.slug.str.match(
        r"bitcoin-up-or-down-[a-z]+-\d+(-\d+)?-\d+[ap]m-et$", na=False)]
    trade_rows(hr, "outcome_0").to_csv(
        "data/manifests/hourly_updown_trades.csv", index=False)
    ab = mk[mk.slug.str.match(
        r"bitcoin-above-\d+-on-[a-z]+-\d+-\d+-\d+[ap]m-et$", na=False)]
    trade_rows(ab, "outcome_0").to_csv(
        "data/manifests/above_hourly_trades.csv", index=False)

    rows = [dict(channel="crypto_prices", date=d.strftime("%Y-%m-%d"), slug="",
                 outcome="", asset_id="btcusd",
                 out_name=f"btcusd__{d.strftime('%Y-%m-%d')}.parquet")
            for d in pd.date_range("2026-04-02", "2026-08-05")]
    pd.DataFrame(rows).to_csv("data/manifests/crypto_prices.csv", index=False)

    rng = 7
    book_rows = []
    for pat, n in [(r"btc-updown-15m-", 1000), (r"btc-updown-5m-", 1000),
                   (r"btc-updown-4h-", 200),
                   (r"bitcoin-up-or-down-[a-z]+-\d+(-\d+)?-\d+[ap]m-et$", 400),
                   (r"bitcoin-above-\d+-on-[a-z]+-\d+-\d+-\d+[ap]m-et$", 400)]:
        df = mk[mk.slug.str.match(pat, na=False)
                & (mk.book_snapshot_5_from != "")]
        take = df.sample(min(n, len(df)), random_state=rng)
        for _, m in take.iterrows():
            d0 = pd.Timestamp(m.book_snapshot_5_from)
            d1 = pd.Timestamp(m.book_snapshot_5_to)
            for d in pd.date_range(d0, d1 - pd.Timedelta(days=1)):
                ds = d.strftime("%Y-%m-%d")
                book_rows.append(dict(channel="book_snapshot_5", date=ds,
                                      slug=m.slug, outcome=m.outcome_0,
                                      asset_id="",
                                      out_name=f"{m.slug}__{ds}.parquet"))
    pd.DataFrame(book_rows).to_csv("data/manifests/book5_sample.csv", index=False)
    print("manifest book5_sample:", len(book_rows))


if __name__ == "__main__":
    mk = download_markets()
    import sys
    steps = sys.argv[1:] or ["updown", "hourly", "above", "manifests"]
    if "updown" in steps:
        build_updown(mk)
    if "hourly" in steps:
        build_hourly(mk)
    if "above" in steps:
        build_above(mk)
    if "manifests" in steps:
        build_manifests(mk)
