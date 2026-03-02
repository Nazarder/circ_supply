"""
fetch_binance_data.py
---------------------
Downloads actual Binance USDT-M perpetual futures data for all symbols
that overlap with our CMC universe:
  - Weekly OHLCV (perp mark price)
  - 8h funding rates → aggregated to weekly sums

Outputs (saved to binance_perp_data/):
  weekly_ohlcv.parquet      — symbol, week_start, open, high, low, close, volume, quote_volume
  weekly_funding.parquet    — symbol, week_start, funding_sum, funding_count, funding_mean
  symbol_meta.csv           — symbol, binance_symbol, onboard_date

Saves incrementally every SAVE_EVERY symbols so progress is never lost.
No API key required — all public endpoints.
"""

import os, sys, time, json, datetime
import urllib.request
import urllib.error
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
OUT_DIR     = "binance_perp_data"
CMC_FILE    = "cmc_historical_top300_filtered_with_supply.csv"
START_DATE  = "2020-01-01"
DELAY_S     = 0.12          # seconds between requests
MAX_RETRIES = 3
SAVE_EVERY  = 50            # checkpoint interval

BASE_URL    = "https://fapi.binance.com"

os.makedirs(OUT_DIR, exist_ok=True)

OHLCV_FILE   = f"{OUT_DIR}/weekly_ohlcv.parquet"
FUNDING_FILE = f"{OUT_DIR}/weekly_funding.parquet"

# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch_json(url: str) -> list | dict:
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("    [rate limit] sleeping 60s...", flush=True)
                time.sleep(60)
            elif attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                raise
    return []

def ms_to_dt(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)

def dt_to_ms(dt: datetime.datetime) -> int:
    return int(dt.timestamp() * 1000)

def save_df(df: pd.DataFrame, path: str):
    if os.path.exists(path):
        existing = pd.read_parquet(path)
        df = pd.concat([existing, df], ignore_index=True).drop_duplicates()
    df.to_parquet(path, index=False)

# ── Step 1: Exchange info ─────────────────────────────────────────────────────
print("=" * 70)
print("Fetching Binance USDT-M perpetual exchange info...")
info = fetch_json(f"{BASE_URL}/fapi/v1/exchangeInfo")
binance_contracts = {
    s["baseAsset"]: {
        "full_symbol": s["symbol"],
        "onboard_date": ms_to_dt(s.get("onboardDate", 0)),
    }
    for s in info["symbols"]
    if s["status"] == "TRADING"
    and s["contractType"] == "PERPETUAL"
    and s["quoteAsset"] == "USDT"
    and s["baseAsset"].isascii()       # skip non-ASCII symbols
}
print(f"  Active USDT-M perp contracts (ASCII only): {len(binance_contracts)}")

# ── Step 2: CMC overlap ───────────────────────────────────────────────────────
print(f"\nLoading CMC universe from {CMC_FILE}...")
df_cmc = pd.read_csv(CMC_FILE)
df_cmc["snapshot_date"] = pd.to_datetime(df_cmc["snapshot_date"])
# keep only ASCII symbols
cmc_syms = {s for s in df_cmc["symbol"].unique() if s.isascii()}

overlap = sorted(cmc_syms & set(binance_contracts.keys()))
print(f"  CMC symbols (ASCII): {len(cmc_syms)}")
print(f"  Overlap with Binance perps: {len(overlap)}")

# Symbol metadata
meta_rows = [{"symbol": s,
              "binance_symbol": binance_contracts[s]["full_symbol"],
              "onboard_date": binance_contracts[s]["onboard_date"]}
             for s in overlap]
pd.DataFrame(meta_rows).to_csv(f"{OUT_DIR}/symbol_meta.csv", index=False)
print(f"  Saved {OUT_DIR}/symbol_meta.csv")

start_ms = dt_to_ms(pd.Timestamp(START_DATE).to_pydatetime())

# ── Step 3: Weekly OHLCV ─────────────────────────────────────────────────────
# Determine which symbols already downloaded
done_ohlcv = set()
if os.path.exists(OHLCV_FILE):
    done_ohlcv = set(pd.read_parquet(OHLCV_FILE)["symbol"].unique())
    print(f"\n[Resume] OHLCV already have {len(done_ohlcv)} symbols, skipping them.")

remaining_ohlcv = [s for s in overlap if s not in done_ohlcv]
print(f"Fetching weekly OHLCV for {len(remaining_ohlcv)} symbols...")

batch_ohlcv = []
failed_ohlcv = []

for i, sym in enumerate(remaining_ohlcv):
    full_sym = binance_contracts[sym]["full_symbol"]
    rows = []
    cur_start = start_ms

    try:
        while True:
            url = (f"{BASE_URL}/fapi/v1/klines"
                   f"?symbol={full_sym}&interval=1w"
                   f"&startTime={cur_start}&limit=500")
            batch = fetch_json(url)
            if not batch:
                break
            for c in batch:
                rows.append({
                    "symbol":       sym,
                    "week_start":   ms_to_dt(c[0]),
                    "open":         float(c[1]),
                    "high":         float(c[2]),
                    "low":          float(c[3]),
                    "close":        float(c[4]),
                    "volume":       float(c[5]),
                    "quote_volume": float(c[7]),
                    "trades":       int(c[8]),
                })
            if len(batch) < 500:
                break
            cur_start = batch[-1][6] + 1
            time.sleep(DELAY_S)
    except Exception as e:
        print(f"  FAILED {sym}: {type(e).__name__}", flush=True)
        failed_ohlcv.append(sym)

    batch_ohlcv.extend(rows)
    done = len(done_ohlcv) + i + 1
    total = len(overlap)
    if (i + 1) % 25 == 0 or (i + 1) == len(remaining_ohlcv):
        print(f"  [{done}/{total}] {sym}: {len(rows)} weeks", flush=True)

    # Checkpoint every SAVE_EVERY symbols
    if len(batch_ohlcv) > 0 and ((i + 1) % SAVE_EVERY == 0 or (i + 1) == len(remaining_ohlcv)):
        save_df(pd.DataFrame(batch_ohlcv), OHLCV_FILE)
        batch_ohlcv = []
        print(f"  [checkpoint saved]", flush=True)

    time.sleep(DELAY_S)

if failed_ohlcv:
    print(f"  OHLCV failures: {failed_ohlcv}")

df_ohlcv = pd.read_parquet(OHLCV_FILE)
print(f"\nOHLCV total: {len(df_ohlcv):,} rows, {df_ohlcv['symbol'].nunique()} symbols")
print(f"Date range : {df_ohlcv['week_start'].min()} -> {df_ohlcv['week_start'].max()}")

# ── Step 4: 8h Funding Rates → weekly ────────────────────────────────────────
done_funding = set()
if os.path.exists(FUNDING_FILE):
    done_funding = set(pd.read_parquet(FUNDING_FILE)["symbol"].unique())
    print(f"\n[Resume] Funding already have {len(done_funding)} symbols, skipping them.")

remaining_fund = [s for s in overlap if s not in done_funding]
print(f"Fetching 8h funding rates for {len(remaining_fund)} symbols...")

batch_funding = []
failed_funding = []

for i, sym in enumerate(remaining_fund):
    full_sym = binance_contracts[sym]["full_symbol"]
    rows = []
    cur_start = start_ms

    try:
        while True:
            url = (f"{BASE_URL}/fapi/v1/fundingRate"
                   f"?symbol={full_sym}&startTime={cur_start}&limit=1000")
            batch = fetch_json(url)
            if not batch:
                break
            for rec in batch:
                rows.append({
                    "symbol":     sym,
                    "funding_ts": ms_to_dt(rec["fundingTime"]),
                    "rate":       float(rec["fundingRate"]),
                })
            if len(batch) < 1000:
                break
            cur_start = batch[-1]["fundingTime"] + 1
            time.sleep(DELAY_S)
    except Exception as e:
        print(f"  FAILED {sym}: {type(e).__name__}", flush=True)
        failed_funding.append(sym)

    # Aggregate 8h → weekly
    if rows:
        df_r = pd.DataFrame(rows)
        df_r["week_start"] = df_r["funding_ts"].dt.to_period("W-SAT").dt.start_time
        grp = df_r.groupby(["symbol", "week_start"])["rate"].agg(
            funding_sum="sum",
            funding_count="count",
            funding_mean="mean",
        ).reset_index()
        batch_funding.append(grp)

    done = len(done_funding) + i + 1
    total = len(overlap)
    if (i + 1) % 25 == 0 or (i + 1) == len(remaining_fund):
        print(f"  [{done}/{total}] {sym}: {len(rows)} 8h periods", flush=True)

    if len(batch_funding) > 0 and ((i + 1) % SAVE_EVERY == 0 or (i + 1) == len(remaining_fund)):
        combined = pd.concat(batch_funding, ignore_index=True)
        save_df(combined, FUNDING_FILE)
        batch_funding = []
        print(f"  [checkpoint saved]", flush=True)

    time.sleep(DELAY_S)

if failed_funding:
    print(f"  Funding failures: {failed_funding}")

df_funding = pd.read_parquet(FUNDING_FILE)
print(f"\nFunding total: {len(df_funding):,} rows, {df_funding['symbol'].nunique()} symbols")
print(f"Date range  : {df_funding['week_start'].min()} -> {df_funding['week_start'].max()}")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("DOWNLOAD COMPLETE")
print(f"  weekly_ohlcv.parquet   : {df_ohlcv['symbol'].nunique()} symbols, {len(df_ohlcv):,} rows")
print(f"  weekly_funding.parquet : {df_funding['symbol'].nunique()} symbols, {len(df_funding):,} rows")
print("=" * 70)
