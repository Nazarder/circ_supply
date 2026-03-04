"""
run_experiments.py
==================
Runs perpetual_ls_v7.py with parameter overrides for each experiment.
Patches config vars in a temp copy, runs subprocess, parses key metrics.
"""

import subprocess
import sys
import re
import os
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

V7_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"

# ─────────────────────────────────────────────────────────────────────────────
#  EXPERIMENTS
# ─────────────────────────────────────────────────────────────────────────────
BASELINE = {"label": "Baseline (current v7)", "overrides": {}}

EXPERIMENTS = [
    # ── Group 1: Start date ──────────────────────────────────────────────────
    {
        "group": "1  Start Date",
        "label": "Pre-2022 (start 2021-01-01)",
        "overrides": {
            "START_DATE": 'pd.Timestamp("2021-01-01")',
        },
    },

    # ── Group 2: Winsorization ────────────────────────────────────────────────
    {
        "group": "2  Winsorization",
        "label": "Wins 5-95%",
        "overrides": {"SUPPLY_INF_WINS": "(0.05, 0.95)"},
    },
    {
        "group": "2  Winsorization",
        "label": "Wins 10-90%",
        "overrides": {"SUPPLY_INF_WINS": "(0.10, 0.90)"},
    },
    {
        "group": "2  Winsorization",
        "label": "No winsorization",
        "overrides": {"SUPPLY_INF_WINS": "(0.00, 1.00)"},
    },

    # ── Group 3: Signal windows ───────────────────────────────────────────────
    {
        "group": "3  Signal Windows",
        "label": "26w + 52w",
        "overrides": {
            "SUPPLY_WINDOW": "26",
            "SIGNAL_SLOW_WEIGHT": "0.50",
        },
    },
    {
        "group": "3  Signal Windows",
        "label": "52w only",
        "overrides": {"SIGNAL_SLOW_WEIGHT": "1.00"},
    },
    {
        "group": "3  Signal Windows",
        "label": "13w only",
        "overrides": {"SIGNAL_SLOW_WEIGHT": "0.00"},
    },

    # ── Group 4: Entry/exit bands ─────────────────────────────────────────────
    {
        "group": "4  Entry/Exit Bands",
        "label": "Wide bands 10/20",
        "overrides": {
            "LONG_ENTRY_PCT":  "0.10",
            "LONG_EXIT_PCT":   "0.20",
            "SHORT_ENTRY_PCT": "0.90",
            "SHORT_EXIT_PCT":  "0.80",
        },
    },
    {
        "group": "4  Entry/Exit Bands",
        "label": "Tight bands 13/15",
        "overrides": {
            "LONG_ENTRY_PCT":  "0.13",
            "LONG_EXIT_PCT":   "0.15",
            "SHORT_ENTRY_PCT": "0.87",
            "SHORT_EXIT_PCT":  "0.85",
        },
    },

    # ── Group 5: Long quality veto ────────────────────────────────────────────
    {
        "group": "5  Long Quality Veto",
        "label": "LQ veto 20%",
        "overrides": {"LONG_QUALITY_VETO_PCT": "0.20"},
    },
    {
        "group": "5  Long Quality Veto",
        "label": "LQ veto 25%",
        "overrides": {"LONG_QUALITY_VETO_PCT": "0.25"},
    },
    {
        "group": "5  Long Quality Veto",
        "label": "LQ lookback 3m",
        "overrides": {"LONG_QUALITY_LOOKBACK": "3"},
    },
    {
        "group": "5  Long Quality Veto",
        "label": "LQ lookback 12m",
        "overrides": {"LONG_QUALITY_LOOKBACK": "12"},
    },

    # ── Group 6: Regime bands ─────────────────────────────────────────────────
    {
        "group": "6  Regime Bands",
        "label": "Tight bands 1.05/0.95",
        "overrides": {
            "BULL_BAND": "1.05",
            "BEAR_BAND": "0.95",
        },
    },
    {
        "group": "6  Regime Bands",
        "label": "Wide bands 1.15/0.85",
        "overrides": {
            "BULL_BAND": "1.15",
            "BEAR_BAND": "0.85",
        },
    },
    {
        "group": "6  Regime Bands",
        "label": "No sideways cash (always trade)",
        "overrides": {
            # Replace sideways rows to (0.75, 0.75) instead of (0.00, 0.00)
            "_SIDEWAYS_SCALE": "(0.75, 0.75)",
        },
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def patch_source(source: str, overrides: dict) -> str:
    """Apply parameter overrides to v7 source via regex replacement."""
    s = source

    for key, val in overrides.items():

        # Special case: sideways scale override patches the REGIME_LS_SCALE dict
        if key == "_SIDEWAYS_SCALE":
            # Replace (0.00, 0.00) only in Sideways lines
            s = re.sub(
                r'("Sideways",\s*(False|True)\):\s*)\(0\.00,\s*0\.00\)',
                r'\g<1>' + val,
                s,
            )
            continue

        # Standard scalar / tuple override
        # Match:  KEY   =   <anything up to comment or newline>
        pattern = rf'^({re.escape(key)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
        replacement = rf'\g<1>{val}\g<3>'
        new_s = re.sub(pattern, replacement, s, flags=re.MULTILINE)
        # warn only if truly no match (not just same value)
        if not re.search(rf'^{re.escape(key)}\s*=', new_s, re.MULTILINE):
            print(f"  [WARN] override for '{key}' matched nothing")
        s = new_s

    # Suppress plots (no display needed, saves time)
    s = s.replace(
        'plt.savefig',
        'pass  # plt.savefig',
    )
    s = s.replace(
        'print(f"[Plot]',
        'pass  # print(f"[Plot]',
    )
    return s


def parse_metrics(stdout: str) -> dict:
    """Extract key metrics from v7 stdout."""
    m = {}

    def find(pattern, group=1, cast=float, default=None):
        match = re.search(pattern, stdout)
        if match:
            try:
                return cast(match.group(group).replace('%','').replace('+','').strip())
            except Exception:
                return default
        return default

    m["ann_ret"]     = find(r'L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%')
    m["max_dd"]      = find(r'L/S Combined \(net\).*?([\-]\d+\.\d+)%\s*$',
                            group=1)
    m["sharpe"]      = find(r'L/S Combined \(net\).*?[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)')
    m["sharpe_lo"]   = find(r'L/S Combined \(net\).*?([\+\-]\d+\.\d+)\s+([\+\-]\d+\.\d+)\s*$',
                            group=2)
    m["bull_spread"] = find(r'Bull\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    m["bear_spread"] = find(r'Bear\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    m["periods"]     = find(r'Rebalancing periods\s*:\s*(\d+)', cast=int)
    m["avg_long"]    = find(r'Avg basket size\s*:\s*Long ([\d\.]+)', cast=float)
    m["avg_short"]   = find(r'Avg basket size\s*:\s*Long [\d\.]+ \| Short ([\d\.]+)', cast=float)
    m["win_rate"]    = find(r'Win rate \(Long > Short, gross\)\s*:\s*\d+/\d+ \(([\d\.]+)%\)')
    m["trades"]      = find(r'All legs\s*:\s*(\d+) trades', cast=int)
    m["turnover_l"]  = find(r'Avg monthly turnover:\s*Long ([\d\.]+)%', cast=float)
    m["turnover_s"]  = find(r'Avg monthly turnover:\s*Long [\d\.]+%\s+Short ([\d\.]+)%', cast=float)
    m["net_funding"] = find(r'Net funding impact\s*:\s*[\-\+]\d+\.\d+ \(([\-\+]\d+\.\d+)%\)')

    # MaxDD from the stats table row for combined
    dd_match = re.search(
        r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%',
        stdout
    )
    if dd_match:
        m["max_dd"] = float(dd_match.group(1))

    return m


def run_experiment(label: str, overrides: dict, source: str) -> dict:
    patched = patch_source(source, overrides)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False,
                                     encoding='utf-8') as f:
        f.write(patched)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True, encoding='utf-8',
            timeout=300
        )
        stdout = result.stdout
        if result.returncode != 0:
            print(f"\n  [ERROR] {label}")
            print(result.stderr[-800:])
            return {"label": label, "error": True}
        metrics = parse_metrics(stdout)
        metrics["label"] = label
        metrics["error"] = False
        return metrics
    except subprocess.TimeoutExpired:
        print(f"\n  [TIMEOUT] {label}")
        return {"label": label, "error": True}
    finally:
        os.unlink(tmp_path)


def fmt(v, pct=True, plus=True):
    if v is None or (isinstance(v, float) and (v != v)):
        return "   N/A"
    if pct:
        s = f"{v:+.2f}%" if plus else f"{v:.2f}%"
    else:
        s = f"{v:+.3f}" if plus else f"{v:.3f}"
    return s


def fmt_plain(v, decimals=1):
    if v is None or (isinstance(v, float) and (v != v)):
        return "  N/A"
    return f"{v:.{decimals}f}"


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    with open(V7_PATH, encoding='utf-8') as f:
        source = f.read()

    all_experiments = [BASELINE] + EXPERIMENTS
    results = []

    for i, exp in enumerate(all_experiments):
        label = exp["label"]
        overrides = exp.get("overrides", {})
        tag = f"[{i}/{len(all_experiments)-1}]"
        print(f"{tag} Running: {label} ...", flush=True)
        r = run_experiment(label, overrides, source)
        r["group"] = exp.get("group", "0  Baseline")
        results.append(r)

    # ── Print results table ───────────────────────────────────────────────────
    print("\n")
    print("=" * 120)
    print("EXPERIMENT RESULTS SUMMARY")
    print("=" * 120)

    header = (
        f"{'Label':<35} {'AnnRet':>8} {'MaxDD':>8} {'Sharpe':>8} {'SharpeHAC':>10} "
        f"{'BullSprd':>9} {'BearSprd':>9} {'WinRate':>8} "
        f"{'Periods':>8} {'AvgL':>5} {'AvgS':>5} {'Trades':>7} "
        f"{'TurnL':>6} {'TurnS':>6} {'Funding':>8}"
    )
    sep = "-" * 120

    current_group = None
    for r in results:
        grp = r.get("group", "")
        if grp != current_group:
            current_group = grp
            print(f"\n  -- {grp} {'-' * (50 - len(grp))}")
            print(header)
            print(sep)

        if r.get("error"):
            print(f"  {'ERROR':>35}")
            continue

        lbl = r["label"][:34]
        print(
            f"  {lbl:<35}"
            f" {fmt(r.get('ann_ret')):>8}"
            f" {fmt(r.get('max_dd')):>8}"
            f" {fmt(r.get('sharpe'), pct=False):>8}"
            f" {fmt(r.get('sharpe_lo'), pct=False):>10}"
            f" {fmt(r.get('bull_spread')):>9}"
            f" {fmt(r.get('bear_spread')):>9}"
            f" {fmt(r.get('win_rate'), plus=False):>8}"
            f" {fmt_plain(r.get('periods'), 0):>8}"
            f" {fmt_plain(r.get('avg_long')):>5}"
            f" {fmt_plain(r.get('avg_short')):>5}"
            f" {fmt_plain(r.get('trades'), 0):>7}"
            f" {fmt_plain(r.get('turnover_l')):>6}"
            f" {fmt_plain(r.get('turnover_s')):>6}"
            f" {fmt(r.get('net_funding')):>8}"
        )

    print("=" * 120)
    print("Done.")


if __name__ == "__main__":
    main()
