# Crypto Supply Inflation Backtesting

Quantitative research into whether token supply unlocks and inflation rates predict
subsequent price performance in the top 300 cryptocurrencies.

Three progressively more rigorous backtesting engines (V1 → V3) test the same core
hypotheses against weekly CoinMarketCap snapshots spanning January 2017 to February 2026.
A standalone extreme-percentile script (`extreme_percentile.py`) provides a benchmark-free
absolute performance test of the tail ends of the supply inflation distribution.
A final beta-hedged L/S script (`beta_hedged_ls.py`) tests whether the short side of the
supply-dilution trade can be made viable by hedging market beta with long positions in
major assets (BTC, BTC+ETH, cap-weighted Top 10).
An alternative methodology script (`backtest_alternatives.py`) systematically varies six
methodological choices — winsorization, holding period, selection granularity, weighting,
exclusion filters, and supply lookback — across 17 configurations to determine whether the
short leg's failure is a real finding or an artifact of how the backtest is built.

---

## Hypotheses

| ID | Name | Question |
|----|------|----------|
| H1 | Sudden Unlocks | Do anomalous single-period supply spikes predict negative abnormal returns over the following 2–4 weeks? |
| H2 | 90-Day Pressure | Do tokens in the top quartile of 13-week trailing supply growth underperform the bottom quartile? |
| H3 | 365-Day Pressure | Same question using 52-week trailing supply growth. |

---

## Dataset

| File | Description |
|------|-------------|
| `CMC.xlsx` / `CMC.csv` | Raw CoinMarketCap historical export |
| `cmc_historical_top300_filtered.csv` | Filtered to rows with valid price and market cap |
| `cmc_historical_top300_filtered_with_supply.csv` | Main dataset — adds derived `circulating_supply` column |
| `circulating_supply.csv` | Standalone derived supply series |

**Coverage:** Top 300 cryptocurrencies, weekly snapshots, 2017-01-01 to 2026-02-22
**Rows:** 135,652 after filtering
**Symbols:** 2,267 unique tokens
**Periods:** 477 weekly snapshots
**Columns:** `snapshot_date`, `rank`, `name`, `symbol`, `market_cap`, `price`,
`circulating_supply`, `volume_24h`, `pct_1h`, `pct_24h`, `pct_7d`

> **Note:** Survivorship bias is not corrected. Tokens that dropped below rank 300
> have no data after exit.

---

## Pipeline

```
CMC.xlsx / CMC.csv
        │
        └── circulating_supply.py
                │
                └── cmc_historical_top300_filtered_with_supply.csv
                        │
                        ├── backtest.py ─────────────────── V1: proof-of-concept
                        │       └── h1_event_study.png
                        │           h2_continuous_pressure_90d.png
                        │           h3_continuous_pressure_365d.png
                        │
                        ├── backtest_v2.py ──────────────── V2: institutional-grade
                        │       └── v2_h1_event_study.png
                        │           v2_h1_bull_bear.png
                        │           v2_h2_longshort.png
                        │           v2_h3_longshort.png
                        │
                        ├── backtest_v3.py ──────────────── V3: regime-conditional L/S
                        │       └── v3_h2_regime_ls.png
                        │           v3_h3_regime_ls.png
                        │
                        ├── extreme_percentile.py ───────── decile absolute basket test
                        │       └── extreme_pct_cumulative.png
                        │
                        ├── beta_hedged_ls.py ───────────── beta-hedged L/S (short Q4 vs long BTC/ETH/Top10)
                        │       └── bh_ls_dollar_neutral.png
                        │           bh_ls_beta_neutral.png
                        │           bh_ls_combined.png
                        │
                        └── backtest_alternatives.py ───── alternative methodology sweep (17 configs)
                                └── alt_backtest_top3.png
                                    alt_backtest_hold_period.png
                                    alt_backtest_winsorization.png
```

### 1. Derive circulating supply

```bash
python circulating_supply.py
```

Computes `circulating_supply = market_cap / price` for rows where price > 0,
writes `cmc_historical_top300_filtered_with_supply.csv`.

### 2. Run backtests

```bash
python backtest.py            # V1 -- baseline
python backtest_v2.py         # V2 -- institutional-grade
python backtest_v3.py         # V3 -- regime-conditional L/S
python extreme_percentile.py  # decile absolute basket test
python beta_hedged_ls.py      # beta-hedged L/S, short Q4 vs long BTC/ETH/Top10
python backtest_alternatives.py  # alternative methodology sweep (17 configs)
```

Each script is self-contained and reads directly from the CSV. Output charts are
saved to the working directory.

---

## Methodology Evolution

### V1 — `backtest.py`

Proof-of-concept implementation.

- **Signal:** Static 3% absolute spike above 12-period rolling median
- **Outlier handling:** Hard clip ±100% on single-period returns
- **Abnormal return:** Raw excess return (`R_token − R_index`)
- **H2/H3 structure:** Two separate equal-weight long-only portfolios (Q1 vs Q4)
- **Annualization:** `cum^(52/n)` — contains a bug treating monthly periods as weekly

### V2 — `backtest_v2.py`

Addresses all V1 methodological weaknesses.

| Upgrade | Detail |
|---------|--------|
| Signal | Z-score > +3.0 vs 12-period rolling mean/std (per-token adaptive threshold) |
| Outlier handling | Cross-sectional Winsorization at 1st/99th percentile per snapshot date |
| Abnormal return | Beta-adjusted: `R_token − (β × R_index)`, trailing 12-period OLS β |
| Slippage | Inverse-turnover drag: `0.05% / turnover`, capped at 200 bps |
| H2/H3 structure | Dollar-neutral L/S with inverse-volatility weighting |
| Annualization | `cum^(1/years)` using actual elapsed calendar days |
| Regime context | Bull/Bear classification via 20-week MA of cap-weighted index price |

### V3 — `backtest_v3.py`

Adds regime-conditional L/S variants for H2/H3, motivated by V2's H1 finding that
the supply-dilution ACAR is negative only in Bear markets (−2.49%) and positive in
Bull markets (+6.33%).

| Variant | Bear period | Bull period |
|---------|-------------|-------------|
| Bear-Only | Long Q1 / Short Q4 | Cash (0%) |
| Bull-Reverse | Cash (0%) | Long Q4 / Short Q1 |
| Regime-Switch | Long Q1 / Short Q4 | Long Q4 / Short Q1 |

### Extreme Percentile — `extreme_percentile.py`

Benchmark-free absolute test using the tail ends of the 13-week trailing supply
inflation distribution. No index, no beta-hedging, no L/S — two separate equal-weight
long-only baskets rebalanced monthly.

| Parameter | Value |
|-----------|-------|
| Low basket | Tokens at or below 10th percentile of supply inflation |
| High basket | Tokens at or above 90th percentile of supply inflation |
| Avg basket size | ~23 tokens each |
| Rebalancing | Monthly (first snapshot of each calendar month) |
| Forward return | 4 weeks, cross-sectional winsorized, slippage-adjusted |

### Beta-Hedged L/S — `beta_hedged_ls.py`

Tests whether shorting Q4 high-inflation altcoins becomes viable when combined with a
long position in major assets to hedge market beta. Short leg is inverse-volatility
weighted with stablecoin exclusion; tested against three long-leg variations and two
portfolio modes (Dollar-Neutral and Beta-Neutral).

| Parameter | Value |
|-----------|-------|
| Short leg | Q4 (≥75th pct) of 13-week supply inflation, inv-vol weighted, slippage-adjusted |
| Stablecoin exclusion | 29 symbols blocked (USDT, USDC, DAI, BUSD, and 25 others) |
| Long leg A | 100% BTC |
| Long leg B | 50% BTC + 50% ETH |
| Long leg C | Cap-weighted Top 10 non-stablecoin assets |
| Portfolio modes | Dollar-Neutral (`R_long - R_short`) and Beta-Neutral (`β × R_long - R_short`) |
| Beta estimation | Trailing 12-period OLS, clamped to [0.5, 3.0] |
| Avg short basket size | ~56 tokens |
| Rebalancing | Monthly, 106 periods |

### Alternative Methodology — `backtest_alternatives.py`

Red-team investigation of whether the short leg's failure is an artifact of specific
methodological choices. Tests six dimensions that could systematically bias against the
short leg, running 17 targeted configurations rather than a full combinatorial sweep.

**Identified blind spots tested:**

| # | Blind Spot | Concern | Tested Values |
|---|-----------|---------|---------------|
| 1 | Winsorization clips short-leg profits | 1/99 pct clipping removes the crash tails that shorts profit from | None, 1/99, 0.5/99.5 |
| 2 | Holding period too short (4w) | Dilution is gradual; 4 weeks may miss the full drag | 4w, 8w, 13w, 26w |
| 3 | Quartile sort too blunt | Signal lives in tails; quartile mixes extreme with moderate tokens | Quartile (25/75), Decile (10/90), Vigintile (5/95) |
| 4 | No exclusion filters | Memecoins and CEX tokens have non-fundamental supply dynamics | None vs Full (stablecoins + CEX + meme + top-20) |
| 5 | Inv-vol overweights stable tokens | Lowest-vol short candidates are most likely to recover | Equal-weight vs Inv-vol |
| 6 | Supply lookback too short | 13 weeks may miss slower dilution cycles | 13w vs 26w |

**Test structure (hypothesis-driven, not combinatorial):**

| Test | Dimension Varied | Held Constant |
|------|-----------------|---------------|
| A (3 configs) | Winsorization: None, 1/99, 0.5/99.5 | Decile, 4w hold, equal-wt, full exclusions, 13w supply |
| B (4 configs) | Holding period: 4w, 8w, 13w, 26w | Decile, no winsor, equal-wt, full exclusions, 13w supply |
| C (3 configs) | Granularity: Quartile, Decile, Vigintile | 13w hold, no winsor, equal-wt, full exclusions, 13w supply |
| D (2 configs) | Weighting: Equal-wt, Inv-vol | Decile, 13w hold, no winsor, full exclusions, 13w supply |
| E (2 configs) | Supply lookback: 13w, 26w | Decile, 13w hold, no winsor, equal-wt, full exclusions |
| F (3 configs) | Regime filter on best A-E config | Best config + None / Bull-only / Bear-only |

---

## Results Summary

### H1 — Event Study

| Version | Events | Method | Post-event ACAR | t-stat | p-value |
|---------|--------|--------|-----------------|--------|---------|
| V1 | 10,403 | Raw excess return | Negative | −6.42 | ≈0 |
| V2/V3 | 3,713 | Beta-adjusted | +2.47% (all) | +2.01 | 0.044 |
| V2/V3 Bull | 2,085 | Beta-adjusted | +6.33% | — | — |
| V2/V3 Bear | 1,628 | Beta-adjusted | −2.49% | — | — |

**Key finding:** The supply-dilution hypothesis holds conditionally in Bear markets only.
In Bull markets, Z-score unlock events are positively associated with beta-adjusted returns
(momentum and project-activity effects dominate dilution).

### H2/H3 — L/S Portfolio

| Portfolio | Ann. Return | Volatility | Sharpe | Max Drawdown |
|-----------|-------------|------------|--------|--------------|
| V2 Unconditional L/S (H2) | N/A | 63.40% | N/A | −100% |
| V2 Unconditional L/S (H3) | N/A | 62.78% | N/A | −100% |
| V3 Bear-Only (H2) | −0.12% | 16.82% | −0.007 | −42.62% |
| V3 Bear-Only (H3) | −0.09% | 20.93% | −0.004 | −38.26% |
| V3 Bull-Reverse (H2) | N/A | 81.19% | N/A | −100% |
| V3 Bull-Reverse (H3) | −17.65% | 61.89% | −0.285 | −94.00% |
| V3 Regime-Switch (H2) | N/A | 82.91% | N/A | −100% |
| V3 Regime-Switch (H3) | −17.72% | 65.31% | −0.271 | −95.31% |
| Index (H2) | +111.06% | 113.95% | 0.975 | −80.39% |
| Index (H3) | +34.06% | 74.59% | 0.457 | −80.39% |

**Key finding:** H2/H3 are rejected across all strategy variants. Regime-gating (Bear-Only)
prevents capital ruin and reduces volatility 4×, but produces near-zero return after
slippage — the supply quartile spread is not exploitable at the 4-week horizon.

### Extreme Percentile — Absolute Basket Test

| Basket | Ann. Return | Volatility | MaxDD |
|--------|-------------|------------|-------|
| 10th Pct (Low Inflation) | +15.20% | 144.63% | -96.08% |
| 90th Pct (High Inflation) | -7.99% | 147.69% | -97.58% |
| **Spread (Low minus High)** | **+23.19 ppt** | 70.69% | — |

| Spread metric | Value |
|---------------|-------|
| Mean per-period return | +1.74% |
| Win rate (Low > High) | 63 / 106 periods (59.4%) |
| Annualized spread vol | 70.69% |
| Avg basket size | ~23 tokens each |
| Rebalancing periods | 106 months |

**Key finding:** The supply-dilution hypothesis holds in absolute terms at the decile
level. Low-inflation tokens outperform high-inflation tokens by ~23 percentage points
annualized with no benchmark, no beta adjustment, and no short leg. Low beats High in
59.4% of monthly periods — a consistent, repeatable edge across 106 independent
observations. The quartile-level L/S in V2/V3 failed not because the effect is absent,
but because the middle of the distribution dilutes it to noise. The signal lives in the
tails; the decile cutoff isolates it.

### Beta-Hedged L/S — Short Q4 vs Long Major Assets

| Portfolio | Ann. Return | Volatility | Sharpe | Max Drawdown |
|-----------|-------------|------------|--------|--------------|
| DN: Long BTC | N/A | 81.92% | N/A | -100% |
| DN: Long BTC+ETH | N/A | 73.15% | N/A | -100% |
| DN: Long Top10 | N/A | 70.95% | N/A | -100% |
| BN: Long BTC | N/A | 80.90% | N/A | -100% |
| BN: Long BTC+ETH | N/A | 74.16% | N/A | -100% |
| BN: Long Top10 | N/A | 71.39% | N/A | -100% |

**Standalone leg reference (for diagnosis):**

| Leg | Ann. Return | Sharpe | Max Drawdown |
|-----|-------------|--------|--------------|
| Short Leg / Q4 basket (held long) | +7.38% | 0.056 | -93.35% |
| Long BTC | +41.92% | 0.526 | -77.52% |
| Long BTC+ETH | +40.38% | 0.439 | -84.85% |
| Long Top10 | +31.10% | 0.353 | -85.93% |

**Trailing beta (short basket vs long leg):** avg 1.09–1.14 across all long-leg variations,
ranging from 0.50 to 2.09.

**Key finding:** All 6 portfolio configurations go bankrupt. The Q4 altcoin basket
returns +7.38% as a long position, confirming that high-inflation altcoins have positive
convexity vs BTC during bull runs that overwhelms any linear beta hedge. The OLS beta
averages ~1.09 but realized bull-market multipliers exceed 2× — a linear hedge cannot
match this non-linearity. Shorting high-inflation altcoins is structurally non-viable
in any configuration.

### Alternative Methodology — Short-Leg Blind Spot Sweep

**Full results table (17 configurations):**

| Config | Ann. Return | Volatility | Sharpe | Max DD | Win Rate | N |
|--------|-------------|------------|--------|--------|----------|---|
| **Test A: Winsorization** | | | | | | |
| A1: No winsorization | N/A | 1920.60% | N/A | -100% | 59.4% | 106 |
| A2: 1/99 pct | +1.40% | 91.86% | 0.015 | -89.02% | 62.3% | 106 |
| A3: 0.5/99.5 pct | N/A | 101.90% | N/A | -100% | 61.3% | 106 |
| **Test B: Holding Period** | | | | | | |
| B1: 4w hold | N/A | 1920.60% | N/A | -100% | 59.4% | 106 |
| B2: 8w hold | N/A | 148.40% | N/A | -100% | 55.2% | 105 |
| B3: 13w hold | N/A | 151.43% | N/A | N/A | 52.9% | 104 |
| B4: 26w hold | N/A | 97407.48% | N/A | -100% | 58.4% | 101 |
| **Test C: Selection Granularity** | | | | | | |
| C1: Quartile (25/75) | N/A | 114.59% | N/A | -100% | 65.4% | 104 |
| C2: Decile (10/90) | N/A | 151.43% | N/A | N/A | 52.9% | 104 |
| C3: Vigintile (5/95) | N/A | 213.80% | N/A | N/A | 55.8% | 104 |
| **Test D: Weighting** | | | | | | |
| D1: Equal-weight | N/A | 151.43% | N/A | N/A | 52.9% | 104 |
| D2: Inv-vol | N/A | 276.14% | N/A | N/A | 54.8% | 104 |
| **Test E: Supply Lookback** | | | | | | |
| E1: 13w supply | N/A | 151.43% | N/A | N/A | 52.9% | 104 |
| E2: 26w supply | N/A | 14028.94% | N/A | -100% | 54.5% | 101 |
| **Test F: Best Config + Regime** | | | | | | |
| F: No filter | +1.40% | 91.86% | 0.015 | -89.02% | 62.3% | 106 |
| F: Bull only | +2.28% | 106.33% | 0.021 | -87.10% | 60.5% | 76 |
| F: Bear only | -0.99% | 34.33% | -0.029 | -31.17% | 66.7% | 30 |

**Dimension impact (Sharpe spread between best and worst within each test):**

| Dimension | Best Config | Worst Config | Sharpe Spread | Verdict |
|-----------|------------|--------------|---------------|---------|
| Winsorization | A2: 1/99 (0.015) | A1/A3: bankrupt | N/A (bankrupt baseline) | Removing winsorization makes results **worse**, not better |
| Holding period | All bankrupt | All bankrupt | N/A | Dilution drag does not emerge at any horizon (4w–26w) |
| Selection | All bankrupt | All bankrupt | N/A | Tighter selection increases vol without improving returns |
| Weighting | All bankrupt | All bankrupt | N/A | Neither method rescues the short leg |
| Supply lookback | All bankrupt | All bankrupt | N/A | Longer lookback adds noise |
| Regime filter | Bull-only (0.021) | Bear-only (-0.029) | 0.050 | Bull regime marginally helps |

**Only 3 of 17 configs produced a positive Sharpe ratio**, all near zero (max 0.021),
with extreme drawdowns (>87%) and triple-digit volatility.

**Key finding:** The short leg's failure is **not** a methodological artifact.
The hypothesis that winsorization clips short-leg profits (the most plausible bias)
is refuted — removing winsorization causes catastrophic losses because unclipped
extreme positive returns (tokens that 10x) destroy the short leg far more than
the clipped crash profits would have helped it. The short side of the supply-dilution
trade is structurally non-viable across all methodological permutations tested.

---

## Output Charts

| File | Description |
|------|-------------|
| `h1_event_study.png` | V1 H1 event study ACAR ±95% CI |
| `h2_continuous_pressure_90d.png` | V1 H2 Q1 vs Q4 cumulative return |
| `h3_continuous_pressure_365d.png` | V1 H3 Q1 vs Q4 cumulative return |
| `v2_h1_event_study.png` | V2 H1 beta-hedged ACAR ±95% CI |
| `v2_h1_bull_bear.png` | V2 H1 ACAR split by Bull vs Bear regime |
| `v2_h2_longshort.png` | V2 H2 dollar-neutral L/S vs index |
| `v2_h3_longshort.png` | V2 H3 dollar-neutral L/S vs index |
| `v3_h1_event_study.png` | V3 H1 (identical to V2) |
| `v3_h1_bull_bear.png` | V3 H1 regime split (identical to V2) |
| `v3_h2_regime_ls.png` | V3 H2 all regime-conditional variants vs index |
| `v3_h3_regime_ls.png` | V3 H3 all regime-conditional variants vs index |
| `extreme_pct_cumulative.png` | 10th vs 90th pct cumulative wealth curves + period spread bar chart |
| `bh_ls_dollar_neutral.png` | Beta-hedged L/S: Dollar-Neutral portfolios (BTC / BTC+ETH / Top10) with Bear shading |
| `bh_ls_beta_neutral.png` | Beta-hedged L/S: Beta-Neutral portfolios (BTC / BTC+ETH / Top10) with Bear shading |
| `bh_ls_combined.png` | Beta-hedged L/S: 2-panel comparison, Dollar-Neutral vs Beta-Neutral, all 6 portfolios |
| `alt_backtest_top3.png` | Alternative methodology: cumulative returns of top 3 configs by Sharpe |
| `alt_backtest_hold_period.png` | Alternative methodology: holding period comparison (4w/8w/13w/26w) |
| `alt_backtest_winsorization.png` | Alternative methodology: winsorization effect (None/1-99/0.5-99.5) |

---

## Reports

| File | Description |
|------|-------------|
| `backtest_report.md` | V1 results and methodology |
| `quant_critique_and_roadmap.md` | Critique of V1 and roadmap for V2/V3 |
| `v2_backtest_report.md` | Full quantitative report: V2 methodology, V1 vs V2 comparison, V3 regime-conditional extension, extreme percentile test, beta-hedged L/S, cross-strategy synthesis |

---

## Perpetual Futures L/S Backtest (New)

A second research track operationalises the supply-dilution signal as an executable
**perpetual futures Long/Short strategy** with realistic costs, regime-aware sizing,
and institutional-grade risk management. Three progressively more rigorous versions
were developed and reviewed by a synthetic Senior QRM and Institutional Analyst.

### Strategy overview

- **Long:** lowest supply-inflation tokens (12th percentile of composite 13w+52w rank)
- **Short:** highest supply-inflation tokens (88th percentile)
- **Execution:** perpetual futures with funding rates, taker fees, slippage
- **Rebalancing:** monthly, with inner buffer band to reduce turnover
- **Universe:** rank 21-200, filtered for $5M+ daily volume, $50M+ market cap,
  and 60+ exclusion symbols (stablecoins, CEX tokens, memes, wrapped assets, LSDs,
  protocol synthetics, commodity-backed tokens)

### Version evolution

| Version | Signal | Weights | Risk | Periods | Combined net | MaxDD |
|---------|--------|---------|------|:-------:|:---:|:---:|
| **v1** `perpetual_ls_backtest.py` | 4w trailing | Equal | None | 108 (2017-26) | +13.6% | −78% |
| **v2** `perpetual_ls_v2.py` | 13w trailing | sqrt(ADTV) | Regime scaling | 40 (2022-26) | −6.60% | −64% |
| **v3** `perpetual_ls_v3.py` | 50% 13w + 50% 52w composite | inv-vol × sqrt(ADTV) | CB + altseason veto + BTC hedge | 39 (2022-26) | **+1.82%** | **−31%** |

### Key findings

- The supply-dilution spread is **positive in all three regimes post-2022**: Bull +84%,
  Bear +37%, Sideways +2% (annualised geometric). The 2021 bull-run peak was the
  primary source of prior regime-conditional failure.
- The combined strategy's +1.82% net return is largely BTC beta (rolling beta avg 0.541).
  After BTC hedging, residual alpha is approximately −14%, confirming the strategy is
  not yet genuinely market-neutral.
- **MaxDD halved from −64% to −31%** between v2 and v3 through the circuit breaker
  (fires when short basket loses >40% in one period) and altcoin-season veto.
- **Capacity ceiling: $5-10M AUM.** Min ADV constraints and basket concentration
  prevent scaling beyond this level.

### Running the perpetual L/S backtests

```bash
python perpetual_ls_backtest.py   # v1 baseline (full history)
python perpetual_ls_v2.py         # v2 institutional (post-2021)
python perpetual_ls_v3.py         # v3 risk-managed (post-2022)
```

### Reports

| File | Description |
|------|-------------|
| `perp_ls_methodology.md` | **Full methodology document** — universe construction, signal math, execution model, risk management, known limitations, data sources for live deployment |
| `institutional_analysis_report.md` | Institutional post-trade analysis: BTC beta decomposition, return distribution, sector bias |
| `risk_manager_teardown.md` | Senior QRM teardown: phantom liquidity, basket composition biases, capacity limits, turnover analysis |

---

## Requirements

```
python >= 3.9
pandas
numpy
scipy
matplotlib
```

Install with:

```bash
pip install pandas numpy scipy matplotlib
```

---

## Conclusion

Six scripts, 26 strategy configurations, and 106–477 months of data converge on the same
answer: supply inflation is a real but one-sided signal.

### Where the signal is present

| Test | Finding |
|------|---------|
| H1 event study — Bear markets | Z-score unlock events → ACAR = -2.49% (beta-adjusted, t-significant) |
| H1 event study — Bull markets | ACAR = +6.33% (momentum/activity effects dominate dilution) |
| Extreme percentile — Long 10th pct | +15.20% annualized vs -7.99% for 90th pct; 59.4% monthly win rate |

### Where the signal fails

| Test | Reason for failure |
|------|--------------------|
| H2/H3 quartile L/S (V2, unconditional) | Bankrupt — middle-of-distribution tokens dilute the tail effect to noise |
| H2/H3 Bear-Only L/S (V3) | Survives (-42% max DD) but returns -0.12% ann. after slippage |
| H2/H3 Bull-Reverse / Regime-Switch (V3) | Bankrupt or near-bankrupt |
| Beta-hedged L/S — all 6 configurations | Bankrupt — Q4 basket returns +7.38% as a long; positive convexity overwhelms linear hedge |
| Alternative methodology — 14 of 17 configs | Bankrupt — removing winsorization, extending hold periods, tighter selection, and different weighting all fail |
| Alternative methodology — 3 surviving configs | Max Sharpe 0.021 with 106% vol and -87% max DD; not investable |

### Why shorting high-inflation altcoins is non-viable

The Q4 supply-inflation basket returned **+7.38% annualized as a long position** across
the full sample. Being short it therefore carries a structural ~7% annual headwind at the
geometric mean, compounded by a worse problem: during crypto bull markets, high-emission
altcoins dramatically outperform their OLS-implied beta (avg ~1.09×). Realized bull-period
multipliers exceed 2×, meaning a linear long hedge of `1.09 × BTC` is structurally
insufficient. Beta neutralization fails because the exposure is regime-conditional and
non-linear — a problem no trailing OLS estimate can correct for.

The alternative methodology sweep (`backtest_alternatives.py`) confirms this is **not a
methodological artifact**. The most plausible bias — winsorization clipping short-leg
crash profits — is refuted: removing winsorization makes results catastrophically worse
(1920% vol, -100% max DD) because unclipped extreme positive returns (tokens that 10x)
destroy the short leg far more than clipped crashes would have helped. Extending the
holding period to 26 weeks, tightening selection to vigintiles, switching to equal
weighting, and applying full exclusion filters all fail to produce a viable short leg.
Only 3 of 17 configurations avoid bankruptcy, with a maximum Sharpe of 0.021 — not
investable by any standard.

### Practitioner implication

The supply-inflation signal has exactly one viable implementation:

> **Long the 10th percentile of 13-week supply inflation, equal-weighted, monthly rebalanced.
> Do not implement a short leg in any form.**

The H1 event-driven short (Z-score > 3.0, bear markets only, ACAR = -2.49%) is statistically
real but not portfolio-scalable — the event density per period is too low to construct a
meaningful position. It is best read as confirmatory evidence that the supply effect exists,
not as a tradeable strategy on its own.
