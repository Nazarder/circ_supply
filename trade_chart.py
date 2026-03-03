"""
trade_chart.py
==============
Visualise individual trade entries and exits from the v7_full basket log.

Four panels:
  A. Gantt-style timeline — each token shown as a coloured bar from entry to exit
     (long = teal, short = coral). Regime shading in background.
  B. Period-by-period opens and closes (bar chart), long and short legs.
  C. Open position count over time (area chart).
  D. Cumulative trade count (long opens + short opens) over time.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
import warnings
warnings.filterwarnings("ignore")

BASKET_LOG  = "D:/AI_Projects/circ_supply/v7_full_basket_log.csv"
OUTPUT_FILE = "D:/AI_Projects/circ_supply/perp_ls_v7_trade_chart.png"

REGIME_COLORS = {"Bull": "#fef9e7", "Bear": "#eaf4fb", "Sideways": "#f9f9f9"}
LONG_COLOR    = "#1a7f5e"   # teal-green
SHORT_COLOR   = "#c0392b"   # red
OPEN_COLOR    = "#2ecc71"   # bright green for opens
CLOSE_COLOR   = "#e74c3c"   # bright red for closes

# ---------------------------------------------------------------------------
# Load basket log
# ---------------------------------------------------------------------------
bl = pd.read_csv(BASKET_LOG, parse_dates=["date"])
bl = bl.sort_values("date").reset_index(drop=True)

def split(cell):
    if pd.isna(cell) or str(cell).strip() == "":
        return []
    return [t.strip() for t in str(cell).split(",") if t.strip()]

bl["long_list"]   = bl["long_tokens"].apply(split)
bl["short_list"]  = bl["short_tokens"].apply(split)
bl["lo_list"]     = bl["long_opens"].apply(split)
bl["lc_list"]     = bl["long_closes"].apply(split)
bl["so_list"]     = bl["short_opens"].apply(split)
bl["sc_list"]     = bl["short_closes"].apply(split)

dates     = bl["date"].tolist()
n_periods = len(dates)

# Next-period date for bar widths (last bar uses a 31-day window)
next_dates = dates[1:] + [dates[-1] + pd.Timedelta(days=31)]

# ---------------------------------------------------------------------------
# Build per-token position spans (for Gantt panel)
# ---------------------------------------------------------------------------
# Track which tokens are currently long / short and when they started
long_spans  = []   # (symbol, start_date, end_date)
short_spans = []

current_long  = {}   # symbol -> entry_date
current_short = {}

for idx, row in bl.iterrows():
    t0 = row["date"]
    t1 = next_dates[idx]

    # --- long leg ---
    new_long = set(row["long_list"])
    for sym in list(current_long.keys()):
        if sym not in new_long:
            long_spans.append((sym, current_long.pop(sym), t0))
    for sym in new_long:
        if sym not in current_long:
            current_long[sym] = t0

    # --- short leg ---
    new_short = set(row["short_list"])
    for sym in list(current_short.keys()):
        if sym not in new_short:
            short_spans.append((sym, current_short.pop(sym), t0))
    for sym in new_short:
        if sym not in current_short:
            current_short[sym] = t0

# Close any still-open at the end
last_date = next_dates[-1]
for sym, start in current_long.items():
    long_spans.append((sym, start, last_date))
for sym, start in current_short.items():
    short_spans.append((sym, start, last_date))

# Assign each symbol a y-position (sorted by first appearance)
all_syms_long  = sorted(set(s for s, _, _ in long_spans),
                         key=lambda s: min(st for sy, st, _ in long_spans if sy == s))
all_syms_short = sorted(set(s for s, _, _ in short_spans),
                          key=lambda s: min(st for sy, st, _ in short_spans if sy == s))
long_y  = {s: i for i, s in enumerate(all_syms_long)}
short_y = {s: i for i, s in enumerate(all_syms_short)}

# ---------------------------------------------------------------------------
# Period-level trade counts
# ---------------------------------------------------------------------------
lo_counts = [len(row["lo_list"]) for _, row in bl.iterrows()]
lc_counts = [len(row["lc_list"]) for _, row in bl.iterrows()]
so_counts = [len(row["so_list"]) for _, row in bl.iterrows()]
sc_counts = [len(row["sc_list"]) for _, row in bl.iterrows()]

long_open_count  = [len(row["long_list"])  for _, row in bl.iterrows()]
short_open_count = [len(row["short_list"]) for _, row in bl.iterrows()]

cum_long_opens  = np.cumsum(lo_counts)
cum_short_opens = np.cumsum(so_counts)
cum_total       = cum_long_opens + cum_short_opens

# ---------------------------------------------------------------------------
# Figure layout
# ---------------------------------------------------------------------------
fig = plt.figure(figsize=(20, 22))
fig.patch.set_facecolor("white")

gs = fig.add_gridspec(4, 2, height_ratios=[4, 4, 2, 2],
                      hspace=0.45, wspace=0.25,
                      left=0.08, right=0.97, top=0.94, bottom=0.04)

ax_gantt_l  = fig.add_subplot(gs[0, 0])   # Gantt long
ax_gantt_s  = fig.add_subplot(gs[0, 1])   # Gantt short
ax_bar_l    = fig.add_subplot(gs[1, 0])   # opens/closes long
ax_bar_s    = fig.add_subplot(gs[1, 1])   # opens/closes short
ax_count    = fig.add_subplot(gs[2, :])   # open position count
ax_cum      = fig.add_subplot(gs[3, :])   # cumulative trades

fig.suptitle("Perpetual L/S v7 — Trade Entry / Exit Analysis (Full History)",
             fontsize=15, fontweight="bold", y=0.97)

# ---------------------------------------------------------------------------
# Helper: shade regime background
# ---------------------------------------------------------------------------
def shade_regime(ax, bl):
    prev_regime = None
    t_start     = None
    for _, row in bl.iterrows():
        if row["regime"] != prev_regime:
            if t_start is not None:
                ax.axvspan(t_start, row["date"],
                           color=REGIME_COLORS.get(prev_regime, "#ffffff"),
                           alpha=0.7, zorder=0)
            t_start     = row["date"]
            prev_regime = row["regime"]
    if t_start is not None:
        ax.axvspan(t_start, next_dates[-1],
                   color=REGIME_COLORS.get(prev_regime, "#ffffff"),
                   alpha=0.7, zorder=0)

# ---------------------------------------------------------------------------
# Panel A1 — Gantt: Long positions
# ---------------------------------------------------------------------------
shade_regime(ax_gantt_l, bl)
for sym, start, end in long_spans:
    y = long_y[sym]
    ax_gantt_l.barh(y, (end - start).days, left=start,
                    height=0.7, color=LONG_COLOR, alpha=0.75, linewidth=0)

ax_gantt_l.set_yticks(range(len(all_syms_long)))
ax_gantt_l.set_yticklabels(all_syms_long, fontsize=6.5)
ax_gantt_l.set_xlim(dates[0] - pd.Timedelta(days=15), next_dates[-1])
ax_gantt_l.set_title("Long basket — position timeline", fontsize=11, fontweight="bold")
ax_gantt_l.set_xlabel("")
ax_gantt_l.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_gantt_l.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_gantt_l.grid(axis="x", linestyle="--", alpha=0.4)
ax_gantt_l.set_facecolor("#fdfdfd")

# ---------------------------------------------------------------------------
# Panel A2 — Gantt: Short positions
# ---------------------------------------------------------------------------
shade_regime(ax_gantt_s, bl)
for sym, start, end in short_spans:
    y = short_y[sym]
    ax_gantt_s.barh(y, (end - start).days, left=start,
                    height=0.7, color=SHORT_COLOR, alpha=0.75, linewidth=0)

ax_gantt_s.set_yticks(range(len(all_syms_short)))
ax_gantt_s.set_yticklabels(all_syms_short, fontsize=6.5)
ax_gantt_s.set_xlim(dates[0] - pd.Timedelta(days=15), next_dates[-1])
ax_gantt_s.set_title("Short basket — position timeline", fontsize=11, fontweight="bold")
ax_gantt_s.set_xlabel("")
ax_gantt_s.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_gantt_s.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_gantt_s.grid(axis="x", linestyle="--", alpha=0.4)
ax_gantt_s.set_facecolor("#fdfdfd")

# ---------------------------------------------------------------------------
# Panel B1 — Opens / Closes: Long leg
# ---------------------------------------------------------------------------
shade_regime(ax_bar_l, bl)
x = [d.to_pydatetime() for d in dates]
width_days = 20
bar_w = [pd.Timedelta(days=width_days)] * n_periods

ax_bar_l.bar(x, lo_counts,  width=width_days, align="center",
             color=OPEN_COLOR,  alpha=0.8, label="Opens",  zorder=2)
ax_bar_l.bar(x, [-v for v in lc_counts], width=width_days, align="center",
             color=CLOSE_COLOR, alpha=0.8, label="Closes", zorder=2)
ax_bar_l.axhline(0, color="black", linewidth=0.8)
ax_bar_l.set_title("Long leg — opens (+) and closes (−) per period",
                   fontsize=10, fontweight="bold")
ax_bar_l.set_ylabel("Token count")
ax_bar_l.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_bar_l.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_bar_l.legend(fontsize=8)
ax_bar_l.grid(axis="y", linestyle="--", alpha=0.4)
ax_bar_l.set_xlim(dates[0] - pd.Timedelta(days=30), next_dates[-1])

# ---------------------------------------------------------------------------
# Panel B2 — Opens / Closes: Short leg
# ---------------------------------------------------------------------------
shade_regime(ax_bar_s, bl)
ax_bar_s.bar(x, so_counts,  width=width_days, align="center",
             color=OPEN_COLOR,  alpha=0.8, label="Opens",  zorder=2)
ax_bar_s.bar(x, [-v for v in sc_counts], width=width_days, align="center",
             color=CLOSE_COLOR, alpha=0.8, label="Closes", zorder=2)
ax_bar_s.axhline(0, color="black", linewidth=0.8)
ax_bar_s.set_title("Short leg — opens (+) and closes (−) per period",
                   fontsize=10, fontweight="bold")
ax_bar_s.set_ylabel("Token count")
ax_bar_s.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_bar_s.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_bar_s.legend(fontsize=8)
ax_bar_s.grid(axis="y", linestyle="--", alpha=0.4)
ax_bar_s.set_xlim(dates[0] - pd.Timedelta(days=30), next_dates[-1])

# ---------------------------------------------------------------------------
# Panel C — Open position count
# ---------------------------------------------------------------------------
shade_regime(ax_count, bl)
ax_count.fill_between(x, long_open_count, alpha=0.5, color=LONG_COLOR, label="Long basket size")
ax_count.fill_between(x, short_open_count, alpha=0.5, color=SHORT_COLOR, label="Short basket size")
ax_count.plot(x, long_open_count,  color=LONG_COLOR,  linewidth=1.5)
ax_count.plot(x, short_open_count, color=SHORT_COLOR, linewidth=1.5)
ax_count.axhline(6, color="gray", linestyle="--", linewidth=0.8, label="Min basket size (6)")
ax_count.set_title("Open positions per period (basket size)",
                   fontsize=10, fontweight="bold")
ax_count.set_ylabel("Token count")
ax_count.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_count.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_count.legend(fontsize=8, loc="upper left")
ax_count.grid(axis="y", linestyle="--", alpha=0.4)
ax_count.set_xlim(dates[0] - pd.Timedelta(days=30), next_dates[-1])

# Annotate total trades
total_long  = int(sum(lo_counts)) + int(sum(lc_counts))
total_short = int(sum(so_counts)) + int(sum(sc_counts))
ax_count.text(0.99, 0.93,
              f"Total trades: {total_long + total_short:,}  "
              f"(Long {total_long:,} | Short {total_short:,})",
              transform=ax_count.transAxes, ha="right", va="top",
              fontsize=9, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8))

# ---------------------------------------------------------------------------
# Panel D — Cumulative trade count
# ---------------------------------------------------------------------------
shade_regime(ax_cum, bl)
ax_cum.plot(x, cum_long_opens,  color=LONG_COLOR,  linewidth=2,
            label=f"Long opens  (cumulative, total={int(cum_long_opens[-1])})")
ax_cum.plot(x, cum_short_opens, color=SHORT_COLOR, linewidth=2,
            label=f"Short opens (cumulative, total={int(cum_short_opens[-1])})")
ax_cum.plot(x, cum_total,       color="#7f8c8d",   linewidth=2, linestyle="--",
            label=f"Total opens (cumulative, total={int(cum_total[-1])})")
ax_cum.fill_between(x, cum_total, alpha=0.12, color="#7f8c8d")
ax_cum.set_title("Cumulative position opens over time",
                 fontsize=10, fontweight="bold")
ax_cum.set_ylabel("Cumulative opens")
ax_cum.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m"))
ax_cum.tick_params(axis="x", labelrotation=30, labelsize=8)
ax_cum.legend(fontsize=8, loc="upper left")
ax_cum.grid(axis="y", linestyle="--", alpha=0.4)
ax_cum.set_xlim(dates[0] - pd.Timedelta(days=30), next_dates[-1])

# ---------------------------------------------------------------------------
# Legend patches for regime shading
# ---------------------------------------------------------------------------
regime_patches = [
    mpatches.Patch(color=REGIME_COLORS["Bull"],     alpha=0.8, label="Bull regime"),
    mpatches.Patch(color=REGIME_COLORS["Bear"],     alpha=0.8, label="Bear regime"),
    mpatches.Patch(color=REGIME_COLORS["Sideways"], alpha=0.8, label="Sideways (cash)"),
]
fig.legend(handles=regime_patches, loc="lower center", ncol=3,
           fontsize=9, frameon=True, bbox_to_anchor=(0.5, 0.01))

plt.savefig(OUTPUT_FILE, dpi=150, bbox_inches="tight", facecolor="white")
print(f"[Chart] Saved: {OUTPUT_FILE}")

# ---------------------------------------------------------------------------
# Print trade count summary
# ---------------------------------------------------------------------------
print()
print("=" * 60)
print("TRADE COUNT SUMMARY (v7 full history, 51 periods)")
print("=" * 60)
print(f"  Long  opens  : {int(sum(lo_counts)):>5}  avg {sum(lo_counts)/n_periods:.1f}/period")
print(f"  Long  closes : {int(sum(lc_counts)):>5}  avg {sum(lc_counts)/n_periods:.1f}/period")
print(f"  Long  total  : {int(sum(lo_counts))+int(sum(lc_counts)):>5}")
print()
print(f"  Short opens  : {int(sum(so_counts)):>5}  avg {sum(so_counts)/n_periods:.1f}/period")
print(f"  Short closes : {int(sum(sc_counts)):>5}  avg {sum(sc_counts)/n_periods:.1f}/period")
print(f"  Short total  : {int(sum(so_counts))+int(sum(sc_counts)):>5}")
print()
grand = int(sum(lo_counts))+int(sum(lc_counts))+int(sum(so_counts))+int(sum(sc_counts))
print(f"  GRAND TOTAL  : {grand:>5}  avg {grand/n_periods:.1f}/period")
print()
print("  Longest-held positions:")
all_spans = [(sym, "LONG",  (e-s).days) for sym, s, e in long_spans] + \
            [(sym, "SHORT", (e-s).days) for sym, s, e in short_spans]
all_spans.sort(key=lambda x: -x[2])
for sym, side, days in all_spans[:15]:
    mo = days / 30.44
    print(f"    {sym:<10} {side:<6}  {mo:.1f} months  ({days} days)")
print("=" * 60)
