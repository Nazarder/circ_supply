"""
overfitting_tests.py
====================
Three overfitting diagnostics for perpetual_ls_v7.py:

  TEST 1 - Walk-Forward Split
    In-sample : 2022-01 to 2023-12  (24 periods, used to discover v8 params)
    Out-of-sample: 2024-01 to 2026-01  (21 periods, never seen)
    Compare v7 (original) vs v8 (combined) on IS and OOS windows.

  TEST 2 - Parameter Sensitivity Grid
    Grid over BULL_BAND x BEAR_BAND for both v7 signal (13w+52w) and
    v8 signal (26w+52w).  Visualises whether v8 sits on a broad plateau
    (robust) or a narrow spike (overfit).

  TEST 3 - Signal Permutation Test
    Shuffle pct_rank across symbols at every period (destroys supply signal,
    keeps costs/regime/sizing intact).  Run N_PERMS times.
    Real Sharpe vs permuted distribution -> p-value.
"""

import subprocess, sys, re, os, tempfile, time
import numpy as np
import multiprocessing as mp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

sys.stdout.reconfigure(encoding="utf-8")

V7_PATH     = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
OUTPUT_DIR  = "D:/AI_Projects/circ_supply/"
N_PERMS     = 200          # permutation simulations
N_WORKERS   = max(1, mp.cpu_count() - 1)

# ── v7 baseline params (already in file as defaults) ─────────────────────────
V7_OVERRIDES = {}

# ── v8 combined params ────────────────────────────────────────────────────────
V8_OVERRIDES = {
    "BULL_BAND":             "1.05",
    "BEAR_BAND":             "0.95",
    "LONG_QUALITY_LOOKBACK": "12",
    "SUPPLY_WINDOW":         "26",
}

# =============================================================================
#  SHARED HELPERS
# =============================================================================

with open(V7_PATH, encoding="utf-8") as f:
    _BASE_SOURCE = f.read()


def patch_source(overrides: dict, extra: dict = None) -> str:
    """Apply overrides + extra dict to v7 source. Suppresses plots."""
    s = _BASE_SOURCE
    all_ov = {**overrides, **(extra or {})}
    for key, val in all_ov.items():
        pattern = rf'^({re.escape(key)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
        s = re.sub(pattern, rf'\g<1>{val}\g<3>', s, flags=re.MULTILINE)
    s = s.replace("plt.savefig",    "pass  # plt.savefig")
    s = s.replace('print(f"[Plot]', 'pass  # print(f"[Plot]')
    return s


def run_patched(source: str, timeout: int = 180) -> str:
    """Write patched source to temp file, run, return stdout."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source)
        tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp],
                           capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout)
        if r.returncode != 0:
            return "__ERROR__\n" + r.stderr[-400:]
        return r.stdout
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)


def parse_sharpe(stdout: str) -> float:
    m = re.search(
        r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)',
        stdout)
    return float(m.group(1)) if m else np.nan


def parse_metrics(stdout: str) -> dict:
    def find(pat, g=1, cast=float):
        m = re.search(pat, stdout)
        return cast(m.group(g).replace("%","").replace("+","").strip()) if m else np.nan

    sharpe = find(
        r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)')
    sharpe_lo = find(
        r'L/S Combined \(net\).*?([\+\-]\d+\.\d+)\s+([\+\-]\d+\.\d+)\s*$', g=2)
    ann = find(r'L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%')
    dd_m = re.search(
        r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+'
        r'[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%', stdout)
    maxdd = float(dd_m.group(1)) if dd_m else np.nan
    bull = find(r'Bull\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    bear = find(r'Bear\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    periods = find(r'Rebalancing periods\s*:\s*(\d+)', cast=int)
    return dict(ann=ann, sharpe=sharpe, sharpe_lo=sharpe_lo,
                maxdd=maxdd, bull=bull, bear=bear, periods=periods)


def fmt(v, pct=True):
    if v is None or (isinstance(v, float) and v != v): return "   N/A"
    return f"{v:+.2f}%" if pct else f"{v:+.3f}"


# =============================================================================
#  TEST 1 -- WALK-FORWARD SPLIT
# =============================================================================

IS_END   = 'pd.Timestamp("2023-12-31")'
OOS_START = 'pd.Timestamp("2024-01-01")'

def run_walkforward():
    print("\n" + "="*70)
    print("TEST 1 -- WALK-FORWARD SPLIT")
    print("  In-sample  : 2022-01 -> 2023-12  (24 periods, params chosen here)")
    print("  Out-of-sample: 2024-01 -> 2026-01 (21 periods, never seen)")
    print("="*70)

    configs = [
        ("v7  IS",  V7_OVERRIDES, {"END_DATE":   IS_END}),
        ("v7  OOS", V7_OVERRIDES, {"START_DATE": OOS_START}),
        ("v8  IS",  V8_OVERRIDES, {"END_DATE":   IS_END}),
        ("v8  OOS", V8_OVERRIDES, {"START_DATE": OOS_START}),
    ]

    results = {}
    for label, base_ov, extra in configs:
        print(f"  Running {label} ...", flush=True)
        src = patch_source(base_ov, extra)
        out = run_patched(src)
        if out.startswith("__"):
            print(f"    ERROR: {out[:60]}")
            results[label] = {}
        else:
            results[label] = parse_metrics(out)

    print()
    hdr = f"  {'Config':<10} {'AnnRet':>8} {'MaxDD':>8} {'Sharpe':>8} {'SharpeHAC':>10} {'BullSprd':>9} {'BearSprd':>9} {'Periods':>8}"
    sep = "  " + "-"*72
    print(hdr); print(sep)
    for label, base_ov, extra in configs:
        r = results.get(label, {})
        print(f"  {label:<10}"
              f" {fmt(r.get('ann')):>8}"
              f" {fmt(r.get('maxdd')):>8}"
              f" {fmt(r.get('sharpe'), pct=False):>8}"
              f" {fmt(r.get('sharpe_lo'), pct=False):>10}"
              f" {fmt(r.get('bull')):>9}"
              f" {fmt(r.get('bear')):>9}"
              f" {str(int(r['periods'])) if r.get('periods')==r.get('periods') else 'N/A':>8}")

    # Degradation ratio
    print()
    for tag, label_is, label_oos in [("v7", "v7  IS", "v7  OOS"),
                                       ("v8", "v8  IS", "v8  OOS")]:
        s_is  = results.get(label_is,  {}).get("sharpe", np.nan)
        s_oos = results.get(label_oos, {}).get("sharpe", np.nan)
        if not np.isnan(s_is) and not np.isnan(s_oos) and s_is != 0:
            ratio = s_oos / s_is
            print(f"  {tag} OOS/IS Sharpe ratio: {s_oos:+.3f} / {s_is:+.3f} = {ratio:.2f}x"
                  f"  ({'good' if ratio > 0.5 else 'DEGRADED' if ratio > 0 else 'SIGN FLIP'})")


# =============================================================================
#  TEST 2 -- PARAMETER SENSITIVITY GRID
# =============================================================================

BULL_VALS   = [1.03, 1.05, 1.08, 1.10, 1.13, 1.15]
BEAR_VALS   = [0.85, 0.87, 0.90, 0.92, 0.95, 0.97]

def _grid_worker(args):
    bull, bear, supply_w = args
    ov = {
        "BULL_BAND":     str(bull),
        "BEAR_BAND":     str(bear),
        "SUPPLY_WINDOW": str(supply_w),
    }
    src = patch_source(ov)
    out = run_patched(src)
    sharpe = parse_sharpe(out) if not out.startswith("__") else np.nan
    return (bull, bear, supply_w, sharpe)


def run_sensitivity_grid():
    print("\n" + "="*70)
    print("TEST 2 -- PARAMETER SENSITIVITY GRID")
    print(f"  BULL_BAND x BEAR_BAND for signal windows 13w and 26w")
    print(f"  {len(BULL_VALS)} x {len(BEAR_VALS)} x 2 = {len(BULL_VALS)*len(BEAR_VALS)*2} combinations")
    print("="*70)

    tasks = [(b1, b2, sw)
             for b1 in BULL_VALS
             for b2 in BEAR_VALS
             for sw in [13, 26]]

    print(f"  Running {len(tasks)} combinations with {N_WORKERS} workers ...", flush=True)
    t0 = time.time()
    with mp.Pool(N_WORKERS) as pool:
        raw = pool.map(_grid_worker, tasks)
    print(f"  Done in {time.time()-t0:.0f}s")

    # Build matrices
    for sw, label in [(13, "13w+52w (v7 signal)"), (26, "26w+52w (v8 signal)")]:
        mat = np.full((len(BEAR_VALS), len(BULL_VALS)), np.nan)
        for bull, bear, supply_w, sharpe in raw:
            if supply_w == sw:
                ri = BEAR_VALS.index(bear)
                ci = BULL_VALS.index(bull)
                mat[ri, ci] = sharpe

        # Print table
        print(f"\n  Sharpe heatmap -- {label}")
        print(f"  {'Bear \\ Bull':>12}", end="")
        for b in BULL_VALS:
            print(f"  {b:.2f}", end="")
        print()
        for ri, bear in enumerate(BEAR_VALS):
            print(f"  {bear:.2f}      ", end="")
            for ci in range(len(BULL_VALS)):
                v = mat[ri, ci]
                mark = "*" if (BULL_VALS[ci] == 1.05 and bear == 0.95) else " "
                print(f"  {v:+.2f}{mark}", end="")
            print()

        # Plot heatmap
        fig, ax = plt.subplots(figsize=(8, 5))
        vmin = np.nanmin(mat); vmax = np.nanmax(mat)
        vcenter = 0.0
        norm = mcolors.TwoSlopeNorm(vmin=min(vmin, -0.1),
                                     vcenter=vcenter,
                                     vmax=max(vmax, 0.1))
        im = ax.imshow(mat, cmap="RdYlGn", norm=norm, aspect="auto")
        ax.set_xticks(range(len(BULL_VALS)))
        ax.set_xticklabels([f"{b:.2f}" for b in BULL_VALS])
        ax.set_yticks(range(len(BEAR_VALS)))
        ax.set_yticklabels([f"{b:.2f}" for b in BEAR_VALS])
        ax.set_xlabel("BULL_BAND"); ax.set_ylabel("BEAR_BAND")
        ax.set_title(f"Sharpe -- {label}\n(* = v8 choice)")
        for ri in range(len(BEAR_VALS)):
            for ci in range(len(BULL_VALS)):
                v = mat[ri, ci]
                if not np.isnan(v):
                    ax.text(ci, ri, f"{v:.2f}", ha="center", va="center",
                            fontsize=8,
                            color="black" if abs(v) < 0.5*(vmax-vmin)+0.001 else "white")
        # mark v8 choice
        if 1.05 in BULL_VALS and 0.95 in BEAR_VALS:
            ci = BULL_VALS.index(1.05); ri = BEAR_VALS.index(0.95)
            ax.add_patch(plt.Rectangle((ci-0.5, ri-0.5), 1, 1,
                         fill=False, edgecolor="blue", lw=3, label="v8"))
        plt.colorbar(im, ax=ax, label="Sharpe")
        plt.tight_layout()
        fname = f"{OUTPUT_DIR}sensitivity_{sw}w.png"
        plt.savefig(fname, dpi=120)
        plt.close()
        print(f"  [Plot] {fname}")


# =============================================================================
#  TEST 3 -- SIGNAL PERMUTATION TEST
# =============================================================================

def _perm_worker(seed):
    ov = {**V8_OVERRIDES, "PERMUTE_SEED": str(seed)}
    src = patch_source(ov)
    out = run_patched(src)
    return parse_sharpe(out) if not out.startswith("__") else np.nan


def run_permutation_test():
    print("\n" + "="*70)
    print("TEST 3 -- SIGNAL PERMUTATION TEST")
    print(f"  Shuffles pct_rank across symbols each period (destroys signal)")
    print(f"  {N_PERMS} simulations on v8 params | {N_WORKERS} workers")
    print("="*70)

    # Real v8 Sharpe
    print("  Running real v8 ...", flush=True)
    real_src = patch_source(V8_OVERRIDES)
    real_out = run_patched(real_src)
    real_sharpe = parse_sharpe(real_out)
    print(f"  Real v8 Sharpe: {real_sharpe:+.3f}")

    # Permuted runs
    print(f"  Running {N_PERMS} permutations ...", flush=True)
    t0 = time.time()
    seeds = list(range(N_PERMS))
    with mp.Pool(N_WORKERS) as pool:
        perm_sharpes = pool.map(_perm_worker, seeds)
    print(f"  Done in {time.time()-t0:.0f}s")

    perm_arr = np.array([s for s in perm_sharpes if not np.isnan(s)])
    n_valid  = len(perm_arr)

    print(f"\n  Permuted Sharpe distribution ({n_valid} valid runs):")
    print(f"    Mean  : {perm_arr.mean():+.3f}")
    print(f"    Std   : {perm_arr.std():+.3f}")
    print(f"    5th   : {np.percentile(perm_arr, 5):+.3f}")
    print(f"    95th  : {np.percentile(perm_arr, 95):+.3f}")
    print(f"    Max   : {perm_arr.max():+.3f}")
    print(f"\n  Real Sharpe : {real_sharpe:+.3f}")

    p_val = (perm_arr >= real_sharpe).mean()
    print(f"  p-value     : {p_val:.4f}  ({(perm_arr >= real_sharpe).sum()}/{n_valid} perms >= real)")
    if p_val < 0.01:
        verdict = "STRONG evidence the supply signal has real edge (p < 0.01)"
    elif p_val < 0.05:
        verdict = "Moderate evidence of real edge (p < 0.05)"
    elif p_val < 0.10:
        verdict = "Weak evidence (p < 0.10) -- borderline"
    else:
        verdict = "NO evidence of real edge -- returns may be from parameter fitting alone"
    print(f"  Verdict     : {verdict}")

    # Plot
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(perm_arr, bins=30, color="steelblue", alpha=0.7,
            label=f"Permuted ({n_valid} runs)")
    ax.axvline(real_sharpe, color="red", lw=2.5,
               label=f"Real v8 Sharpe = {real_sharpe:+.3f}")
    ax.axvline(np.percentile(perm_arr, 95), color="orange", lw=1.5,
               ls="--", label="95th pct permuted")
    ax.set_xlabel("Sharpe Ratio")
    ax.set_ylabel("Count")
    ax.set_title(f"Permutation Test -- Supply Signal\n"
                 f"p-value = {p_val:.4f}  |  {verdict}")
    ax.legend()
    plt.tight_layout()
    fname = f"{OUTPUT_DIR}permutation_test.png"
    plt.savefig(fname, dpi=120)
    plt.close()
    print(f"  [Plot] {fname}")


# =============================================================================
#  MAIN
# =============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("OVERFITTING DIAGNOSTICS  --  perpetual_ls_v7")
    print("=" * 70)
    print(f"Using {N_WORKERS} parallel workers")

    run_walkforward()
    run_sensitivity_grid()
    run_permutation_test()

    print("\n" + "="*70)
    print("All tests complete.")
    print("="*70)
