"""
fetch_orderbook_slippage.py
===========================
Fetch current Binance USDT-M Futures order book for all tokens in the
v9 universe. Compute actual bid-ask spread and market-impact slippage
at multiple position sizes.

No API key required — uses public /fapi/v1/depth endpoint.
API key (if provided) is used only to raise rate limits.

Outputs
-------
  orderbook_slippage.csv   — per-token: spread, impact at $100K/$375K/$1M/$5M
  (printed table)          — comparison: AC model vs actual book at $375K

Usage
-----
  python fetch_orderbook_slippage.py
  python fetch_orderbook_slippage.py --key YOUR_KEY --secret YOUR_SECRET
"""

import sys
import os
import time
import argparse
import hmac
import hashlib
import requests
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ───────────────────────────────────────────────────────────────────
BASE_URL   = "https://fapi.binance.com"
DEPTH_LEVELS = 20          # order book depth levels to fetch per symbol
RATE_SLEEP   = 0.05        # seconds between requests (no key: ~1200 req/min limit)

BN_META_PATH = "D:/AI_Projects/circ_supply/binance_perp_data/symbol_meta.csv"
BN_OHLCV_PATH = "D:/AI_Projects/circ_supply/binance_perp_data/weekly_ohlcv.parquet"

# Position sizes to test (USD per token)
POSITION_SIZES = [100_000, 375_000, 1_000_000, 5_000_000]

# ── Argument parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--key",    default="", help="Binance API key (optional)")
parser.add_argument("--secret", default="", help="Binance API secret (optional)")
args = parser.parse_args()

HEADERS = {}
if args.key:
    HEADERS["X-MBX-APIKEY"] = args.key
    RATE_SLEEP = 0.01   # with key: ~6000 req/min
    print(f"[Auth] Using API key (higher rate limit)")
else:
    print(f"[Auth] No API key — using public endpoints (rate limit: ~1200 req/min)")


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_exchange_info():
    """Fetch all active USDT-M perp symbols from Binance."""
    r = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", headers=HEADERS, timeout=10)
    r.raise_for_status()
    symbols = []
    for s in r.json()["symbols"]:
        if (s["contractType"] == "PERPETUAL"
                and s["quoteAsset"] == "USDT"
                and s["status"] == "TRADING"):
            symbols.append(s["baseAsset"])
    return set(symbols)


def get_depth(symbol_usdt: str) -> dict | None:
    """Fetch order book for a USDT-M perp. Returns None on error."""
    url = f"{BASE_URL}/fapi/v1/depth"
    try:
        r = requests.get(url, params={"symbol": symbol_usdt, "limit": DEPTH_LEVELS},
                         headers=HEADERS, timeout=8)
        if r.status_code == 400:
            return None   # symbol not found / not active
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def compute_impact(bids: list, asks: list, pos_usd: float) -> dict:
    """
    Walk the order book to compute:
      - best bid/ask spread (half-spread)
      - market impact of a BUY of pos_usd (lifting asks)
      - market impact of a SELL of pos_usd (hitting bids)
      - average one-way slippage vs mid

    bids/asks: list of [price_str, qty_str] pairs, best first.
    """
    if not bids or not asks:
        return {"half_spread": np.nan, "impact_buy": np.nan,
                "impact_sell": np.nan, "avg_impact": np.nan,
                "depth_buy_usd": 0, "depth_sell_usd": 0}

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid      = (best_bid + best_ask) / 2.0
    half_spread = (best_ask - best_bid) / (2.0 * mid)

    def _walk(side: list, pos: float) -> tuple[float, float]:
        """
        Walk the book, returning (vwap_impact, total_available_usd).
        vwap_impact = (vwap - mid) / mid  (positive = cost)
        """
        remaining = pos
        dollar_sum = 0.0
        qty_sum    = 0.0
        available  = 0.0
        for p_str, q_str in side:
            p = float(p_str)
            q = float(q_str)
            level_usd = p * q
            available += level_usd
            if remaining <= 0:
                break
            take = min(remaining, level_usd)
            dollar_sum += take
            qty_sum    += take / p
            remaining  -= take
        if qty_sum == 0:
            return np.nan, available
        vwap = dollar_sum / qty_sum
        return abs(vwap - mid) / mid, available

    impact_buy,  depth_buy  = _walk(asks, pos_usd)
    impact_sell, depth_sell = _walk(bids, pos_usd)

    # one-way average impact (buy or sell)
    avg_impact = (
        (impact_buy + impact_sell) / 2.0
        if not (np.isnan(impact_buy) or np.isnan(impact_sell))
        else np.nanmean([impact_buy, impact_sell])
    )

    # total effective cost = max(half_spread, market_impact)
    # (spread is always paid even for tiny orders; impact adds for large ones)
    total_cost = max(half_spread, avg_impact)

    return {
        "half_spread":   half_spread,
        "impact_buy":    impact_buy,
        "impact_sell":   impact_sell,
        "avg_impact":    avg_impact,
        "total_cost":    total_cost,
        "depth_buy_usd": depth_buy,
        "depth_sell_usd": depth_sell,
    }


def compute_ac_impact(vol_annual: float, adtv_weekly_usd: float,
                       pos_usd: float, eta: float = 0.5) -> float:
    """AC model impact for comparison: eta * sigma_daily * sqrt(pos / ADTV)."""
    sigma_d = vol_annual / np.sqrt(252)
    adtv_d  = max(adtv_weekly_usd / 7.0, 1.0)
    return min(eta * sigma_d * np.sqrt(max(pos_usd / adtv_d, 0.0)), 0.02)


# ── Load universe ─────────────────────────────────────────────────────────────

print("\n[1] Loading v9 universe from Binance meta + OHLCV...")
meta = pd.read_csv(BN_META_PATH)
universe_symbols = set(meta["symbol"].str.upper())

# Latest weekly volatility and ADTV from OHLCV
ohlcv = pd.read_parquet(BN_OHLCV_PATH)
ohlcv["cmc_date"] = ohlcv["week_start"] + pd.Timedelta(days=6)
close_piv = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                               values="close", aggfunc="last").sort_index()
ret_piv  = close_piv.pct_change(1)
vol_piv  = ret_piv.rolling(8, min_periods=4).std() * np.sqrt(52)
adtv_piv = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                               values="quote_volume", aggfunc="last")

# Use most recent available data point for each symbol
latest_vol  = vol_piv.ffill().iloc[-1]
latest_adtv = adtv_piv.ffill().iloc[-1]

print(f"[1] Universe: {len(universe_symbols)} symbols from meta")

# ── Fetch live exchange info ──────────────────────────────────────────────────

print("[2] Fetching active USDT-M perp symbols from Binance...")
try:
    active_symbols = get_exchange_info()
    print(f"[2] Active USDT-M perps: {len(active_symbols)} symbols")
except Exception as e:
    print(f"[2] WARNING: could not fetch exchange info: {e}")
    active_symbols = universe_symbols

tradeable = universe_symbols & active_symbols
print(f"[2] Tradeable overlap: {len(tradeable)} symbols")

# ── Fetch order books ─────────────────────────────────────────────────────────

print(f"\n[3] Fetching order books ({len(tradeable)} symbols, {DEPTH_LEVELS} levels)...")
print(f"    Expected time: ~{len(tradeable) * RATE_SLEEP:.0f}s")
print()

rows = []
failed = []
for i, sym in enumerate(sorted(tradeable)):
    bn_sym = f"{sym}USDT"
    depth  = get_depth(bn_sym)
    time.sleep(RATE_SLEEP)

    if depth is None or "bids" not in depth or "asks" not in depth:
        failed.append(sym)
        continue

    bids = depth["bids"]
    asks = depth["asks"]

    # AC model values from historical data
    vol_a  = float(latest_vol.get(sym, np.nan))   if sym in latest_vol.index  else np.nan
    adtv_w = float(latest_adtv.get(sym, np.nan))  if sym in latest_adtv.index else np.nan

    row = {"symbol": sym}

    # Compute at each position size
    for pos in POSITION_SIZES:
        impact_data = compute_impact(bids, asks, pos)
        label = f"${pos//1000}K" if pos < 1_000_000 else f"${pos//1_000_000}M"
        row[f"half_spread"]         = impact_data["half_spread"]
        row[f"impact_{label}"]      = impact_data["avg_impact"]
        row[f"total_{label}"]       = impact_data["total_cost"]
        row[f"depth_buy_{label}"]   = impact_data["depth_buy_usd"]

    # AC model at $375K (default $5M AUM, 10-token basket)
    if not np.isnan(vol_a) and not np.isnan(adtv_w):
        row["ac_impact_375K"] = compute_ac_impact(vol_a, adtv_w, 375_000)
        row["vol_annual"]     = vol_a
        row["adtv_weekly"]    = adtv_w
    else:
        row["ac_impact_375K"] = np.nan
        row["vol_annual"]     = np.nan
        row["adtv_weekly"]    = np.nan

    rows.append(row)

    if (i + 1) % 20 == 0:
        print(f"  {i+1}/{len(tradeable)} done...", flush=True)

df = pd.DataFrame(rows)
print(f"\n[3] Fetched: {len(df)} symbols  |  Failed: {len(failed)} ({', '.join(failed[:10])})")

# ── Save raw CSV ──────────────────────────────────────────────────────────────

out_path = "D:/AI_Projects/circ_supply/orderbook_slippage.csv"
df.to_csv(out_path, index=False)
print(f"[4] Saved → {out_path}")

# ── Analysis ──────────────────────────────────────────────────────────────────

print("\n" + "=" * 78)
print("  ORDER BOOK SLIPPAGE vs ALMGREN-CHRISS MODEL  (position = $375K)")
print("  = realistic $5M AUM portfolio, 10-token basket, 0.75 scale")
print("=" * 78)

# Focus on $375K position
df_valid = df[df["impact_$375K"].notna() & df["half_spread"].notna()].copy()
df_valid["half_spread_bps"] = df_valid["half_spread"] * 10_000
df_valid["impact_375K_bps"] = df_valid["impact_$375K"] * 10_000
df_valid["total_375K_bps"]  = df_valid["total_$375K"]  * 10_000
df_valid["ac_375K_bps"]     = df_valid["ac_impact_375K"] * 10_000

df_sort = df_valid.sort_values("total_375K_bps", ascending=False)

print(f"\n  {'Symbol':<10} {'Spread':>9} {'Impact':>9} {'Total':>9} {'AC model':>9} "
      f"{'AC/Book':>9} {'Depth($M)':>10}")
print(f"  {'':10} {'(half,bps)':>9} {'(bps)':>9} {'(bps)':>9} {'(bps)':>9} "
      f"{'ratio':>9} {'@375K':>10}")
print("  " + "-" * 76)

for _, r in df_sort.iterrows():
    spread = r["half_spread_bps"]
    impact = r["impact_375K_bps"]
    total  = r["total_375K_bps"]
    ac     = r["ac_375K_bps"]
    depth  = r.get("depth_buy_$375K", np.nan)
    ratio  = ac / total if (not np.isnan(ac) and total > 0) else np.nan

    spread_s = f"{spread:>8.1f}" if not np.isnan(spread) else "     N/A"
    impact_s = f"{impact:>8.1f}" if not np.isnan(impact) else "     N/A"
    total_s  = f"{total:>8.1f}"  if not np.isnan(total)  else "     N/A"
    ac_s     = f"{ac:>8.1f}"     if not np.isnan(ac)     else "     N/A"
    ratio_s  = f"{ratio:>8.2f}x" if not np.isnan(ratio)  else "     N/A"
    depth_s  = f"{depth/1e6:>9.1f}M" if not np.isnan(depth) and depth > 0 else "     N/A"

    print(f"  {r['symbol']:<10} {spread_s}  {impact_s}  {total_s}  {ac_s}  {ratio_s}  {depth_s}")

# ── Summary statistics ────────────────────────────────────────────────────────
print()
print("=" * 78)
print("  SUMMARY STATISTICS")
print("=" * 78)

for pos in POSITION_SIZES:
    label = f"${pos//1000}K" if pos < 1_000_000 else f"${pos//1_000_000}M"
    col   = f"total_{label}"
    if col in df_valid.columns:
        vals = df_valid[col].dropna() * 10_000
        print(f"\n  Position = {label:>6}:")
        print(f"    Mean total cost   : {vals.mean():>6.1f} bps")
        print(f"    Median total cost : {vals.median():>6.1f} bps")
        print(f"    P75               : {vals.quantile(0.75):>6.1f} bps")
        print(f"    P90               : {vals.quantile(0.90):>6.1f} bps")
        print(f"    > 50 bps (>0.5%)  : {(vals > 50).sum():>3} tokens")

if "ac_375K_bps" in df_valid.columns and "total_375K_bps" in df_valid.columns:
    valid_cmp = df_valid[df_valid["ac_375K_bps"].notna()].copy()
    valid_cmp["ratio"] = valid_cmp["ac_375K_bps"] / valid_cmp["total_375K_bps"]
    print(f"\n  AC model vs book at $375K:")
    print(f"    AC overestimates  : {(valid_cmp['ratio'] > 1.5).sum():>3} tokens "
          f"(AC > 1.5× book)")
    print(f"    AC roughly matches: {((valid_cmp['ratio'] >= 0.7) & (valid_cmp['ratio'] <= 1.5)).sum():>3} tokens "
          f"(0.7× to 1.5× book)")
    print(f"    AC underestimates : {(valid_cmp['ratio'] < 0.7).sum():>3} tokens "
          f"(AC < 0.7× book)")
    print(f"    Median ratio (AC / book): {valid_cmp['ratio'].median():.2f}x")

print(f"\n  Depth available at $375K position:")
if "depth_buy_$375K" in df_valid.columns:
    depth_vals = df_valid["depth_buy_$375K"].dropna()
    fully_filled = (depth_vals >= 375_000).sum()
    print(f"    Symbols with sufficient depth: {fully_filled}/{len(depth_vals)}")
    print(f"    Symbols that can't fill $375K in {DEPTH_LEVELS} levels: "
          f"{(depth_vals < 375_000).sum()}")

print(f"\n  Full results saved to: {out_path}")
print("=" * 78)
