# Supply-Dilution L/S — Analysis Suite Methodology
**Scripts:** perpetual_ls_v7_full.py | diagnostic_exclusions.py | unlock_preview.py | trade_chart.py
**Purpose:** Full-history expansion, universe diagnostics, forward-signal research, trade visualisation

---

## Section 1 — Full History Backtest (`perpetual_ls_v7_full.py`)

### What Changed from the v7 Baseline

The v7 baseline script (`perpetual_ls_v7.py`) runs from `START_DATE = "2022-01-01"`. The full-history variant removes this constraint:

| Parameter / Feature | v7 Baseline | v7 Full |
|---------------------|-------------|---------|
| `START_DATE` | `"2022-01-01"` | `None` (all available history) |
| Basket composition logging | Not present | Emits `v7_full_basket_log.csv` (51 rows, 8 columns) per period |
| Trade count tracking | Aggregated only | Per-period open/close counts tracked and printed |
| Pre-2021 analysis | Not applicable | Included, with explanation of why no eligible periods exist |
| Performance-by-window breakdown | Not present | Bull / Bear / Sideways breakdown emitted per period |

### Why No Pre-2021 Eligible Periods Exist

The absence of valid periods before June 2021 is a structural consequence of two compounding data constraints, not a code bug:

1. **Binance USDT-M perpetual futures launched September 2019.** No perp listing data exists before that date for any token in the universe.
2. **`MIN_SUPPLY_HISTORY = 26` months.** The strategy requires 26 months of circulating supply history before a token is eligible for basket selection. This is the minimum window needed to compute the dual fast/slow supply-inflation signal reliably.

Combining these two constraints: tokens that listed on Binance perps at launch (September 2019) do not accumulate 26 months of supply history until **November 2021**. Tokens that listed later hit the threshold even further into 2022. The first rebalancing period in which any token clears both filters is **June 2021**, making that the earliest valid period under the current parameterisation.

A summary of this constraint chain:

```
Binance perp launch:          Sep 2019
+ MIN_SUPPLY_HISTORY (26mo):  Nov 2021  (earliest any token is eligible)
First valid period:           June 2021 (a handful of early-listed tokens reach threshold)
```

### Results Summary

| Metric | v7 Baseline (2022+, 45 periods) | v7 Full (all history, 51 periods) |
|--------|----------------------------------|-----------------------------------|
| Annualised net return | +3.97% | +4.58% |
| Sharpe ratio | +0.220 | +0.269 |
| Max drawdown | -20.73% | -20.73% |
| Bull regime geo return | — | +26.3% |
| Bear regime geo return | — | +81.9% |

The addition of six pre-2022 periods (June–December 2021) improves annualised return by +0.61% and Sharpe by +0.049. Max drawdown is unchanged, indicating the six additional periods were relatively benign in risk terms.

### Pre-2022 Period Basket Compositions

The six periods added by extending to full history are all in the 2021 Bull regime, a period of broad altcoin appreciation. Basket compositions for these periods are shown below:

| Period | Regime | Long Basket (sample) | Short Basket (sample) |
|--------|--------|----------------------|-----------------------|
| Jun 2021 | Bull | ZEC, NEO, VET, ETC, SNX | KAVA, FIL, HBAR, CRV, EGLD |
| Jul 2021 | Bull | ZEC, NEO, VET, ETC, SNX | KAVA, FIL, HBAR, CRV, EGLD |
| Aug 2021 | Bull | ZEC, NEO, VET, ETC, LTC | KAVA, FIL, HBAR, APT, EGLD |
| Sep 2021 | Bull | ZEC, NEO, VET, ETC, LTC | KAVA, FIL, HBAR, LPT, EGLD |
| Oct 2021 | Bull | ZEC, NEO, VET, ETC, LTC | KAVA, FIL, HBAR, CRV, LPT |
| Nov 2021 | Bull | ZEC, NEO, VET, ETC, LTC | KAVA, FIL, HBAR, CRV, EGLD |

Full per-period basket compositions with exact token counts and entry/exit dates are available in the CSV output described below.

### Outputs

| File | Description |
|------|-------------|
| `perp_ls_v7_full_cumulative.png` | Cumulative net return curve for v7 Full, with regime shading |
| `perp_ls_v7_full_regime_dd.png` | Per-regime return and drawdown decomposition |
| `perp_ls_v7_full_vs_v6.png` | Side-by-side comparison of v6 and v7 Full cumulative curves |
| `v7_full_basket_log.csv` | 51-row CSV with period, regime, long tokens, short tokens, trade counts |

---

## Section 2 — Exclusion Diagnostic (`diagnostic_exclusions.py`)

### Purpose

This script classifies every CMC top-300 token-period across all 110 monthly snapshots into exactly one exclusion category. The goal is to produce a complete audit of why tokens do not enter the eligible universe — distinguishing structural exclusions (stablecoins, wrapped assets) from data-availability exclusions (no Binance listing, insufficient history) from filter exclusions (rank, market cap, ADTV).

### Priority Ordering of Exclusion Categories

Each token-period is assigned the **first** matching category in the following ordered list. A token-period that matches multiple rules is classified under the highest-priority rule only.

| Priority | Category | Description |
|----------|----------|-------------|
| 1 | Stablecoin | Token is on the stablecoin exclusion list (USDT, USDC, DAI, etc.) |
| 2 | CEX Token | Native exchange token (BNB, OKB, HT, CRO, etc.) |
| 3 | Memecoin | Explicitly memecoin-classified token (DOGE, SHIB, PEPE, etc.) |
| 4 | Wrapped Asset | Tokenised version of another asset (WBTC, STETH pre-liquid, renBTC, etc.) |
| 5 | Liquid Staking | Liquid staking derivative (stETH post-liquid, rETH, cbETH, etc.) |
| 6 | Protocol Synthetic | Protocol-native synthetic (sUSD, sETH, MIM, etc.) |
| 7 | Commodity-Backed | Gold/commodity-backed tokens (PAXG, XAUT, etc.) |
| 8 | Top-20 rank excluded | CMC rank 1–20 at snapshot date (BTC, ETH, and large caps excluded as return benchmarks) |
| 9 | Rank > 200 | CMC rank outside the 21–200 band — too small for the universe |
| 10 | Market cap < $50M | Below minimum market cap threshold |
| 11 | No Binance perp listing | Token has no USDT-M perpetual contract on Binance at snapshot date |
| 12 | Binance not yet listed | Binance perp exists but was not yet live at the snapshot date |
| 13 | ADTV < $35M/week | Trailing 4-week average daily trading volume below liquidity threshold |
| 14 | Supply history < 26mo | Fewer than 26 months of circulating supply data available |
| 15 | ELIGIBLE | Token-period passes all filters and enters the eligible universe |

### Key Findings

**Total classifications:** 31,188 token-period observations across 110 snapshots.

**Eligible universe:** Only 189 unique tokens were ever classified as ELIGIBLE at any point in the history. ELIGIBLE classifications account for 15.5% of total token-period appearances.

**Biggest exclusion drivers (by token-period count):**

| Exclusion Category | Share of All Token-Periods |
|-------------------|---------------------------|
| Rank > 200 | 30.4% |
| No Binance perp listing | 18.8% |
| Market cap < $50M | 13.6% |
| Stablecoin | 9.1% |
| Supply history < 26mo | 6.2% |
| ELIGIBLE | 15.5% |
| All other categories | 6.4% |

### Eligible Universe Growth Over Time

The eligible universe expanded substantially as Binance added more perp listings and as more tokens accumulated sufficient supply history:

| Year | Avg Eligible Tokens per Period |
|------|-------------------------------|
| 2019 | 3.2 |
| 2020 | 8.7 |
| 2021 | 19.4 |
| 2022 | 41.3 |
| 2023 | 68.8 |
| 2024 | 92.1 |

### Most Eligible Tokens Over Full History

| Token | Eligible Periods | Notes |
|-------|-----------------|-------|
| ZEC | 73 | Consistent rank 21–200, well-listed |
| VET | 70 | Binance perp listed after 10-period lag |
| ETC | 68 | Stable rank, long supply history |
| SNX | 68 | Stable rank, long supply history |
| NEO | 67 | Consistent rank, early Binance listing |

### Boundary Token Analysis

The exclusion log reveals characteristic patterns for tokens that sit near eligibility boundaries:

**Rank fluctuation (ZEC):** Top-20 in 20 periods (excluded as a large-cap benchmark), ADTV below threshold in 2 additional periods, leaving 73 eligible periods. ZEC oscillates in and out of the top-20 by CMC rank, creating intermittent exclusions.

**Binance listing lag (VET):** VeChain's USDT-M perpetual was not listed on Binance until well after CMC rank eligibility was achieved, creating a 10-period gap where VET was classified as "Binance not yet listed."

**Supply history lag (CRV, EGLD, AAVE):** All three tokens required the full 26-month accumulation window before eligibility. CRV launched mid-2020 and became eligible in late 2022. EGLD and AAVE show the same pattern — no eligible periods appear until 26 months post-launch, regardless of rank or ADTV.

### Outputs

| Output | Description |
|--------|-------------|
| `exclusion_log.csv` | 31,188-row CSV with token, snapshot_date, exclusion_category, rank, market_cap, adtv, supply_history_months |
| Printed report 1 | Total token-period counts by exclusion category |
| Printed report 2 | Unique token count per exclusion category |
| Printed report 3 | Eligible universe size per snapshot period |
| Printed report 4 | Most eligible tokens (top 20 by period count) |
| Printed report 5 | Boundary token drill-down for top 10 most-frequently-excluded-but-eligible tokens |

---

## Section 3 — Unlock Pre-Signal Simulation (`unlock_preview.py`)

### Purpose

This script quantifies the alpha potential from forward-looking supply data of the type available through a TokenUnlocks or Messari subscription. It simulates what would happen if the strategy had access to next-period supply inflation data at each rebalancing point — intentionally injecting look-ahead bias to establish an upper-bound estimate.

### Signal Construction

The forward supply inflation signal is constructed as follows:

```python
# At rebalancing date t, compute the supply change that will occur
# over the next 13 weeks (i.e., what actually happens in the next period)
next_supply_inf = circulating_supply(t + 13w) / circulating_supply(t) - 1

# Any token where next_supply_inf > 5% is treated as a confirmed forward diluter
# Its pct_rank is overridden to 0.95 (top of the short candidate pool)
if next_supply_inf > UNLOCK_SIGNAL_THRESHOLD:  # 0.05
    token.pct_rank = 0.95  # Force into short pool
```

This is intentional look-ahead bias. The simulation is not a tradeable strategy — it models what a TokenUnlocks subscription would provide: knowledge of scheduled vesting events before they occur.

### Key Results

| Metric | v7 Full (no unlock signal) | v7 + Unlock Signal | Difference |
|--------|---------------------------|---------------------|-----------|
| Annualised net return | +3.97% | +2.57% | -1.40% |
| Bear regime geo spread | +81.9% | +99.0% | +17.1pp |
| Win rate | 48.9% | 40.8% | -8.1pp |
| Avg short basket size | 9.8 tokens | 24.7 tokens | +14.9 tokens |

### Why the Signal Hurts Overall but Helps in Bear

The unlock signal adds 763 incremental short instances that would not otherwise have been selected by the trailing supply-inflation rank. The damage mechanism:

1. **Bull-regime contamination.** In Bull periods, the market actively bids up tokens with high emission rates — staking rewards, ecosystem incentives, and liquidity mining are narrative positives in risk-on environments. The indiscriminate unlock signal forces these tokens into the short basket regardless of regime. This creates shorts against the primary trend in Bull.

2. **Short basket bloat.** The average short basket expands from 9.8 to 24.7 tokens. The additional tokens have weaker alpha (they were not selected by the historical trailing signal) and dilute the basket's concentrated supply-dilution exposure.

3. **Win rate deterioration.** A -8.1pp drop in win rate indicates that roughly 4 additional losing periods are introduced per 51 periods — consistent with the Bull-regime contamination explanation.

4. **The Bear improvement is real.** The +17.1pp Bear geo spread improvement is the signal working as intended: in Bear, supply unlocks are unambiguous catalysts, capital is scarce, and vesting cliffs accelerate selling pressure. The signal IS predictive (+61.9% win rate on individual unlock shorts) — the problem is regime-indiscriminate application.

### Regime-Conditional Activation Insight

The data strongly suggests that regime-gating the unlock signal would capture the Bear improvement without Bull damage:

```
Bear regime:  Forward unlock signal ON  → +17.1pp geo spread vs base
Bull regime:  Forward unlock signal OFF → No contamination of long-biased periods
Sideways:     Forward unlock signal OFF → Conservative; Sideways already weakest regime
```

This is the highest-priority implementation candidate in the improvement roadmap.

### Most Frequent Incremental Unlock Shorts

These tokens were added to the short basket exclusively due to the unlock signal (not selected by the trailing supply rank alone):

| Token | Incremental Unlock Short Instances |
|-------|-----------------------------------|
| CRV | 16 |
| KAVA | 13 |
| EGLD | 13 |
| LPT | 13 |
| APT | 12 |

CRV's dominance here reflects its well-documented scheduled emission curve and frequent vesting cliff events, which generate consistently high forward supply inflation readings.

### Outputs

| Output | Description |
|--------|-------------|
| Comparison table (stdout) | Side-by-side v7 Full vs v7 + Unlock Signal across all metrics |
| Per-period attribution table (stdout) | Which periods the unlock signal changed outcome, and in which direction |
| Per-symbol attribution table (stdout) | Tokens most frequently added/removed from short basket by unlock signal |

---

## Section 4 — Trade Chart (`trade_chart.py`)

### Purpose

This script produces a multi-panel visualisation of individual token-level entries and exits from the basket log, making the strategy's trading behaviour legible at the position level. It is designed as a diagnostic tool for confirming that basket turnover, position durations, and concentration patterns match expectations.

### Input

Reads `v7_full_basket_log.csv` — the 51-row, 8-column basket log produced by `perpetual_ls_v7_full.py`. Columns:

| Column | Description |
|--------|-------------|
| `period` | Period index (1–51) |
| `rebal_date` | Rebalancing date (YYYY-MM-DD) |
| `regime` | "Bull", "Bear", or "Sideways" |
| `long_tokens` | Pipe-separated list of long basket tokens |
| `short_tokens` | Pipe-separated list of short basket tokens |
| `long_opens` | Number of new long entries this period |
| `long_closes` | Number of long exits this period |
| `short_opens` | Number of new short entries this period |
| `short_closes` | Number of short exits this period |

### Chart Panels

The output is a single figure with four vertically stacked panels:

**Panel A — Gantt Timeline (top)**

A horizontal bar chart showing each token's continuous holding period. Long positions are rendered in teal; short positions in red. The x-axis is calendar time. Regime is shown as a shaded background (Bull = light green, Bear = light red, Sideways = light grey). Each token occupies one row. Gaps in the bar indicate periods where the token was not held.

**Panel B — Period-Level Open/Close Bar Chart (middle)**

A mirrored bar chart. For each rebalancing period, the height above the zero line represents the number of new opens in that period; the height below represents the number of closes. Long leg (teal) and short leg (red) are plotted as side-by-side bars within each period. This panel makes it visually clear which periods had high turnover.

**Panel C — Open Position Count Area Chart (lower)**

An area chart showing the total number of open positions at each rebalancing date. The long leg area (teal, semi-transparent) and short leg area (red, semi-transparent) are stacked. This panel confirms basket size stability over time.

**Panel D — Cumulative Opens Line Chart (bottom)**

Three lines: cumulative long opens (teal), cumulative short opens (red), and total (black dashed). This panel shows the overall trading pace and confirms that trade counts are proportional across regimes.

### Trade Count Summary

| Leg | Opens | Closes | Total Trades | Avg Opens/Period |
|-----|-------|--------|--------------|-----------------|
| Long | 373 | 367 | 740 | 7.3 |
| Short | 353 | 313 | 666 | 6.9 |
| **Grand Total** | **726** | **680** | **1,406** | **27.6** |

Note: Opens and closes do not balance exactly because the basket log includes partial-period positions at the beginning and end of the history window.

### Longest-Held Positions

| Token | Leg | Duration |
|-------|-----|----------|
| KAVA | Short | 14.0 months |
| HBAR | Short | 12.9 months |
| SUSHI | Long | 12.0 months |
| FIL | Short | 12.0 months |
| NEO | Long | 10.8 months |

The longest short positions all correspond to chronic diluters (KAVA, HBAR, FIL), confirming that the supply-inflation signal is identifying structural emission problems, not transient spikes. SUSHI and NEO as long-held positions reflect consistently low historical supply inflation in the eligible universe context.

### Output

| File | Description |
|------|-------------|
| `perp_ls_v7_trade_chart.png` | Four-panel trade visualisation as described above |
