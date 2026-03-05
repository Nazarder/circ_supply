"""
test_architectures.py
=====================
Architectural variant tests using only existing data.
All tests share the v8 baseline parameters.

Tests
-----
  BASELINE   : v8 full (reference)
  SHORT_ONLY : Zero long leg (long_scale=0), short leg only
  WIN_26_104 : Supply windows 26w fast + 104w slow
  WIN_52_104 : Supply windows 52w fast + 104w slow  (both long-term)
  WIN_39_78  : Supply windows 39w fast + 78w slow   (1.5x scaled)
  BTC_REGIME : BTC price replaces cap-weighted altcoin index in regime detection
  FUND_VETO  : Short only tokens with positive prior-period 8h funding (carry filter)
  BTC_LONG   : BTC perpetual replaces the altcoin long basket (0.75 long BTC)
  COMBINED_A : SHORT_ONLY + BTC_REGIME (simplified architecture)
  COMBINED_B : BTC_LONG   + BTC_REGIME (clean structural fix)
  COMBINED_C : BTC_LONG   + BTC_REGIME + WIN_52_104
"""
import sys, os, re, subprocess, tempfile, multiprocessing as mp
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH    = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
OUTPUT_DIR = "D:/AI_Projects/circ_supply/"
N_WORKERS  = max(1, mp.cpu_count() - 2)

with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

# v8 defaults (already in file, explicit for clarity)
V8 = {
    "BULL_BAND":             "1.05",
    "BEAR_BAND":             "0.95",
    "SUPPLY_WINDOW":         "26",
    "LONG_QUALITY_LOOKBACK": "12",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def param_patch(source, overrides):
    """Apply key = value overrides (single-line params only)."""
    for k, v in overrides.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        source = re.sub(pat, rf"\g<1>{v}\g<3>", source, flags=re.MULTILINE)
    return source


def suppress_plots(source):
    s = source.replace("plt.savefig", "pass  # plt.savefig")
    s = s.replace('print(f"[Plot]', 'pass  # print(f"[Plot]')
    return s


def run_source(source, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source)
        tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        if r.returncode != 0:
            return "__ERROR__\n" + r.stderr[-800:]
        return r.stdout
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)


def parse(stdout):
    if not stdout or stdout.startswith("__"):
        return None

    def find(pat, g=1, cast=float):
        m = re.search(pat, stdout)
        if not m:
            return float("nan")
        return cast(m.group(g).replace("%", "").replace("+", "").strip())

    ann    = find(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%")
    sharpe = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)")
    hac    = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
                  r"[\+\-]\d+\.\d+\s+([\+\-]\d+\.\d+)")
    dd_m   = re.search(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
                       r"[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%",
                       stdout)
    maxdd  = float(dd_m.group(1)) if dd_m else float("nan")
    win    = find(r"Win rate \(Long > Short, gross\)\s*:\s*\d+/\d+\s*\((\d+\.\d+)%\)")

    reg    = re.findall(r"\[Regime[^\]]*\]\s*Bull=(\d+)\s*Bear=(\d+)\s*Sideways=(\d+)", stdout)
    bull, bear, side = (int(reg[-1][0]), int(reg[-1][1]), int(reg[-1][2])) if reg else (0, 0, 0)

    # Net funding
    nf_m   = re.search(r"Net funding impact\s*:\s*([\+\-]\d+\.\d+)\s*\(", stdout)
    net_fund = float(nf_m.group(1)) if nf_m else float("nan")

    return dict(ann=ann, sharpe=sharpe, hac=hac, maxdd=maxdd, win=win,
                bull=bull, bear=bear, side=side, net_fund=net_fund)


# ---------------------------------------------------------------------------
# Source patches for each architectural variant
# ---------------------------------------------------------------------------

# ── A1: Pure short-only ────────────────────────────────────────────────────
OLD_LS_SCALE = """\
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),   # [V7-2] hold cash
    ("Sideways", True):  (0.00, 0.00),   # [V7-2] hold cash
    ("Bull",     False): (0.75, 0.75),   # [V7-6] symmetric L/S, same as Bear
    ("Bull",     True):  (0.50, 0.25),   # high-vol bull: scale back
    ("Bear",     False): (0.75, 0.75),   # unchanged from v4/v6
    ("Bear",     True):  (0.50, 0.25),   # unchanged from v4/v6
}"""

NEW_LS_SHORT_ONLY = """\
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),
    ("Sideways", True):  (0.00, 0.00),
    ("Bull",     False): (0.00, 0.75),   # SHORT-ONLY: zero long leg
    ("Bull",     True):  (0.00, 0.25),
    ("Bear",     False): (0.00, 0.75),
    ("Bear",     True):  (0.00, 0.25),
}"""

# ── A4: BTC-only regime ────────────────────────────────────────────────────
OLD_BUILD_REGIME = """\
def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["rank"] <= 100].copy().sort_values(["symbol", "snapshot_date"])
    top["pct_ret"] = top.groupby("symbol")["price"].pct_change(1)
    top = top[top["pct_ret"].notna()]

    def cap_wt(g):
        total = g["market_cap"].sum()
        return float((g["market_cap"] / total * g["pct_ret"]).sum()) if total > 0 else np.nan

    idx = (top.groupby("snapshot_date")
              .apply(cap_wt, include_groups=False)
              .reset_index().rename(columns={0: "index_return"})
              .sort_values("snapshot_date"))

    idx["index_price"] = (1 + idx["index_return"].fillna(0)).cumprod()
    idx["index_ma"]    = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    ratio              = idx["index_price"] / idx["index_ma"]
    idx["regime"]      = np.where(ratio >= BULL_BAND, "Bull",
                         np.where(ratio <= BEAR_BAND, "Bear", "Sideways"))

    btc_rets       = df[df["symbol"] == "BTC"].set_index("snapshot_date")["price"].pct_change(1)
    btc_vol_series = (btc_rets.reindex(idx["snapshot_date"])
                               .rolling(VOL_WINDOW, min_periods=4).std() * np.sqrt(52))
    idx["btc_vol_8w"] = btc_vol_series.values
    idx["high_vol"]   = idx["btc_vol_8w"] > HIGH_VOL_THRESHOLD

    n_bull = (idx["regime"] == "Bull").sum()
    n_bear = (idx["regime"] == "Bear").sum()
    n_side = (idx["regime"] == "Sideways").sum()
    print(f"[Regime] Bull={n_bull} Bear={n_bear} Sideways={n_side} "
          f"HighVol={idx['high_vol'].sum()}")
    return idx[["snapshot_date", "index_return", "regime", "high_vol"]]"""

NEW_BUILD_REGIME_BTC = """\
def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    # BTC-ONLY REGIME: Use BTC price directly instead of cap-weighted altcoin index.
    # Rationale: altcoin index ≈ 1.35×BTC + noise; simplifying removes the
    # single-token concentration noise from the top of the altcoin cap stack.
    btc = (df[df["symbol"] == "BTC"]
           .sort_values("snapshot_date")
           .copy())
    btc = btc[btc["price"].notna() & (btc["price"] > 0)].copy()
    btc["index_return"] = btc["price"].pct_change(1)
    btc = btc[btc["index_return"].notna()]

    idx = btc[["snapshot_date", "index_return"]].reset_index(drop=True).copy()
    idx["index_price"] = (1 + idx["index_return"]).cumprod()
    idx["index_ma"]    = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    ratio              = idx["index_price"] / idx["index_ma"]
    idx["regime"]      = np.where(ratio >= BULL_BAND, "Bull",
                         np.where(ratio <= BEAR_BAND, "Bear", "Sideways"))

    btc_rets       = btc.set_index("snapshot_date")["price"].pct_change(1)
    btc_vol_series = (btc_rets.reindex(idx["snapshot_date"])
                               .rolling(VOL_WINDOW, min_periods=4).std() * np.sqrt(52))
    idx["btc_vol_8w"] = btc_vol_series.values
    idx["high_vol"]   = idx["btc_vol_8w"] > HIGH_VOL_THRESHOLD

    n_bull = (idx["regime"] == "Bull").sum()
    n_bear = (idx["regime"] == "Bear").sum()
    n_side = (idx["regime"] == "Sideways").sum()
    print(f"[Regime-BTC] Bull={n_bull} Bear={n_bear} Sideways={n_side} "
          f"HighVol={idx['high_vol'].sum()}")
    return idx[["snapshot_date", "index_return", "regime", "high_vol"]]"""

# ── A5: Funding veto on short selection ───────────────────────────────────
# Inject after the momentum veto block: only short tokens where the prior
# period's mean 8h funding rate was positive (i.e., we receive carry).
# We load funding_mean from the parquet (funding_mean column exists there).
OLD_FUND_INJECT_ANCHOR = """\
        entry_short  = entry_short_raw - momentum_vetoed
        stay_short   = stay_short_raw   # stay positions not vetoed by momentum"""

NEW_FUND_INJECT_WITH_VETO = """\
        entry_short  = entry_short_raw - momentum_vetoed
        stay_short   = stay_short_raw   # stay positions not vetoed by momentum

        # FUNDING VETO: only short tokens where prior-period mean 8h funding > 0.
        # Positive funding = longs pay shorts = natural carry credit on the short.
        # Negative funding = shorts pay longs = carry penalty; skip these.
        if _fund_mean_piv is not None and t0 in _fund_mean_piv.index:
            _frow = _fund_mean_piv.loc[t0].dropna()
            _pos_carry = {s for s in _frow.index if float(_frow[s]) > 0}
            _entry_carry = entry_short & _pos_carry
            if len(_entry_carry) >= MIN_BASKET_SIZE:
                entry_short = _entry_carry"""

# Inject the funding_mean pivot load at the start of run_backtest
OLD_BACKTEST_START = """\
def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame,
                 bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw,
                 onboard_map: dict) -> dict:"""

NEW_BACKTEST_START_WITH_FUND = """\
def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame,
                 bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw,
                 onboard_map: dict) -> dict:
    # Load funding_mean pivot for carry veto (FUNDING_VETO mode)
    try:
        _fn_df = pd.read_parquet(BN_DIR + "/weekly_funding.parquet")
        _fn_df["cmc_date"] = _fn_df["week_start"] + pd.Timedelta(days=6)
        _fund_mean_piv = _fn_df.pivot_table(
            index="cmc_date", columns="symbol", values="funding_mean", aggfunc="mean")
    except Exception:
        _fund_mean_piv = None"""

# ── A6: BTC as long leg ───────────────────────────────────────────────────
OLD_LONG_BASKET_RETURN = \
    "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)"

NEW_LONG_BASKET_BTC = """\
        # BTC LONG LEG: Replace altcoin long basket with BTC perp position.
        # BTC's forward return is in fwd (from bn_price_piv); funding from fund_row.
        # Convention: fund_long_basket = what longs pay per period (positive = drag).
        # actual_fund_long_drag = -fund_long_basket < 0 when BTC funding > 0.
        _btc_fwd  = float(fwd["BTC"])  if ("BTC" in fwd.index  and pd.notna(fwd["BTC"]))  else np.nan
        _btc_fund = float(fund_row["BTC"]) if ("BTC" in fund_row.index and pd.notna(fund_row["BTC"])) else 0.0
        r_long_gross  = _btc_fwd
        slip_long     = TAKER_FEE      # BTC perp near-zero slippage at our size
        fund_long_basket = _btc_fund   # positive = longs pay = drag (sign matches basket_return)"""

# ---------------------------------------------------------------------------
# Build source for each test
# ---------------------------------------------------------------------------

def make_source(extra_params=None, code_patches=None):
    """Build a runnable source string with v8 defaults + extra patches."""
    s = BASE
    # Apply v8 + extra param overrides
    s = param_patch(s, {**V8, **(extra_params or {})})
    # Apply code-level patches (list of (old, new) tuples)
    for old, new in (code_patches or []):
        if old not in s:
            print(f"  [WARN] patch anchor not found: {old[:60]!r}")
        s = s.replace(old, new)
    s = suppress_plots(s)
    return s


def _fund_veto_patches():
    return [
        (OLD_BACKTEST_START, NEW_BACKTEST_START_WITH_FUND),
        (OLD_FUND_INJECT_ANCHOR, NEW_FUND_INJECT_WITH_VETO),
    ]


tests = [
    ("BASELINE",   make_source()),
    ("SHORT_ONLY", make_source(code_patches=[(OLD_LS_SCALE, NEW_LS_SHORT_ONLY)])),
    ("WIN_26_104", make_source(extra_params={"SUPPLY_WINDOW_SLOW": "104"})),
    ("WIN_52_104", make_source(extra_params={"SUPPLY_WINDOW": "52",
                                              "SUPPLY_WINDOW_SLOW": "104"})),
    ("WIN_39_78",  make_source(extra_params={"SUPPLY_WINDOW": "39",
                                              "SUPPLY_WINDOW_SLOW": "78"})),
    ("BTC_REGIME", make_source(code_patches=[(OLD_BUILD_REGIME, NEW_BUILD_REGIME_BTC)])),
    ("FUND_VETO",  make_source(code_patches=_fund_veto_patches())),
    ("BTC_LONG",   make_source(code_patches=[(OLD_LONG_BASKET_RETURN, NEW_LONG_BASKET_BTC)])),
    ("CMB_A",      make_source(code_patches=[  # SHORT_ONLY + BTC_REGIME
        (OLD_LS_SCALE,      NEW_LS_SHORT_ONLY),
        (OLD_BUILD_REGIME,  NEW_BUILD_REGIME_BTC),
    ])),
    ("CMB_B",      make_source(code_patches=[  # BTC_LONG + BTC_REGIME
        (OLD_LONG_BASKET_RETURN, NEW_LONG_BASKET_BTC),
        (OLD_BUILD_REGIME,       NEW_BUILD_REGIME_BTC),
    ])),
    ("CMB_C",      make_source(          # BTC_LONG + BTC_REGIME + WIN_52_104
        extra_params={"SUPPLY_WINDOW": "52", "SUPPLY_WINDOW_SLOW": "104"},
        code_patches=[
            (OLD_LONG_BASKET_RETURN, NEW_LONG_BASKET_BTC),
            (OLD_BUILD_REGIME,       NEW_BUILD_REGIME_BTC),
        ]
    )),
]

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_one(args):
    label, source = args
    out = run_source(source)
    m   = parse(out)
    if m is None:
        snippet = out[:300] if out else "(empty)"
        return label, None, snippet
    return label, m, None


if __name__ == "__main__":
    print("=" * 90)
    print("ARCHITECTURE TESTS — v8 Supply-Dilution L/S Strategy")
    print("No new data sources. All tests use existing CMC + Binance perp + funding data.")
    print("=" * 90)
    print(f"Running {len(tests)} tests on {N_WORKERS} workers ...\n")

    # Run baseline first so we can show deltas
    base_label, base_src = tests[0]
    print(f"  [{base_label}] running baseline ...", flush=True)
    _, base_m, base_err = _run_one((base_label, base_src))
    if base_err:
        print(f"  BASELINE ERROR: {base_err[:400]}")
        sys.exit(1)
    base_sharpe = base_m["sharpe"]
    print(f"  [{base_label}] Sharpe={base_sharpe:+.3f}  Ann={base_m['ann']:+.2f}%\n")

    print(f"  Running remaining {len(tests)-1} tests in parallel ...", flush=True)
    with mp.Pool(processes=min(N_WORKERS, len(tests) - 1)) as pool:
        rest = pool.map(_run_one, tests[1:])

    all_results = [(base_label, base_m, None)] + rest

    # ── Print table ──────────────────────────────────────────────────────────
    print()
    print(f"  {'Label':<12} {'Ann%':>8} {'Sharpe':>8} {'HAC':>8} {'MaxDD%':>8} "
          f"{'Win%':>6} {'B/Be/Sw':>10} {'NetFund':>9} {'dSR':>8}")
    print("  " + "-" * 95)

    for label, m, err in all_results:
        if err or m is None:
            print(f"  {label:<12} ERROR: {(err or '')[:60]}")
            continue
        dsr = m["sharpe"] - base_sharpe if label != base_label else float("nan")
        dsr_s = f"{dsr:>+7.3f}" if not (dsr != dsr) else "       "  # nan check
        regime_s = f"{m['bull']}/{m['bear']}/{m['side']}"
        flag = ""
        if not (dsr != dsr):
            if dsr >= 0.10:   flag = "  *** BETTER"
            elif dsr >= 0.05: flag = "  *  BETTER"
            elif dsr <= -0.10: flag = "  *** WORSE"
            elif dsr <= -0.05: flag = "  *  WORSE"
        print(f"  {label:<12} {m['ann']:>+7.2f}%  {m['sharpe']:>+7.3f}  "
              f"{m['hac']:>+7.3f}  {m['maxdd']:>+7.2f}%  {m['win']:>5.1f}%  "
              f"{regime_s:>10}  {m['net_fund']:>+8.4f}  {dsr_s}{flag}")

    print()
    print("  dSR: Sharpe delta vs v8 baseline. Positive = improvement. Threshold |dSR|>=0.05.")
    print("  B/Be/Sw: Bull / Bear / Sideways periods.")
    print("  NetFund: net funding P&L (cumulative fraction, long+short combined).")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)

    winners = [(l, m["sharpe"] - base_sharpe) for l, m, e in all_results[1:]
               if m and not (m["sharpe"] != m["sharpe"])]
    winners.sort(key=lambda x: -x[1])

    print(f"\n  Ranked by Sharpe improvement vs v8 baseline:")
    for rank, (l, d) in enumerate(winners, 1):
        tag = "BETTER" if d > 0.05 else ("NEUTRAL" if abs(d) <= 0.05 else "WORSE")
        print(f"    {rank:2}. {l:<12}  dSR={d:>+.3f}  [{tag}]")

    print("\nDone.")
