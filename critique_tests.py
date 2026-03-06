"""
critique_tests.py
=================
Tests every actionable finding from the systematic v9 critique (2026-03-06).

Published v9 baseline: SR=+0.966, Ann=+17.11%, MaxDD=-13.06% (45 periods)

Tests
-----
  A0  v9 baseline — confirm published numbers
  A1  IS only  (2022-2023, bear market)
  A2  OOS only (2024-2026, bull market)

  B0  ZEC excluded — single-token alpha dependency
  B1  ZEC excluded, IS only
  B2  ZEC excluded, OOS only  ← where the big event lives

  C   BTC_LONG — replace altcoin long basket with BTC perp

  D   MIN_SUPPLY_HISTORY=32 — fix mismatch with SUPPLY_WINDOW=32

  E1  Slippage k=0.002 (4×)  — realistic mid-cap alt execution
  E2  Slippage k=0.004 (8×)
  E3  Slippage k=0.006 (12×)

  F   Turnover-adjusted fees  — pay taker only on traded fraction, not flat 100%

  G   Turnover-adjusted fees + sideways state reset
      (reset prev sets to {} on sideways → re-entry pays full entry taker)

  H   BTC_LONG + realistic slippage k=0.002
  I   BTC_LONG + turnover-adjusted fees + k=0.002  (full realistic)
  J   ZEC excluded + k=0.002
  K   ZEC excluded + BTC_LONG
  L   ZEC excluded + BTC_LONG + k=0.002  (kitchen sink)
"""

import sys, os, re, subprocess, tempfile, time
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

V9_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"


# ===========================================================================
#  Core helpers
# ===========================================================================

def _load():
    with open(V9_PATH, encoding="utf-8") as f:
        return f.read()


def _patch_param(src, key, val):
    """Patch a top-level constant: KEY = <anything>  [# comment]"""
    return re.sub(
        rf"^({re.escape(key)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$",
        rf"\g<1>{val}\3",
        src, flags=re.MULTILINE
    )


def _run_src(src, timeout=380):
    src = src.replace("plot_results(results)", "pass  # plots suppressed")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(src)
        tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse(out):
    nan = float("nan")
    if not out or "__TIMEOUT__" in out:
        return nan, nan, nan, 0
    def f(pat):
        m = re.search(pat, out)
        if not m:
            return nan
        return float(m.group(1).replace("%", "").replace("+", "").strip())
    ann    = f(r"L/S Combined \(net\)\s+([\+\-]?\d+\.\d+)%")
    sharpe = f(r"L/S Combined \(net\)\s+[\+\-]?\d+\.\d+%\s+[\+\-]?\d+\.\d+%\s+([\+\-]?\d+\.\d+)")
    maxdd  = f(r"L/S Combined \(net\)\s+[\+\-]?\d+\.\d+%\s+[\+\-]?\d+\.\d+%\s+"
               r"[\+\-]?\d+\.\d+\s+[\+\-]?\d+\.\d+\s+[\+\-]?\d+\.\d+\s+([\+\-]?\d+\.\d+)%")
    m2 = re.search(r"Rebalancing periods\s*:\s*(\d+)", out)
    return ann, sharpe, maxdd, int(m2.group(1)) if m2 else 0


def _run(params=None, src_fns=None, start=None, end=None):
    """
    params   : dict of constant overrides (regex-patched)
    src_fns  : list of functions (src -> src) for structural patches
    start/end: date range overrides
    """
    src = _load()
    if params:
        for k, v in params.items():
            src = _patch_param(src, k, v)
    if start:
        src = _patch_param(src, "START_DATE", f'pd.Timestamp("{start}")')
    if end:
        src = _patch_param(src, "END_DATE", f'pd.Timestamp("{end}")')
    if src_fns:
        for fn in src_fns:
            src = fn(src)
    return _parse(_run_src(src))


# ===========================================================================
#  Source-level patch functions
# ===========================================================================

def patch_zec_exclusion(src):
    """Add ZEC to the EXCLUDED set."""
    OLD = '| COMMODITY_BACKED)'
    NEW = '| COMMODITY_BACKED | {"ZEC"})'
    if OLD not in src:
        print("  [WARN] ZEC patch: target not found")
    return src.replace(OLD, NEW, 1)


def patch_btc_long(src):
    """Replace altcoin long basket return with BTC perp return."""
    OLD = ('        r_long_gross,  slip_long,  fund_long_basket  '
           '= basket_return(basket_long)')
    NEW = (OLD + '\n'
           '        # BTC_LONG: override altcoin long with BTC perp\n'
           '        _btc_r = float(fwd.get("BTC", np.nan))\n'
           '        if not np.isnan(_btc_r):\n'
           '            r_long_gross = _btc_r\n'
           '            fund_long_basket = (\n'
           '                float(fund_row["BTC"])\n'
           '                if "BTC" in fund_row.index and pd.notna(fund_row["BTC"])\n'
           '                else 0.0)\n')
    if OLD not in src:
        print("  [WARN] BTC_LONG patch: target not found")
    return src.replace(OLD, NEW, 1)


def patch_turnover_fees(src):
    """Pay taker fees only on the traded fraction, not flat 100% every period."""
    OLD = ('        fee_cost = 2 * TAKER_FEE\n'
           '\n'
           '        actual_fund_long_drag  = -fund_long_basket\n'
           '        actual_fund_short_cred = +fund_short_basket\n'
           '\n'
           '        r_long_net  = r_long_gross  - fee_cost - slip_long  + actual_fund_long_drag\n'
           '        r_short_net = -r_short_gross - fee_cost - slip_short + actual_fund_short_cred')
    NEW = ('        # Turnover-adjusted fees: pay taker only on traded fraction\n'
           '        fee_cost_long  = to_l * 2 * TAKER_FEE\n'
           '        fee_cost_short = to_s * 2 * TAKER_FEE\n'
           '\n'
           '        actual_fund_long_drag  = -fund_long_basket\n'
           '        actual_fund_short_cred = +fund_short_basket\n'
           '\n'
           '        r_long_net  = r_long_gross  - fee_cost_long  - slip_long  + actual_fund_long_drag\n'
           '        r_short_net = -r_short_gross - fee_cost_short - slip_short + actual_fund_short_cred')
    if OLD not in src:
        print("  [WARN] turnover fees patch: target not found")
        # Debug: show what we find around 'fee_cost'
        idx = src.find('fee_cost = 2 * TAKER_FEE')
        if idx >= 0:
            print(f"  [DBG] found 'fee_cost = 2 * TAKER_FEE' at char {idx}")
            print(repr(src[idx-10:idx+120]))
    return src.replace(OLD, NEW, 1)


def patch_sideways_reset(src):
    """
    Inside the sideways cash branch, reset prev_long_set / prev_short_set to {}
    before continue — forces full entry taker cost on re-emergence from cash.
    """
    OLD = ('            fund_actual_short_l.append(0.0)\n'
           '            continue')
    NEW = ('            fund_actual_short_l.append(0.0)\n'
           '            # Sideways reset: full re-entry taker cost on next active period\n'
           '            prev_long_set  = set()\n'
           '            prev_short_set = set()\n'
           '            continue')
    if OLD not in src:
        print("  [WARN] sideways reset patch: target not found")
    return src.replace(OLD, NEW, 1)


# ===========================================================================
#  Test runner
# ===========================================================================

def fmt(v, pct=False):
    if np.isnan(v):
        return "   N/A"
    return f"{v:>+7.2f}%" if pct else f"{v:>+6.3f}"


if __name__ == "__main__":
    t0_total = time.time()

    print("\n" + "=" * 82)
    print("  CRITIQUE TESTS — v9 Supply-Dilution L/S")
    print("  Published baseline: SR=+0.966, Ann=+17.11%, MaxDD=-13.06% (45 periods)")
    print("=" * 82)

    # (label, params, src_fns, start, end, note)
    TESTS = [
        # ── Baseline & sub-periods ──────────────────────────────────────────
        ("A0  v9 baseline (confirm)",
            None, None, None, None,
            "confirm"),
        ("A1  IS only  2022-2023 (bear)",
            None, None, "2022-01-01", "2023-12-31",
            "regime sanity check"),
        ("A2  OOS only 2024-2026 (bull)",
            None, None, "2024-01-01", None,
            "regime sanity check"),

        # ── ZEC single-token dependency ─────────────────────────────────────
        ("B0  ZEC excluded (full period)",
            None, [patch_zec_exclusion], None, None,
            "critique §4A: 46%+ alpha from 1 event"),
        ("B1  ZEC excluded, IS only",
            None, [patch_zec_exclusion], "2022-01-01", "2023-12-31",
            "ZEC impact is OOS-only?"),
        ("B2  ZEC excluded, OOS only",
            None, [patch_zec_exclusion], "2024-01-01", None,
            "ZEC impact is OOS-only?"),

        # ── BTC_LONG architecture ────────────────────────────────────────────
        ("C   BTC_LONG (replace alt long)",
            None, [patch_btc_long], None, None,
            "critique §4A: alt long is aesthetic"),

        # ── Data/parameter fixes ─────────────────────────────────────────────
        ("D   MIN_SUPPLY_HISTORY=32 (fix)",
            {"MIN_SUPPLY_HISTORY": "32"}, None, None, None,
            "critique §1C: mismatch with SW=32"),

        # ── Slippage reality ─────────────────────────────────────────────────
        ("E1  k=0.002  (4× baseline)",
            {"SLIPPAGE_K": "0.002"}, None, None, None,
            "critique §1E / Test3: realistic execution"),
        ("E2  k=0.004  (8× baseline)",
            {"SLIPPAGE_K": "0.004"}, None, None, None,
            ""),
        ("E3  k=0.006  (12× baseline)",
            {"SLIPPAGE_K": "0.006"}, None, None, None,
            ""),

        # ── Fee model fixes ───────────────────────────────────────────────────
        ("F   Turnover-adjusted fees",
            None, [patch_turnover_fees], None, None,
            "critique §3C: flat fee assumes 100% turnover"),
        ("G   TO-fees + sideways state reset",
            None, [patch_turnover_fees, patch_sideways_reset], None, None,
            "critique §3E: re-entry from cash pays full taker"),

        # ── Combined realistic scenarios ──────────────────────────────────────
        ("H   BTC_LONG + k=0.002",
            {"SLIPPAGE_K": "0.002"}, [patch_btc_long], None, None,
            "BTC_LONG under realistic slippage"),
        ("I   BTC_LONG + TO-fees + k=0.002",
            {"SLIPPAGE_K": "0.002"}, [patch_btc_long, patch_turnover_fees], None, None,
            "fully realistic BTC_LONG"),
        ("J   ZEC excl + k=0.002",
            {"SLIPPAGE_K": "0.002"}, [patch_zec_exclusion], None, None,
            "ZEC-free under realistic slippage"),
        ("K   ZEC excl + BTC_LONG",
            None, [patch_zec_exclusion, patch_btc_long], None, None,
            "ZEC-free + better long leg"),
        ("L   ZEC excl + BTC_LONG + k=0.002",
            {"SLIPPAGE_K": "0.002"}, [patch_zec_exclusion, patch_btc_long], None, None,
            "kitchen sink: all main fixes"),
    ]

    print(f"\n  {'Test':<38}  {'SR':>7}  {'Ann':>8}  {'MaxDD':>8}  {'N':>4}  {'dSR':>8}")
    print("  " + "-" * 82)

    baseline_sr = float("nan")
    results = {}

    for label, params, src_fns, start, end, note in TESTS:
        t_start = time.time()
        ann, sr, dd, n = _run(params, src_fns, start, end)
        elapsed = time.time() - t_start
        results[label] = (ann, sr, dd, n)

        if "baseline (confirm)" in label:
            baseline_sr = sr
            dsr_str = " (baseline)"
        elif np.isnan(sr) or np.isnan(baseline_sr):
            dsr_str = "    N/A"
        else:
            dsr_str = f"{sr - baseline_sr:>+8.3f}"

        ann_s = f"{ann:>+7.2f}%" if not np.isnan(ann) else "    N/A"
        sr_s  = f"{sr:>+6.3f}"  if not np.isnan(sr)  else "   N/A"
        dd_s  = f"{dd:>+7.2f}%" if not np.isnan(dd)  else "    N/A"

        print(f"  {label:<38}  {sr_s}  {ann_s}  {dd_s}  {n:>4}  {dsr_str}"
              f"   [{elapsed:.0f}s]", flush=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 82)
    print("  SUMMARY")
    print("=" * 82)

    # Issue: ZEC single-token dependency
    b0_sr = results.get("B0  ZEC excluded (full period)", (float("nan"),)*4)[1]
    zec_dep = baseline_sr - b0_sr
    print(f"\n  ZEC dependency:        SR drops {zec_dep:>+.3f} when ZEC excluded  "
          f"({zec_dep/baseline_sr*100:.0f}% of total Sharpe)")

    # Issue: long leg vs BTC_LONG
    c_sr = results.get("C   BTC_LONG (replace alt long)", (float("nan"),)*4)[1]
    print(f"  BTC_LONG gain:         +{c_sr - baseline_sr:.3f} dSR vs altcoin long")

    # Issue: slippage reality
    e1_sr = results.get("E1  k=0.002  (4× baseline)", (float("nan"),)*4)[1]
    print(f"  Slippage k=0.002:      SR = {e1_sr:>+.3f}  (vs {baseline_sr:>+.3f} at k=0.0005)")

    # Issue: fee model
    f_sr = results.get("F   Turnover-adjusted fees", (float("nan"),)*4)[1]
    g_sr = results.get("G   TO-fees + sideways state reset", (float("nan"),)*4)[1]
    print(f"  TO-adjusted fees:      SR = {f_sr:>+.3f}  (delta {f_sr-baseline_sr:>+.3f}; fees slightly lower)")
    print(f"  + sideways reset:      SR = {g_sr:>+.3f}  (delta {g_sr-baseline_sr:>+.3f}; re-entry cost charged)")

    # Issue: MIN_SUPPLY_HISTORY mismatch
    d_sr = results.get("D   MIN_SUPPLY_HISTORY=32 (fix)", (float("nan"),)*4)[1]
    print(f"  MIN_SUPPLY_HISTORY=32: SR = {d_sr:>+.3f}  (delta {d_sr-baseline_sr:>+.3f}; expect ~0)")

    # Best realistic scenario
    l_sr  = results.get("L   ZEC excl + BTC_LONG + k=0.002", (float("nan"),)*4)[1]
    l_ann = results.get("L   ZEC excl + BTC_LONG + k=0.002", (float("nan"),)*4)[0]
    print(f"\n  Most realistic (L): ZEC excl + BTC_LONG + k=0.002 → SR={l_sr:>+.3f}, Ann={l_ann:>+.2f}%")

    print(f"\n  Total runtime: {(time.time()-t0_total)/60:.1f} min")
    print("=" * 82)
