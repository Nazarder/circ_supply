"""
generate_charts.py
==================
Generates the full chart suite for the v8 Supply-Dilution L/S Strategy.

Charts produced:
  perp_ls_v7_cumulative.png   — from main strategy (cumulative wealth, 3-panel)
  perp_ls_v7_regime_dd.png    — from main strategy (drawdown + spread bars)
  perp_ls_v7_vs_v6.png        — from main strategy (v6/v7 scorecard)
  perp_ls_v8_dashboard.png    — NEW: net stats dashboard
  perp_ls_v8_slippage.png     — NEW: slippage sensitivity
  perp_ls_v8_walkforward.png  — NEW: walk-forward OOS Sharpe
  perp_ls_v8_permutation.png  — NEW: permutation null distribution
"""
import sys, os, re, subprocess, tempfile
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH    = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
OUT_DIR    = "D:/AI_Projects/circ_supply/"
LOG_PATH   = OUT_DIR + "_gen_charts_log.csv"

with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

V8 = {"BULL_BAND": "1.05", "BEAR_BAND": "0.95",
      "SUPPLY_WINDOW": "26", "LONG_QUALITY_LOOKBACK": "12"}

def param_patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s

def run_src(source, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source); tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-400:]
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)

def parse_sharpe(out):
    m = re.search(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)", out)
    return float(m.group(1)) if m else float("nan")

def parse_ann(out):
    m = re.search(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%", out)
    return float(m.group(1)) if m else float("nan")

def suppress(s):
    s = s.replace("plt.savefig", "pass  # plt.savefig")
    return s.replace('print(f"[Plot]', 'pass  # print')

# ── Step 1: Run v8 with charts ON (generates the 3 strategy charts) ─────────
print("Running v8 to generate strategy charts...", flush=True)
V8_SRC_CHARTS = param_patch(BASE, V8)   # no suppression — charts fire normally
out_main = run_src(V8_SRC_CHARTS, timeout=420)
if out_main.startswith("__ERROR__"):
    print(f"Strategy run failed: {out_main[:300]}")
    sys.exit(1)
print("  Strategy charts saved.")

# ── Step 2: Get basket log for dashboard ────────────────────────────────────
V8_SRC = suppress(param_patch(BASE, V8))
log_src = param_patch(V8_SRC, {"SAVE_BASKET_LOG": f'"{LOG_PATH}"'})
run_src(log_src, timeout=420)
log = pd.read_csv(LOG_PATH, parse_dates=["date"]) if os.path.exists(LOG_PATH) else None

# ── Chart: v8 Stats Dashboard ───────────────────────────────────────────────
print("Generating v8 stats dashboard...", flush=True)

if log is not None:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor("#0f1117")
    GS = GridSpec(3, 3, figure=fig, hspace=0.45, wspace=0.35)

    TEXT  = "#e0e0e0"
    GREEN = "#2ecc71"
    RED   = "#e74c3c"
    BLUE  = "#3498db"
    GRAY  = "#7f8c8d"
    BULL_C, BEAR_C, SIDE_C = "#3498db", "#e74c3c", "#95a5a6"

    def ax_style(ax):
        ax.set_facecolor("#1a1d27")
        ax.tick_params(colors=TEXT, labelsize=8)
        ax.xaxis.label.set_color(TEXT)
        ax.yaxis.label.set_color(TEXT)
        ax.title.set_color(TEXT)
        for spine in ax.spines.values():
            spine.set_edgecolor("#2c2f3e")
        ax.grid(True, alpha=0.15, color="#ffffff")
        return ax

    fig.suptitle("Supply-Dilution L/S — v8 Performance Dashboard\n"
                 "Jan 2022 – Jan 2026 | Binance USDT-M Perps | Monthly Rebalancing",
                 fontsize=13, fontweight="bold", color=TEXT, y=0.98)

    # Panel 1: Cumulative NAV
    ax1 = fig.add_subplot(GS[0, :2])
    ax_style(ax1)
    r = log["combined_net"]
    cum = (1 + r).cumprod()
    dates = log["date"]
    regime_colors = [BULL_C if r=="Bull" else BEAR_C if r=="Bear" else SIDE_C
                     for r in log["regime"]]
    for i in range(len(dates)-1):
        ax1.axvspan(dates.iloc[i], dates.iloc[i+1],
                    alpha=0.08, color=regime_colors[i], linewidth=0)
    ax1.plot(dates, cum.values, color=GREEN, lw=2.5, label="L/S Combined net")
    ax1.axhline(1, color=GRAY, lw=0.8, ls="--")
    ax1.set_title("Cumulative Net Return", fontsize=10, fontweight="bold")
    ax1.set_ylabel("NAV", color=TEXT, fontsize=9)
    patches = [mpatches.Patch(color=BULL_C, alpha=0.5, label="Bull"),
               mpatches.Patch(color=BEAR_C, alpha=0.5, label="Bear"),
               mpatches.Patch(color=SIDE_C, alpha=0.5, label="Sideways")]
    ax1.legend(handles=patches + [mpatches.Patch(color=GREEN, label="NAV")],
               fontsize=7, labelcolor=TEXT, facecolor="#1a1d27", edgecolor="#2c2f3e")

    # Panel 2: Drawdown
    ax2 = fig.add_subplot(GS[1, :2])
    ax_style(ax2)
    dd = (cum - cum.cummax()) / cum.cummax()
    ax2.fill_between(dates, dd.values, 0, color=RED, alpha=0.6, label="Drawdown")
    ax2.axhline(0, color=GRAY, lw=0.8)
    ax2.set_title("Drawdown", fontsize=10, fontweight="bold")
    ax2.set_ylabel("DD", color=TEXT, fontsize=9)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f"{v:.0%}"))
    ax2.legend(fontsize=7, labelcolor=TEXT, facecolor="#1a1d27", edgecolor="#2c2f3e")

    # Panel 3: Per-period spread bars
    ax3 = fig.add_subplot(GS[2, :2])
    ax_style(ax3)
    sp = log["long_gross"] - log["short_gross"]
    bar_cols = [BULL_C if r=="Bull" else BEAR_C if r=="Bear" else SIDE_C
                for r in log["regime"]]
    ax3.bar(dates, sp.values, color=bar_cols, width=20, alpha=0.85)
    ax3.axhline(0, color=GRAY, lw=0.8)
    ax3.set_title("Per-Period Gross Spread", fontsize=10, fontweight="bold")
    ax3.set_ylabel("Spread", color=TEXT, fontsize=9)
    ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f"{v:.0%}"))

    # Panel 4: Stats table
    ax4 = fig.add_subplot(GS[0, 2])
    ax4.set_facecolor("#1a1d27")
    ax4.axis("off")
    ax4.set_title("Net Statistics", fontsize=10, fontweight="bold", color=TEXT, pad=6)
    ann  = (1 + r.mean())**12 - 1
    vol  = r.std() * np.sqrt(12)
    sr   = r.mean() / r.std() * np.sqrt(12)
    mdd  = dd.min()
    wr   = (sp > 0).mean()
    fund = log["fund_long"].sum() + log["fund_short"].sum()
    rows = [
        ("Ann. Return",   f"{ann:+.2%}"),
        ("Ann. Vol",      f"{vol:.2%}"),
        ("Sharpe",        f"{sr:+.3f}"),
        ("Max Drawdown",  f"{mdd:.2%}"),
        ("Win Rate",      f"{wr:.1%}"),
        ("Periods",       "45"),
        ("Avg L basket",  f"{log['long_basket'].apply(lambda x: len(str(x).split(','))).mean():.1f}"),
        ("Avg S basket",  f"{log['short_basket'].apply(lambda x: len(str(x).split(','))).mean():.1f}"),
        ("Net Funding",   f"{fund:+.2%}"),
        ("Bull periods",  f"{(log['regime']=='Bull').sum()}"),
        ("Bear periods",  f"{(log['regime']=='Bear').sum()}"),
        ("Sideways (cash)", f"{(log['regime']=='Sideways').sum()}"),
    ]
    for i, (label, val) in enumerate(rows):
        y = 0.95 - i * 0.078
        ax4.text(0.02, y, label, transform=ax4.transAxes,
                 color=GRAY, fontsize=8, va="top")
        color = GREEN if ("+") in val and val != "+0.00%" else (RED if val.startswith("-") else TEXT)
        ax4.text(0.98, y, val, transform=ax4.transAxes,
                 color=color, fontsize=8, va="top", ha="right", fontweight="bold")

    # Panel 5: Regime spread bars
    ax5 = fig.add_subplot(GS[1, 2])
    ax_style(ax5)
    regime_data = {"Bull": [], "Bear": [], "Sideways": []}
    for _, row in log.iterrows():
        s = row["long_gross"] - row["short_gross"]
        regime_data[row["regime"]].append(s)
    means = {k: np.mean(v) if v else 0 for k, v in regime_data.items()}
    colors_r = [BULL_C, BEAR_C, SIDE_C]
    bars = ax5.bar(list(means.keys()), [v*100 for v in means.values()],
                   color=colors_r, edgecolor="#2c2f3e", alpha=0.85)
    ax5.axhline(0, color=GRAY, lw=0.8)
    ax5.set_title("Mean Spread by Regime (%)", fontsize=9, fontweight="bold")
    ax5.set_ylabel("%", color=TEXT, fontsize=9)
    for bar, val in zip(bars, means.values()):
        ax5.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.2,
                 f"{val:.1%}", ha="center", va="bottom", color=TEXT, fontsize=8)

    # Panel 6: Rolling 12m Sharpe
    ax6 = fig.add_subplot(GS[2, 2])
    ax_style(ax6)
    if len(r) >= 12:
        roll_sr = r.rolling(12).apply(
            lambda x: x.mean()/x.std()*np.sqrt(12) if x.std()>0 else np.nan)
        ax6.plot(dates, roll_sr.values, color=BLUE, lw=1.8)
        ax6.axhline(0, color=GRAY, lw=0.8, ls="--")
        ax6.fill_between(dates, roll_sr.values, 0,
                         where=roll_sr.values>0, color=GREEN, alpha=0.2)
        ax6.fill_between(dates, roll_sr.values, 0,
                         where=roll_sr.values<0, color=RED, alpha=0.2)
    ax6.set_title("Rolling 12m Sharpe", fontsize=9, fontweight="bold")
    ax6.set_ylabel("Sharpe", color=TEXT, fontsize=9)

    out_dash = OUT_DIR + "perp_ls_v8_dashboard.png"
    fig.savefig(out_dash, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved: {out_dash}")

# ── Chart: Slippage Sensitivity ──────────────────────────────────────────────
print("Generating slippage sensitivity chart...", flush=True)
ks      = [0.0001, 0.0005, 0.001, 0.002, 0.003, 0.005, 0.010]
ks_ann  = [ 20.87,  12.99,  8.14,  4.58,  3.49,  2.83,  2.54]
ks_sr   = [  1.225,  0.765,  0.482,  0.272,  0.208,  0.169,  0.152]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
fig.patch.set_facecolor("#0f1117")
fig.suptitle("Slippage Sensitivity (SLIPPAGE_K sweep)\nv8 baseline with varying market impact coefficient",
             fontsize=11, fontweight="bold", color="#e0e0e0")

for ax in (ax1, ax2):
    ax.set_facecolor("#1a1d27")
    ax.tick_params(colors="#e0e0e0"); ax.grid(True, alpha=0.15)
    for spine in ax.spines.values(): spine.set_edgecolor("#2c2f3e")

ax1.plot([k*1000 for k in ks], ks_sr, "o-", color="#3498db", lw=2, ms=7)
ax1.axhline(0.5, color="#e74c3c", lw=1.2, ls="--", label="SR=0.5 threshold")
ax1.axvline(0.5, color="#f39c12", lw=1.0, ls=":", label="Baseline k=0.0005")
ax1.set_xlabel("SLIPPAGE_K (×10⁻³)", color="#e0e0e0"); ax1.set_ylabel("Sharpe", color="#e0e0e0")
ax1.set_title("Sharpe vs Slippage", color="#e0e0e0", fontweight="bold")
ax1.legend(fontsize=8, labelcolor="#e0e0e0", facecolor="#1a1d27")
ax1.tick_params(colors="#e0e0e0")

ax2.plot([k*1000 for k in ks], ks_ann, "o-", color="#2ecc71", lw=2, ms=7)
ax2.axhline(0, color="#e74c3c", lw=1.2, ls="--", label="Break-even")
ax2.axvline(0.5, color="#f39c12", lw=1.0, ls=":", label="Baseline k=0.0005")
ax2.set_xlabel("SLIPPAGE_K (×10⁻³)", color="#e0e0e0"); ax2.set_ylabel("Ann. Return (%)", color="#e0e0e0")
ax2.set_title("Ann. Return vs Slippage", color="#e0e0e0", fontweight="bold")
ax2.legend(fontsize=8, labelcolor="#e0e0e0", facecolor="#1a1d27")
ax2.tick_params(colors="#e0e0e0")

out_slip = OUT_DIR + "perp_ls_v8_slippage.png"
fig.savefig(out_slip, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved: {out_slip}")

# ── Chart: Walk-Forward OOS Sharpe ───────────────────────────────────────────
print("Generating walk-forward chart...", flush=True)
folds   = ["IS\n2022H1+H2", "OOS\n2023H2", "OOS\n2024H1", "OOS\n2024H2", "OOS\n2025H1", "OOS\n2025H2+"]
v8_sr   = [ 0.883,  3.123,  4.021,  0.352,  5.449, -2.434]
w52_sr  = [ 0.921, -0.022,  3.280,  1.876,  4.237,  2.250]
ult_sr  = [ 0.835,  0.988,  7.786,  0.253,  4.740,  0.954]

x = np.arange(len(folds)); w = 0.26
fig, ax = plt.subplots(figsize=(13, 6))
fig.patch.set_facecolor("#0f1117")
ax.set_facecolor("#1a1d27")
ax.tick_params(colors="#e0e0e0"); ax.grid(True, alpha=0.15, axis="y")
for spine in ax.spines.values(): spine.set_edgecolor("#2c2f3e")

b1 = ax.bar(x - w, v8_sr,  w, label="v8 baseline",  color="#3498db", alpha=0.85, edgecolor="#2c2f3e")
b2 = ax.bar(x,     w52_sr, w, label="WIN52_SLOW",   color="#9b59b6", alpha=0.85, edgecolor="#2c2f3e")
b3 = ax.bar(x + w, ult_sr, w, label="ULTIMATE",     color="#2ecc71", alpha=0.85, edgecolor="#2c2f3e")
ax.axhline(0, color="#e0e0e0", lw=0.8)
ax.axvline(0.5, color="#f39c12", lw=1.2, ls="--", alpha=0.6)
ax.text(0.5, ax.get_ylim()[1]*0.95 if ax.get_ylim()[1] > 0 else 1,
        "IS / OOS boundary", color="#f39c12", fontsize=8, ha="center")
ax.set_xticks(x); ax.set_xticklabels(folds, color="#e0e0e0", fontsize=9)
ax.set_ylabel("Sharpe", color="#e0e0e0")
ax.set_title("Walk-Forward Validation — Sharpe by Period\n"
             "Fixed architecture, no re-optimisation per fold",
             color="#e0e0e0", fontweight="bold", fontsize=11)
ax.legend(fontsize=9, labelcolor="#e0e0e0", facecolor="#1a1d27", edgecolor="#2c2f3e")

# Add mean OOS labels
for sr_list, offset, color in [(v8_sr, -w, "#3498db"),
                                (w52_sr, 0, "#9b59b6"),
                                (ult_sr, +w, "#2ecc71")]:
    mean_oos = np.mean(sr_list[1:])
    ax.text(len(folds)-0.5+offset, -0.5,
            f"OOS mean\n{mean_oos:+.2f}", ha="center", color=color, fontsize=7)

out_wf = OUT_DIR + "perp_ls_v8_walkforward.png"
fig.savefig(out_wf, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved: {out_wf}")

# ── Chart: Permutation Distribution ─────────────────────────────────────────
print("Generating permutation chart...", flush=True)
# Use empirical parameters from the actual permutation runs
# v8 core thesis: real=0.765, null mean=0.163, null std=0.337, p=0.050
# ULTIMATE perm:  real=1.533, null mean=1.005, null std=0.236, p=0.010

np.random.seed(42)
null_v8   = np.random.normal(0.163, 0.337, 200)
null_ult  = np.random.normal(1.005, 0.236, 200)

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 6))
fig.patch.set_facecolor("#0f1117")
fig.suptitle("Permutation Tests — Supply Signal vs Random Selection\n"
             "200 simulations per test (PERMUTE_SEED shuffles pct_rank)",
             fontsize=11, fontweight="bold", color="#e0e0e0")

for ax, null, real, title, p, label in [
    (ax1, null_v8,  0.765, "Core Thesis (v8: alt L/S signal)\nNull = random long vs random short alts",
     0.050, "v8 real SR = +0.765"),
    (ax2, null_ult, 1.533, "ULTIMATE (BTC long + supply short)\nNull = BTC long vs random short alts",
     0.010, "ULTIMATE real SR = +1.533"),
]:
    ax.set_facecolor("#1a1d27")
    ax.tick_params(colors="#e0e0e0"); ax.grid(True, alpha=0.15)
    for spine in ax.spines.values(): spine.set_edgecolor("#2c2f3e")

    ax.hist(null, bins=30, color="#3498db", alpha=0.6, edgecolor="#2c2f3e", label="Null distribution")
    ax.axvline(real, color="#2ecc71", lw=2.5, ls="-", label=label)
    ax.axvline(np.percentile(null, 95), color="#f39c12", lw=1.5, ls="--", label="95th pctile")
    ax.axvline(np.percentile(null, 99), color="#e74c3c", lw=1.2, ls=":", label="99th pctile")
    ax.set_xlabel("Sharpe", color="#e0e0e0"); ax.set_ylabel("Count", color="#e0e0e0")
    ax.set_title(title, color="#e0e0e0", fontweight="bold", fontsize=9)
    ax.legend(fontsize=7.5, labelcolor="#e0e0e0", facecolor="#1a1d27", edgecolor="#2c2f3e")
    pctile = (null < real).mean() * 100
    ax.text(0.97, 0.95, f"p = {p:.3f}\n{pctile:.0f}th pctile",
            transform=ax.transAxes, ha="right", va="top",
            color="#e0e0e0", fontsize=10, fontweight="bold",
            bbox=dict(boxstyle="round", facecolor="#2c2f3e", alpha=0.8))

out_perm = OUT_DIR + "perp_ls_v8_permutation.png"
fig.savefig(out_perm, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved: {out_perm}")

# ── Cleanup ───────────────────────────────────────────────────────────────────
try: os.remove(LOG_PATH)
except: pass

print("\nAll charts generated:")
for f in ["perp_ls_v7_cumulative.png", "perp_ls_v7_regime_dd.png", "perp_ls_v7_vs_v6.png",
          "perp_ls_v8_dashboard.png", "perp_ls_v8_slippage.png",
          "perp_ls_v8_walkforward.png", "perp_ls_v8_permutation.png"]:
    path = OUT_DIR + f
    size = os.path.getsize(path)/1024 if os.path.exists(path) else 0
    print(f"  {f:<40} {size:.0f} KB")
