"""
diagnostic_exclusions.py
========================
Analyzes why tokens are excluded from the v7 supply-dilution L/S strategy
across the full CMC history (2017-2026).

For each monthly snapshot, every token is classified into exactly one
exclusion category (or ELIGIBLE) using the same filters as v7.

Outputs five reports to stdout and saves the full per-token-period
exclusion log to exclusion_log.csv.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

# ===========================================================================
#  CONFIGURATION  (identical to v7)
# ===========================================================================

INPUT_FILE = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
BN_DIR     = "D:/AI_Projects/circ_supply/binance_perp_data/"
OUTPUT_LOG = "D:/AI_Projects/circ_supply/exclusion_log.csv"

MAX_RANK             = 200
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000        # daily USD proxy; weekly floor = *7
MIN_MKTCAP           = 50_000_000
MIN_SUPPLY_HISTORY   = 26
FFILL_LIMIT          = 1
SUPPLY_WINDOW        = 13

STABLECOINS = {
    "USDT","USDC","BUSD","DAI","TUSD","USDP","GUSD","FRAX","LUSD","MIM",
    "USDN","USTC","UST","HUSD","SUSD","PAX","USDS","USDJ","NUSD","USDK",
    "USDX","CUSD","CEUR","USDH","USDD","FDUSD","PYUSD","EURC","EURS",
    "USDQ","USDB","USDTB",
}
CEX_TOKENS = {
    "BNB","HT","KCS","OKB","MX","CRO","BIX","GT","LEO","FTT",
    "WBT","BGB","BTSE","NEXO","CEL","LATOKEN","BTMX",
}
MEMECOINS = {
    "DOGE","SHIB","FLOKI","PEPE","BONK","WIF","FARTCOIN","SAFEMOON","ELON",
    "DOGELON","MEME","TURBO","POPCAT","MOG","BABYDOGE","KISHU","AKITA","HOGE",
    "SAITAMA","VOLT","ELONGATE","SAMO","BOME","NEIRO","SPX","BRETT","MYRO",
    "SLERF","TOSHI","GIGA","SUNDOG","MOODENG","PNUT","ACT","GOAT","CHILLGUY",
    "PONKE","LADYS","COQ","AIDOGE","WOJAK","HUHU","MILADY","BOBO","QUACK",
    "BONE","LEASH","FLOOF","PITBULL","HOKK","CATGIRL","SFM","LUNC",
}
WRAPPED_ASSETS = {
    "WBTC","BTCB","BBTC","RBTC","rBTC","FBTC",
    "UNIBTC","PUMPBTC","EBTC","LBTC","SolvBTC","xSolvBTC",
    "WETH","WBNB","renBTC","renETH","renDOGE","renZEC","BTC.b","SolvBTC.BBN",
}
LIQUID_STAKING = {
    "STETH","RETH","CBETH","ANKRETH","FRXETH",
    "OSETH","LSETH","METH","EZETH","EETH",
    "SFRXETH","CMETH","BETH","TETH","PZETH",
    "ETHX","PUFETH","RSETH",
    "JITOSOL","MSOL","BNSOL","JUPSOL","BSOL","BBSOL","JSOL",
    "SAVAX","sAVAX","STRX","HASUI","KHYPE",
    "slisBNB","WBETH","stkAAVE","STKAAVE",
}
PROTOCOL_SYNTHETICS = {"vBTC","vETH","vBNB","vXVS","VRT","VTHO"}
COMMODITY_BACKED    = {"PAXG","XAUT","XAUt","KAU"}

EXCLUDED = (STABLECOINS | CEX_TOKENS | MEMECOINS
            | WRAPPED_ASSETS | LIQUID_STAKING
            | PROTOCOL_SYNTHETICS | COMMODITY_BACKED)

# Priority-ordered list of (category_label, check_function)
# check_function signature: (row, bn_symbols, onboard_map, adtv_now, snap_date) -> bool
# We evaluate in order and assign the FIRST matching category.
CATEGORY_ORDER = [
    "Stablecoin",
    "CEX Token",
    "Memecoin",
    "Wrapped Asset",
    "Liquid Staking Token",
    "Protocol Synthetic",
    "Commodity-Backed",
    "Top-20 rank excluded",
    "Rank > 200",
    "Market cap < $50M",
    "No Binance perp listing",
    "Binance not yet listed",
    "ADTV < $35M/week",
    "Supply history < 26mo",
    "ELIGIBLE",
]


# ===========================================================================
#  STEP 1 — Load & preprocess CMC data  (same as v7 load_cmc + engineer_features)
# ===========================================================================

def load_cmc(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    # ASCII filter
    df = df[df["symbol"].apply(lambda s: str(s).isascii())]
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(
            lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()

    # Supply features (same as v7 engineer_features)
    grp = df.groupby("symbol", group_keys=False)
    df["supply_inf_13w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf"] = df["supply_inf_13w"]
    df["supply_hist_count"] = grp["supply_inf"].transform(
        lambda s: s.notna().cumsum())

    print(f"[CMC] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  STEP 2 — Load Binance data  (same as v7 load_binance, but we only need
#            bn_symbols, onboard_map, and weekly ADTV pivot)
# ===========================================================================

def load_binance(bn_dir: str):
    ohlcv = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    meta  = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    ohlcv["cmc_date"] = ohlcv["week_start"] + pd.Timedelta(days=6)

    bn_adtv_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="quote_volume", aggfunc="last")

    bn_symbols  = set(ohlcv["symbol"].unique())
    onboard_map = dict(zip(meta["symbol"], meta["onboard_date"]))

    print(f"[Binance] {len(bn_symbols)} unique symbols | "
          f"onboard entries: {len(onboard_map)} | "
          f"ADTV pivot: {bn_adtv_piv.shape}")
    return bn_symbols, onboard_map, bn_adtv_piv


# ===========================================================================
#  STEP 3 — Monthly rebalancing dates  (first snapshot date of each month,
#            matching v7's  all_rebal = sorted(df.groupby("ym")["snapshot_date"].min()))
# ===========================================================================

def get_rebal_dates(df: pd.DataFrame) -> list:
    df2 = df.copy()
    df2["ym"] = df2["snapshot_date"].dt.to_period("M")
    rebals = sorted(df2.groupby("ym")["snapshot_date"].min().tolist())
    print(f"[Rebal] {len(rebals)} monthly snapshot dates "
          f"({rebals[0].date()} -> {rebals[-1].date()})")
    return rebals


# ===========================================================================
#  STEP 4 — Classify every token at every snapshot
# ===========================================================================

def classify_snapshot(snap: pd.DataFrame,
                       snap_date: pd.Timestamp,
                       bn_symbols: set,
                       onboard_map: dict,
                       adtv_now: pd.Series) -> list:
    """
    Returns a list of dicts: {symbol, snap_date, category, rank, market_cap}
    for every token in this snapshot.
    """
    records = []
    for _, row in snap.iterrows():
        sym  = row["symbol"]
        rank = row.get("rank", np.nan)
        mc   = row.get("market_cap", np.nan)

        # Walk through priority-ordered categories
        if sym in STABLECOINS:
            cat = "Stablecoin"
        elif sym in CEX_TOKENS:
            cat = "CEX Token"
        elif sym in MEMECOINS:
            cat = "Memecoin"
        elif sym in WRAPPED_ASSETS:
            cat = "Wrapped Asset"
        elif sym in LIQUID_STAKING:
            cat = "Liquid Staking Token"
        elif sym in PROTOCOL_SYNTHETICS:
            cat = "Protocol Synthetic"
        elif sym in COMMODITY_BACKED:
            cat = "Commodity-Backed"
        elif pd.notna(rank) and rank <= TOP_N_EXCLUDE:
            cat = "Top-20 rank excluded"
        elif pd.isna(rank) or rank > MAX_RANK:
            cat = "Rank > 200"
        elif pd.isna(mc) or mc < MIN_MKTCAP:
            cat = "Market cap < $50M"
        elif sym not in bn_symbols:
            cat = "No Binance perp listing"
        elif pd.isna(onboard_map.get(sym)) or onboard_map.get(sym) > snap_date:
            cat = "Binance not yet listed"
        elif (adtv_now is not None and
              (pd.isna(adtv_now.get(sym)) or
               float(adtv_now.get(sym, 0)) < MIN_VOLUME * 7)):
            cat = "ADTV < $35M/week"
        elif (row.get("supply_hist_count", 0) < MIN_SUPPLY_HISTORY or
              pd.isna(row.get("supply_inf_13w"))):
            cat = "Supply history < 26mo"
        else:
            cat = "ELIGIBLE"

        records.append({
            "symbol":    sym,
            "snap_date": snap_date,
            "category":  cat,
            "rank":      rank,
            "market_cap": mc,
        })
    return records


def build_exclusion_log(df: pd.DataFrame,
                         rebals: list,
                         bn_symbols: set,
                         onboard_map: dict,
                         bn_adtv_piv: pd.DataFrame) -> pd.DataFrame:
    all_records = []
    snap_df = df[df["snapshot_date"].isin(set(rebals))].copy()

    for snap_date in rebals:
        snap = snap_df[snap_df["snapshot_date"] == snap_date]
        if snap.empty:
            continue

        adtv_now = (bn_adtv_piv.loc[snap_date]
                    if snap_date in bn_adtv_piv.index
                    else None)

        records = classify_snapshot(snap, snap_date, bn_symbols,
                                    onboard_map, adtv_now)
        all_records.extend(records)

    log = pd.DataFrame(all_records)
    print(f"[Log] {len(log):,} token-periods classified across "
          f"{log['snap_date'].nunique()} snapshots")
    return log


# ===========================================================================
#  STEP 5 — Reports
# ===========================================================================

def report_a(log: pd.DataFrame) -> None:
    """Exclusion reason summary (all time)."""
    total = len(log)
    summary = (log.groupby("category")
                  .agg(token_periods=("symbol", "count"),
                       unique_tokens=("symbol", "nunique"))
                  .reset_index())
    summary["pct"] = summary["token_periods"] / total * 100
    summary = summary.sort_values("token_periods", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 78)
    print("REPORT A — Exclusion Reason Summary (All Time)")
    print("=" * 78)
    hdr = f"{'Exclusion reason':<32} {'Unique tokens':>14} {'Token-periods':>14} {'% of all appearances':>22}"
    print(hdr)
    print("-" * 84)
    for _, r in summary.iterrows():
        print(f"{r['category']:<32} {r['unique_tokens']:>14,} "
              f"{r['token_periods']:>14,} {r['pct']:>21.1f}%")
    print(f"\n  Total token-periods: {total:,}")


def report_b(log: pd.DataFrame) -> None:
    """Eligible token count over time (by year)."""
    elig = log[log["category"] == "ELIGIBLE"].copy()
    elig["year"] = elig["snap_date"].dt.year

    # Periods per year
    periods_per_year = (log.groupby(log["snap_date"].dt.year)["snap_date"]
                           .nunique().rename("periods"))
    elig_by_year = (elig.groupby("year")
                        .agg(total_elig=("symbol", "count"),
                             unique_elig=("symbol", "nunique"))
                        .join(periods_per_year)
                        .reset_index())
    elig_by_year["avg_per_period"] = (elig_by_year["total_elig"] /
                                       elig_by_year["periods"])

    print("\n" + "=" * 78)
    print("REPORT B — Eligible Token Count Over Time")
    print("=" * 78)
    print(f"{'Year':>6} {'Periods':>9} {'Avg eligible/period':>22} "
          f"{'Total ELIGIBLE token-periods':>30}")
    print("-" * 70)
    for _, r in elig_by_year.iterrows():
        print(f"{int(r['year']):>6} {int(r['periods']):>9} "
              f"{r['avg_per_period']:>22.1f} {int(r['total_elig']):>30,}")


def report_c(log: pd.DataFrame) -> None:
    """Per-token eligibility breakdown for tokens with 10+ eligible periods."""
    elig = log[log["category"] == "ELIGIBLE"].copy()
    elig_counts = (elig.groupby("symbol")
                       .agg(eligible_periods=("snap_date", "count"),
                            first_eligible=("snap_date", "min"),
                            last_eligible=("snap_date", "max"))
                       .reset_index())
    elig_counts = elig_counts[elig_counts["eligible_periods"] >= 10]
    elig_counts = elig_counts.sort_values("eligible_periods", ascending=False)

    # For each token, find the primary pre-eligible exclusion reason
    # (the most frequent non-ELIGIBLE category before first_eligible date)
    pre_excl = {}
    non_elig = log[log["category"] != "ELIGIBLE"]
    for _, row in elig_counts.iterrows():
        sym  = row["symbol"]
        fe   = row["first_eligible"]
        pre  = non_elig[(non_elig["symbol"] == sym) &
                        (non_elig["snap_date"] < fe)]
        if not pre.empty:
            pre_excl[sym] = pre["category"].value_counts().idxmax()
        else:
            pre_excl[sym] = "—"

    top30 = elig_counts.head(30).reset_index(drop=True)

    print("\n" + "=" * 78)
    print("REPORT C — Per-Token Eligibility Breakdown (top 30 most-eligible tokens)")
    print("  (Only tokens with >= 10 eligible periods shown)")
    print("=" * 78)
    print(f"{'Symbol':<12} {'Eligible periods':>18} {'First eligible':>16} "
          f"{'Last eligible':>16} {'Pre-eligible exclusion':>24}")
    print("-" * 90)
    for _, r in top30.iterrows():
        sym = r["symbol"]
        print(f"{sym:<12} {int(r['eligible_periods']):>18} "
              f"{str(r['first_eligible'].date()):>16} "
              f"{str(r['last_eligible'].date()):>16} "
              f"{pre_excl.get(sym, '—'):>24}")


def report_d(log: pd.DataFrame) -> None:
    """All unique symbols in each exclusion category (ever appeared in CMC data)."""
    cats = [c for c in CATEGORY_ORDER
            if c not in ("ELIGIBLE",)]

    print("\n" + "=" * 78)
    print("REPORT D — Categorically Excluded Tokens (all unique symbols per category)")
    print("=" * 78)
    for cat in cats:
        subset = log[log["category"] == cat]
        if subset.empty:
            continue
        syms = sorted(subset["symbol"].unique())
        print(f"\n  {cat} ({len(syms)} unique symbols):")
        # Print in rows of 10
        for i in range(0, len(syms), 10):
            print("    " + "  ".join(syms[i:i+10]))


def report_e(log: pd.DataFrame) -> None:
    """Tokens that were ELIGIBLE in some periods but excluded in others."""
    elig_sym = set(log[log["category"] == "ELIGIBLE"]["symbol"].unique())
    excl_sym = set(log[log["category"] != "ELIGIBLE"]["symbol"].unique())
    boundary = elig_sym & excl_sym

    if not boundary:
        print("\n[Report E] No boundary tokens found.")
        return

    elig = log[log["category"] == "ELIGIBLE"]
    elig_counts = (elig[elig["symbol"].isin(boundary)]
                       .groupby("symbol")["snap_date"]
                       .count()
                       .rename("eligible_periods")
                       .reset_index()
                       .sort_values("eligible_periods", ascending=False))

    top20 = elig_counts.head(20).reset_index(drop=True)

    # For each, find the dominant exclusion reason (in non-eligible periods)
    non_elig = log[(log["category"] != "ELIGIBLE") &
                   (log["symbol"].isin(top20["symbol"].tolist()))]
    excl_counts_by_sym = (non_elig.groupby(["symbol", "category"])["snap_date"]
                                  .count()
                                  .reset_index()
                                  .rename(columns={"snap_date": "excl_periods"}))

    # Build dominant exclusion map
    dominant_excl = {}
    for sym in top20["symbol"]:
        sub = excl_counts_by_sym[excl_counts_by_sym["symbol"] == sym]
        if sub.empty:
            dominant_excl[sym] = "—"
        else:
            sub_s = sub.sort_values("excl_periods", ascending=False)
            # Summarise as "reason(N), reason2(M), ..."
            parts = [f"{r['category']}({int(r['excl_periods'])})"
                     for _, r in sub_s.head(3).iterrows()]
            dominant_excl[sym] = ", ".join(parts)

    print("\n" + "=" * 78)
    print("REPORT E — Tokens on the Boundary (ELIGIBLE in some, excluded in other periods)")
    print("  Top 20 by number of eligible periods")
    print("=" * 78)
    print(f"{'Symbol':<12} {'Eligible periods':>18} {'Exclusion reasons (top 3)':>0}")
    print("-" * 78)
    for _, r in top20.iterrows():
        sym = r["symbol"]
        ep  = int(r["eligible_periods"])
        ex  = dominant_excl.get(sym, "—")
        print(f"{sym:<12} {ep:>18}   {ex}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 78)
    print("diagnostic_exclusions.py — v7 Universe Filter Diagnostics")
    print("=" * 78)

    df         = load_cmc(INPUT_FILE)
    bn_symbols, onboard_map, bn_adtv_piv = load_binance(BN_DIR)
    rebals     = get_rebal_dates(df)

    log        = build_exclusion_log(df, rebals, bn_symbols, onboard_map, bn_adtv_piv)

    # Save full log
    log.to_csv(OUTPUT_LOG, index=False)
    print(f"[Saved] Exclusion log -> {OUTPUT_LOG}  ({len(log):,} rows)")

    # Print reports
    report_a(log)
    report_b(log)
    report_c(log)
    report_d(log)
    report_e(log)

    print("\n" + "=" * 78)
    print("Done.")
    print("=" * 78)


if __name__ == "__main__":
    main()
