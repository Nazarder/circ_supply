# Supply Data Integrity: How Circulating Supply is Manipulated and Corrupted in Crypto

**Research context:** Supply-Dilution Long/Short Strategy — CMC Weekly Snapshots, Top 300 Tokens, 2017-01-01 to 2026-02-22
**Dataset:** 135,652 rows, 2,267 unique symbols, 477 weekly snapshots
**Signal basis:** `supply_inf_13w = circulating_supply(t) / circulating_supply(t-13) - 1`

---

## Table of Contents

1. [CMC Self-Reporting Problem (Root Cause)](#1-cmc-self-reporting-problem-root-cause)
2. [Multi-Chain Bridge Double-Counting](#2-multi-chain-bridge-double-counting)
3. [Emission Schedule Pauses and Manipulation](#3-emission-schedule-pauses-and-manipulation)
4. [Supply Unit Changes, Token Splits, and Merges](#4-supply-unit-changes-token-splits-and-merges)
5. [Exchange and Custodian Holding Exclusions](#5-exchange-and-custodian-holding-exclusions)
6. [Buyback-and-Burn vs Buyback-and-Lock](#6-buyback-and-burn-vs-buyback-and-lock)
7. [Airdrop Claim Windows](#7-airdrop-claim-windows)
8. [Protocol Revenue Redistribution as New Supply](#8-protocol-revenue-redistribution-as-new-supply)
9. [Cross-Chain Native Token Counting (L1s)](#9-cross-chain-native-token-counting-l1s)
10. [The Winsorization Partial Defense](#10-the-winsorization-partial-defense)
11. [Summary Table: Manipulation Type to Signal Direction to Model Reaction](#11-summary-table-manipulation-type--signal-direction--model-reaction)
12. [Recommended Mitigations](#12-recommended-mitigations)

---

## 1. CMC Self-Reporting Problem (Root Cause)

### The Fundamental Architecture Failure

CoinMarketCap does not verify circulating supply on-chain. Instead, it relies on projects to self-report their circulating supply figure through a manual submission process or API endpoint they control. CMC publishes what projects tell it. There is no independent on-chain reconciliation, no cryptographic proof, and no audit trail.

This is the root cause from which every other data integrity problem in this document descends. Every manipulation type described below is either (a) enabled by the absence of on-chain verification, (b) amplified by CMC's reporting lag, or (c) both.

### The Derived Supply Problem

This research compounds the problem further. The primary `circulating_supply` field used throughout the backtest is not taken directly from CMC's reported supply field. It is derived:

```
circulating_supply = market_cap / price
```

Both `market_cap` and `price` carry independent measurement error from CMC's data ingestion pipeline. When these two noisy quantities are divided, errors compound multiplicatively. A 0.5% CMC price feed lag combined with a 0.5% market cap reporting lag can produce a phantom supply change of approximately 1%. At the 13-week trailing window scale, individual errors can wash out — but correlated errors (e.g., CMC updates market cap at a different cadence than price for a specific token) produce persistent systematic bias in the derived supply series.

**Quantified evidence from the dataset:**

- BTC: 178 weekly periods where `circulating_supply = market_cap / price` exceeded the known hard-capped maximum supply of 21,000,000 BTC. This is mathematically impossible on-chain and proves the derived metric carries significant noise even for the most-scrutinized asset in crypto.
- ETH: 152 such periods where the derived supply exceeded any reasonable estimate of ETH total supply.
- BNB: 145 such periods.

These are not edge cases. For the three largest tokens by market cap — tokens with the deepest analytical coverage, the most liquid markets, and the most scrutinized supply metrics — the derived circulating supply figure is wrong more than 30% of the time in absolute terms.

### The Reporting Latency Problem

Even when a project reports accurately and CMC records it correctly, the weekly snapshot introduces an irreducible timing problem. On-chain vesting contracts execute at block time (seconds). The actual supply release hits the circulating supply metric at some point between snapshot T and T+1 — but where within that 7-day window is unknown. This creates ±3.5 days of timing jitter on every supply event detection. For a signal whose holding window is 13 weeks, this matters less than for event-driven strategies, but it means the model is always reacting to supply changes that happened days or weeks ago.

---

## 2. Multi-Chain Bridge Double-Counting

### Mechanism

When a token is bridged from its native chain (Chain A) to a second chain (Chain B), the economic process is:

1. Token holder on Chain A deposits N tokens into the bridge contract.
2. Bridge contract on Chain A locks the N tokens (marks them as held in escrow).
3. Bridge contract on Chain B mints N wrapped representations of those tokens.
4. The user now holds N wrapped tokens on Chain B.

The correct accounting treatment: the N tokens on Chain A are locked (and should be excluded from circulating supply), while the N wrapped tokens on Chain B are now circulating. Net change to true circulating supply: zero.

**What CMC actually does:** This is inconsistent and project-dependent. CMC may or may not exclude the locked bridge deposits on Chain A from its reported circulating supply. Whether it does depends on whether the project has submitted an updated wallet exclusion list that includes the bridge escrow contract address. Meanwhile, the N wrapped tokens on Chain B are generally counted as circulating because they are actively tradeable.

The result is a temporal pattern of data artifacts:

| Event | CMC Effect |
|-------|-----------|
| Bridge opens, tokens flow Chain A → Chain B | CMC may still count original Chain A tokens as circulating AND counts new Chain B tokens → apparent supply spike |
| Bridge exclusion list updated by project | Chain A bridge deposits suddenly excluded → apparent supply drop |
| Tokens return Chain B → Chain A (bridge unwind) | Chain B tokens burned, Chain A tokens unlocked → apparent supply drop then spike depending on update timing |

### Examples in the Dataset

1INCH, APT, and ONDO show classic spike-drop artifact patterns in the weekly supply series. The spikes are followed within 4–12 weeks by drops of comparable magnitude. This is the fingerprint of bridge events: supply appears to increase when the bridge opens (or when CMC is slow to update the exclusion list), then corrects when the project updates its reported circulating supply.

Wrapped assets as a category were explicitly excluded from the strategy universe for this reason. The `WRAPPED_ASSETS` exclusion set in the strategy code includes WBTC, BTCB, WETH, WBNB, and various bridged Bitcoin representations, precisely because their supply dynamics are pure accounting artifacts with no relationship to token dilution.

### How the Model Reacts

The bridge double-counting artifact produces random noise in the supply signal:

- **When the bridge opens (apparent spike):** `supply_inf_13w` rises. If large enough, the token ranks high on the supply inflation cross-section → enters the **SHORT basket**. The model is shorting not because of genuine dilution but because of a bridge accounting event.
- **When the bridge exclusion is updated (apparent drop):** `supply_inf_13w` falls. If the token had previously been shorted, the apparent "supply deflation" now causes it to rank low → becomes a **LONG candidate**. The model is now longing a token that was previously shorted due to the same bridge.

The net effect is random-direction signal noise generated by infrastructure events rather than by economic dilution. The SHORT fires on bridge openings; the LONG fires on bridge corrections. Neither is correct. The strategy is paying transaction costs to trade on accounting ledger updates.

**Severity:** High. The dataset shows this pattern across hundreds of tokens. Every multi-chain token with active bridge activity is potentially affected at every rebalancing period.

---

## 3. Emission Schedule Pauses and Manipulation

### Mechanism

Token vesting and emission contracts are smart contracts with administrative functions. Most include a `pause()` function that allows the contract owner (typically a multisig controlled by the project team) to halt all token emissions until `unpause()` is called. This is a legitimate safety feature — if a vesting contract is exploited, pausing it prevents additional damage.

However, the same function can be used strategically. A team facing an upcoming supply-inflation ranking evaluation can pause the vesting contract for exactly long enough to suppress the 13-week trailing supply inflation signal, then resume normal emissions afterward. The supply appears flat; the signal shows zero inflation; the model ranks the token as a low-inflation long candidate.

### Empirical Evidence from the Dataset

Emission pause tokens identified by looking for tokens with exact-zero week-over-week supply changes for extended consecutive periods (indicating the supply series is literally flat — no rounding noise, no fractional changes, just zero):

| Token | Weeks of Flat Supply | Interpretation |
|-------|:-------------------:|----------------|
| RLC (iExec) | **207 weeks** | ~4 years of reported zero emission |
| NEO | **158 weeks** | ~3 years |
| LINK (Chainlink) | **156 weeks** | ~3 years |
| QNT (Quant) | **153 weeks** | ~3 years |
| GAS (NEO Gas) | **122 weeks** | ~2.4 years |
| MANA (Decentraland) | **112 weeks** | ~2.2 years |
| STORJ | **109 weeks** | ~2.1 years |

These figures are not plausible as genuine zero-emission periods for all tokens. In the case of LINK, for example, Chainlink's vesting schedule involves periodic team and investor distributions. 156 consecutive weeks of zero circulating supply change is almost certainly a CMC reporting artifact or a combination of CMC data non-updates and the derived supply measurement noise washing out small changes.

However, the mechanism is real regardless of cause: when `supply_inf_13w = 0` across the trailing 13 weeks, the token ranks at the bottom of the supply inflation cross-section. It enters the **LONG basket** every period it remains flat.

### The Gaming Window

The critical observation for strategy robustness: **our short signal window is exactly 13 weeks**.

A project team that understands this strategy (or any supply-inflation-based factor model with a 13-week lookback) has a precise gaming window. By pausing token emissions for 13 consecutive weeks before a major evaluation date (a token listing, a fund rebalancing, or any event that drives capital allocation decisions), the team can ensure their token ranks in the bottom decile of supply inflation. After the evaluation period, they resume emissions and the inflation returns to the supply series.

This is not hypothetical. The V7 development notes document an attempt to add a 4-week supply-change component to the composite signal. The result: "Tokens with a temporarily quiet 4-week supply period but high trailing 13w/52w inflation were incorrectly promoted to longs. In practice these were tokens mid-vesting-cycle (supply paused between cliff events), which subsequently resumed dilution." The model already encounters this problem within the existing data; it is a real phenomenon, not a theoretical concern.

### How the Model Reacts

**Flat supply = 0% 13-week inflation = very low composite rank = LONG basket.**

The signal is arguably correct in the literal sense: if a token genuinely has zero inflation for 13 weeks, there is no dilution pressure. The problem is twofold:

1. **It can be gamed:** A team controls the pause() function. The signal can be manufactured without any genuine change in the long-term emission schedule.
2. **The signal reversal is abrupt:** When emissions resume after a pause, the trailing 13-week window suddenly shows concentrated inflation (all the weeks that were zero are now being compared against newly inflated weeks). The token rapidly rotates from LONG basket to SHORT basket. The model exits a long position and potentially enters a short at the exact moment of the emission restart — a discrete event the market already knew was coming.

---

## 4. Supply Unit Changes, Token Splits, and Merges

### Mechanism

Token redenominations, migrations, and rebrand events change the total number of tokens outstanding without any change in the underlying economic value or ownership. A "token split" multiplies the number of tokens by some factor (like a stock split); a "token merge" (reverse split, consolidation) divides them.

Because CMC reports the raw token count, not the economic value of the circulating supply, these events produce discontinuous jumps in the supply series that have no relationship to genuine dilution.

### GALA: The Textbook Example

GALA underwent a supply restructuring that changed its supply from billions of tokens to hundreds of millions — a reduction of approximately 87% in reported circulating supply. CMC records this as an -87% long-term deflation signal. In reality, no economic value was destroyed; the restructuring was a redenomination. Holders of old GALA tokens received new GALA tokens at the appropriate conversion ratio.

**CMC shows:** `-87% apparent deflation`
**Economic reality:** `0% change in economic value`

### MATIC to POL Rebrand

The Polygon network's rebrand from MATIC to POL involved a token migration where MATIC tokens were exchanged for POL tokens. Depending on the precise conversion ratio and timing of CMC's supply updates for each symbol, this migration can appear in the supply series as a discontinuous supply change on one or both symbols.

### How the Model Reacts

The signal is entirely backward:

| Event | Apparent Supply Change | Signal Direction | Actual Economic Effect |
|-------|----------------------|-----------------|----------------------|
| Token split (1:10 ratio) | +900% supply inflation | SHORT | Zero dilution |
| Token merge (10:1 ratio) | -90% supply deflation | LONG | Zero anti-dilution |
| Token migration (old → new) | -100% on old symbol, large spike on new | SHORT on new | Zero dilution |

The cross-sectional winsorization at 2nd/98th percentile clips the most extreme versions of these events (a 900% spike would be clipped), but it does not remove them. A 2x token split (100% apparent inflation) survives the winsorization and ranks in the top percentile of supply inflation, triggering a SHORT signal that is entirely spurious.

**Severity:** Medium frequency but high impact when it occurs. The model reacts with maximum-confidence signals (these events are extreme on the supply inflation cross-section) to events that carry zero informational content about dilution.

---

## 5. Exchange and Custodian Holding Exclusions

### Mechanism

CMC adjusts its reported circulating supply based on wallet labels. Certain large wallet addresses are known to be exchange cold storage, protocol treasuries, or other non-circulating holdings. When CMC excludes these wallets from its circulating supply calculation, the reported supply decreases relative to what would be counted if all on-chain tokens were treated as circulating.

The problem arises not from the exclusion itself (which is economically reasonable) but from the process of updating these exclusions. When a large exchange moves tokens from a hot wallet to a cold wallet — a routine security operation — and CMC subsequently updates its exclusion list to reflect the new cold wallet address, the reported circulating supply drops. No actual economic change occurred: the tokens were held by the same entity the whole time. Only the label changed.

The reverse occurs when exchanges move coins from cold wallets that were previously excluded back to circulating wallets, or when CMC's automated systems fail to track wallet migrations properly.

### Examples of the Mechanism

- Exchange moves 100M tokens from hot wallet (counted as circulating) to cold wallet (excluded from circulating). CMC updates its tracking. **Result:** 100M token apparent supply decrease. No economic change.
- Exchange restructures its cold storage across multiple new wallet addresses. CMC's old exclusion entries no longer match the new addresses. **Result:** Apparent supply increase as tokens that were excluded (old addresses) are no longer excluded, while the new addresses are not yet on the exclusion list.
- Exchange insolvency (FTX 2022): Exchange tokens that were previously excluded as "exchange holdings" become unaccounted. **Result:** Erratic supply reporting for tokens that had significant FTX custodianship.

### How the Model Reacts

Exchange bookkeeping updates look indistinguishable from real supply changes in the weekly CMC data:

- **Apparent supply decrease (exchange adds cold wallet to exclusion list):** Signal shows deflation → **LONG signal**. This rewards the model for detecting an exchange's internal security operation, not for identifying a token with low dilution.
- **Apparent supply increase (exclusion list lag):** Signal shows inflation → **SHORT signal**. This punishes tokens for their exchange custody arrangements, not for genuine issuance.

This is a pure data artifact with no economic content, yet it is indistinguishable from genuine supply dynamics in the weekly snapshot data. The only mitigation is the WoW stability filter described in Section 12, which would catch large enough custody movements.

---

## 6. Buyback-and-Burn vs Buyback-and-Lock

### The Genuine vs Fake Burn Distinction

**Genuine burn:** The project purchases tokens on the open market or uses protocol revenue, then sends them to `0x000000000000000000000000000000000000dEaD` (the canonical burn address) or mints them to the null address. Anyone can verify on-chain that the tokens are permanently inaccessible. This genuinely reduces circulating supply and is irreversible.

**Fake burn / treasury lock:** The project announces a "burn" but sends the tokens to a multisig wallet controlled by the team, a proxy contract with an upgradeable admin, or any address where the team retains de facto control. This does not permanently remove tokens from circulation. The team can later "unlock" them, vote to re-issue them via governance, or simply send them to an exchange.

### CMC's Treatment

CMC reduces reported circulating supply when a wallet is labeled as a "burn address." The labeling is performed based on project-submitted information. CMC does not independently verify whether the destination address is a genuine irreversible burn or a team-controlled lock.

**The manipulation vector:** A team announces "We are burning 20% of the supply" (which creates positive price pressure and marketing momentum), sends tokens to a multisig labeled "burn.tokenname.eth" in ENS, submits the wallet label to CMC, and CMC reduces reported circulating supply by 20%. Reported supply deflation: -20%. Actual supply deflation: 0% (the team controls the wallet). The tokens can be re-introduced to circulation at any time.

This is not hypothetical. This pattern has been identified in multiple tokens across various market cycles. The absence of on-chain verification (Section 1) is precisely what makes this manipulation possible.

### How the Model Reacts

**Fake burn announcement:**

1. Project announces "burn." Price typically rises 10-30% on the news.
2. CMC updates reported circulating supply downward.
3. `supply_inf_13w` drops. Token ranks low on supply inflation cross-section.
4. Model enters **LONG position**.
5. Team controls the "burned" tokens. At any time they can reverse the lock and create genuine sell pressure.

The model is longing a token at the precise moment the team has manufactured a positive supply narrative — the worst possible entry point. The team now holds a large unlabeled position that can be sold into the model's long position.

**Severity:** High. This is an active manipulation vector, not a passive data artifact. The team has both the incentive (LONG signal creates buy demand that supports their sale) and the capability (control of multisig) to execute this manipulation systematically.

---

## 7. Airdrop Claim Windows

### Mechanism

At Token Generation Event (TGE), a project's reported circulating supply often reflects only the tokens allocated to:
- Team and advisor vesting schedules (partially vested at TGE)
- Seed and private sale investors (partially unlocked at TGE)
- Immediate liquidity allocations

The community airdrop allocation — which may represent 10-50% of total token supply — is frequently classified as "allocated but unclaimed" and excluded from circulating supply until claim windows open. This creates an artificial appearance of low initial circulating supply, which inflates the fully-diluted valuation multiple and makes the token appear scarcer than it is.

When the claim window opens, millions of eligible addresses claim their tokens in a compressed time window (days to weeks). The full airdrop allocation enters circulating supply essentially simultaneously. CMC records this as a supply spike of 30-50% or more within a single week.

### Examples in the Dataset

STRK (Starknet), ENA (Ethena), APT (Aptos), and ONDO all show massive single-week supply spikes in the CMC data. These spikes correspond to the opening of their community airdrop claim windows. The supply increase is real in the sense that these tokens genuinely moved from "unclaimed" to "claimed and held by thousands of community wallets." However, the event is not genuine dilution — the community allocation was disclosed at TGE; the claim window opening was on a known schedule; the "inflation" was simply the recording of pre-disclosed supply reaching circulating status.

### How the Model Reacts

This is one of the more nuanced cases because the signal is partially correct and partially wrong:

**Why the SHORT signal is technically correct:**
- The tokens are real and are now genuinely in circulating supply
- The new holders may sell to realize airdrop profits (the well-documented "airdrop farming dump" dynamic)
- There is genuine post-airdrop sell pressure from recipients who have zero cost basis

**Why the SHORT signal fires at the worst possible time:**
- Airdrop announcements typically coincide with peak market attention and hype for the token
- Price often spikes significantly in the days around the claim window opening (demand exceeds the seller supply)
- The SHORT signal enters at the hype peak, right as the token is experiencing its maximum positive attention
- The subsequent "airdrop dump" may or may not occur depending on whether the project has meaningful retention mechanisms

**Additionally:** The apparent 30-50% supply spike from a single airdrop claim window will survive the 2nd/98th percentile winsorization in some cases and will anchor the token in the top decile of the supply inflation cross-section for the subsequent 13 weeks — long after the actual claim event is complete and any post-airdrop selling pressure has resolved.

---

## 8. Protocol Revenue Redistribution as New Supply

### Mechanism

Some DeFi protocols create new token supply as rewards to stakers or liquidity providers, but structure these rewards with delayed vesting to prevent immediate selling:

- **esGMX (GMX Protocol):** Stakers of GMX receive esGMX (escrowed GMX) as rewards. esGMX is not freely transferable and cannot be immediately sold. It converts to regular GMX after a 12-month vesting period, at which point it enters circulating supply. During the vesting period, esGMX is excluded from CMC's circulating supply.
- **Synthetix (SNX):** The protocol has historically used inflationary SNX rewards for stakers, with staking rewards vesting over a 12-month escrow period.

### The Signal Distortion

The inflation was effectively "priced in" at the time the rewards were earned. Market participants who hold GMX understand that their staking rewards will vest into tradeable GMX in approximately 12 months. This is public information; sophisticated holders have already adjusted their valuation models to account for future vesting supply.

When the 12-month vesting period completes and esGMX converts to freely circulating GMX, CMC records the supply increase. The `supply_inf_13w` signal spikes. The model sees "supply inflation → SHORT."

**But the market already knew this was coming.** The inflationary pressure was priced in over the 12-month vesting period, not at the moment of conversion. The model is reacting to a supply event whose price impact was already fully absorbed a year earlier.

### How the Model Reacts

The model SHORTs the token at the exact moment the vesting conversion completes — which is precisely when the previously-known dilution pressure is finally resolved. If there is any residual selling pressure, it is from smaller holders who weren't tracking the vesting schedule closely. The model fires on the lagged recording of a pre-disclosed event, long after informed participants have already positioned around it.

**The information timing problem is severe:** The on-chain vesting conversion is visible in real-time. Any participant monitoring Dune Analytics or Glassnode exchange flows can see esGMX-to-GMX conversions happening. By the time CMC's weekly snapshot reflects the supply change, sophisticated participants have already extracted the information advantage.

---

## 9. Cross-Chain Native Token Counting (L1s)

### Mechanism

For proof-of-stake networks, a significant fraction of native token supply is staked with validators. Whether staked tokens are counted as "circulating" varies by chain and by CMC's reporting methodology:

- **Cosmos (ATOM):** CMC excludes staked ATOM from reported circulating supply. The staking ratio for ATOM has historically ranged from 60-70% of total supply.
- **Polkadot (DOT):** CMC excludes staked DOT. Staking ratio approximately 50-60%.
- **Kusama (KSM):** Similar treatment.

When the staking ratio changes — which happens continuously as validators bond and unbond — the reported circulating supply changes even though no new tokens were issued and no existing tokens were destroyed.

### The 2023 Cosmos Restaking Dynamic

During the Cosmos restaking craze of 2023, significant ATOM was unstaked from traditional staking as holders moved to liquid staking protocols (stATOM, qATOM) or to restaking protocols. When large validators unbonded, the 21-day unbonding period ended, and the newly liquid ATOM appeared in "circulating" (non-staked) wallets.

CMC's circulating supply for ATOM increased not because new tokens were issued but because previously-staked tokens were now in liquid wallets. The increase could be substantial: if 5% of the staked supply unbonded in a quarter, and staked supply represents 65% of total supply, circulating supply increases by approximately 3.25% in that quarter — enough to rank ATOM higher on the supply inflation cross-section.

### How the Model Reacts

**Unstaking wave (apparent supply inflation):**
- CMC records increased circulating supply
- `supply_inf_13w` rises
- Model sees apparent "inflation" → **SHORT signal**
- Reality: no new tokens were issued; the same tokens moved from one wallet type to another

**Restaking wave (apparent supply deflation):**
- Tokens move into staking contracts (excluded from circulating)
- CMC records decreased circulating supply
- Model sees apparent "deflation" → **LONG signal**
- Reality: no tokens were destroyed; the same tokens moved from circulating wallets into staking contracts

The model is responding to changes in network participation dynamics — fundamentally a different signal than supply dilution. An unstaking wave might indicate validator bearishness on the protocol (a weak negative signal at best), but it is categorically different from new token issuance.

---

## 10. The Winsorization Partial Defense

### What Winsorization Does

Before computing cross-sectional supply inflation ranks, the strategy winsorizes `supply_inf_13w` at the 2nd and 98th percentiles each period:

```python
lo, hi = supply_inf.quantile([0.02, 0.98])
supply_inf_winsorised = supply_inf.clip(lo, hi)
```

This removes the most extreme outliers — the 10,000% spikes from token migrations, the -99% apparent crashes from supply corrections. Any token with a 13-week supply inflation figure above the 98th cross-sectional percentile is clipped to the 98th percentile value.

### What Winsorization Does NOT Do

**1. It does not remove moderate artifacts.**

The winsorization boundary is cross-sectional and time-varying. In a period with many extreme events, the 98th percentile might be set at 500%, clipping tokens with 1,000%+ spikes. In a quiet period, the 98th percentile might be set at 50%, meaning a token with 60% apparent supply inflation from a bridge event is clipped to 50% — still ranking at the 98th percentile of the cross-section, still entering the SHORT basket.

Artifacts producing 10-50% apparent supply changes from bridge events, custody reclassifications, staking ratio changes, and emission pauses populate the middle of the distribution and are completely unaffected by 2-98% winsorization. These tokens still rank high or low in the cross-section based on data artifacts.

**2. It does not identify which extreme values are artifacts vs genuine.**

A token with genuine 90% supply inflation from real vesting unlocks and a token with 90% apparent supply inflation from a bridge double-counting event are treated identically after winsorization. Both rank at the 95th percentile. Both enter the SHORT basket. The model has no mechanism to distinguish the signal-carrying event from the noise event.

**3. It is documented to fail against the short leg specifically.**

The alternative methodology sweep (17 configurations) tested removing winsorization entirely. The result: catastrophically worse performance (1,920% volatility, -100% max drawdown), because unclipped extreme positive returns from tokens that 10x destroy the short leg far more than clipped crashes would have helped it. Winsorization is a necessary but not sufficient defense — it protects against the worst extreme data artifacts while leaving the model exposed to hundreds of moderate-severity artifacts across the full basket.

**4. Almost all basket tokens have significant data quality issues.**

Analysis across the full dataset shows that more than 160 tokens in the basket universe exhibit 150 or more data quality events — defined as week-over-week supply changes exceeding 100% upward or 50% downward. Additionally, apparent "deflation" of -95% to -100% in long-term supply using trailing median comparisons appears for LINK, VET, TON, LTC, and AVAX — not because these tokens genuinely experienced near-total supply destruction but because CMC data noise creates extreme median-based metrics. These are signal-polluting artifacts that survive even robust winsorization approaches.

---

## 11. Summary Table: Manipulation Type → Signal Direction → Model Reaction

| Manipulation / Artifact Type | Apparent Effect on Circulating Supply | Signal Direction | Is Signal Correct? | Frequency in Data | Severity |
|------------------------------|--------------------------------------|-----------------|-------------------|-------------------|----------|
| Bridge double-counting (open) | Large spike (+20% to +200%) | SHORT | No — bridge accounting artifact | Very High (hundreds of tokens) | High |
| Bridge correction (exclusion update) | Large drop (-20% to -80%) | LONG | No — same artifact reversing | Very High | High |
| Emission pause (short-term) | Flat at zero (0% for 13w) | LONG | Partially — no current dilution but gamed | High — 7 tokens exceed 109 weeks flat | Medium |
| Emission pause gaming (pre-eval) | Manufactured zero inflation | LONG | No — pre-emptive pause, will resume | Unknown — undetectable from CMC data | High |
| Token split / redenomination | Apparent inflation (+100% to +1,000%) | SHORT | No — no economic dilution | Low-Medium | High |
| Token merge / consolidation | Apparent deflation (-50% to -99%) | LONG | No — no genuine anti-dilution | Low-Medium | High |
| Token migration (old → new) | -100% on old symbol, spike on new | SHORT on new | No — accounting artifact | Low | High |
| Exchange custody reclassification | Spike or drop depending on direction | SHORT or LONG | No — internal bookkeeping | Medium | Medium |
| Fake burn / treasury lock | Apparent deflation (-5% to -30%) | LONG | No — team controls "burned" tokens | Medium (common marketing tactic) | High |
| Genuine buyback and burn | Deflation | LONG | Yes — irreversible supply reduction | Medium | Low (signal is correct) |
| Airdrop claim window opening | Large one-time spike (+30% to +80%) | SHORT | Partially correct but wrong timing | Medium (all recent TGE tokens) | Medium |
| esGMX / vesting reward conversion | Moderate spike (+5% to +20%) | SHORT | No — already priced into valuations | Medium (multiple DeFi protocols) | Medium |
| Staking/unstaking ratio change | Spike (unstaking) or drop (staking) | SHORT or LONG | No — network participation, not issuance | High (all PoS L1s) | Medium |
| ve-token locking (e.g., veCRV, veBAL) | Supply drop when tokens locked | LONG | No — tokens locked but not burned, still outstanding | High (all ve-model protocols) | Medium |
| Derived supply noise (market cap / price error) | Random noise ±1-5% per week | Both | No | Universal — BTC has 178 error periods | Low per event, High cumulatively |
| CMC reporting latency | Delayed spike registration | Both (lagged) | Signal correct but stale | Universal | Medium |
| Supply history < 26 weeks (new token) | Abnormal early supply mechanics | Both | No | High for newly-listed tokens | Medium |

---

## 12. Recommended Mitigations

The following mitigations are ordered by impact-to-implementation-effort ratio. The first two are foundational; the remainder are incremental improvements.

---

### Priority 1: Week-over-Week Supply Stability Filter

**What:** Before computing the 13-week supply inflation signal, exclude any token-period where the single-week supply change exceeds ±50% absolute. Apply at the snapshot level, not at the signal level.

**Implementation:**

```python
# Compute WoW supply change
df['supply_wow'] = df.groupby('symbol')['circulating_supply'].pct_change(1)

# Flag problematic periods
df['supply_data_ok'] = df['supply_wow'].between(-0.50, 1.00)

# Set supply to NaN for flagged periods (not the token, just that period's supply read)
df.loc[~df['supply_data_ok'], 'circulating_supply'] = np.nan

# Re-derive supply_inf_13w from cleaned supply
df['supply_inf_13w'] = df.groupby('symbol')['circulating_supply'].pct_change(13)
```

**Why this is the single highest-impact fix:**

The data analysis shows that 160+ basket tokens have 150+ supply spikes exceeding 100% WoW or drops exceeding 50% WoW. These extreme events are not genuine supply dynamics. By zeroing out the poisoned supply reads before computing the 13-week signal, we remove the contribution of bridge events, token migrations, and custody reclassifications from the trailing window. A token with one poisoned week in a 13-week window has 12 weeks of valid data instead of zero; a token with three consecutive poisoned weeks (a common bridge event pattern) gets its 13-week signal derived from the pre- and post-event reads.

**Threshold justification:** A 50% WoW increase in circulating supply for any token in the top 200 by market cap has no plausible genuine cause. Even the most aggressive real emission schedule — full unlocking of a 2-year vesting cliff in one week — would represent at most 20-30% of total supply for tokens with significant circulating float at listing. A 100% WoW increase is definitionally impossible for any token that had meaningful circulating supply before the period. Any such observation is a data artifact.

**Note:** This filter will NaN some genuine airdrop claim events. This is acceptable — the strategy already handles this by excluding tokens with fewer than 26 weeks of supply history, which filters most newly-TGE tokens before their airdrop windows open.

---

### Priority 2: Emission Pause Handling

**What:** Identify tokens where `supply_inf_13w = 0.0` exactly for multiple consecutive periods. Treat these differently rather than letting them rank as low-inflation longs by default.

**Implementation options (in order of conservatism):**

**Option A — Exclusion:** Exclude any token from the universe where `supply_inf_13w = 0.0` exactly for 8 or more consecutive weeks. Exact zero is not a rounding artifact; it means the supply series has not moved. These tokens are either genuinely not emitting (fine, include them) or they have a stale CMC data feed (problematic). The 8-week threshold distinguishes a brief pause from a structural data stale.

**Option B — Use a longer signal window for paused tokens:** If a token's 13-week supply inflation is zero but its 52-week supply inflation is positive, flag it. Use a composite signal that down-weights the 13-week rank for this token. The 52-week window is less gameable because pausing emissions for 52 weeks requires a full year of suppressed activity.

**Option C — Flat supply detection with minimum activity requirement:** Require that a token's supply series shows at least 1 non-zero week-over-week change in the trailing 13 weeks to be eligible for the lowest supply inflation rank tier. If all 13 readings are identical, the token is moved to a "data uncertain" category and excluded from both baskets.

**The gaming window implication:** This mitigation directly addresses the 13-week gaming window described in Section 3. A team that pauses emissions for exactly 13 weeks to suppress the inflation signal cannot game Option C (the pause itself triggers the exclusion) or Option B (the 52-week signal correctly reflects the pre-pause emission rate).

---

### Priority 3: Known ve-Token List with On-Chain Locked Supply

**What:** Maintain an explicit list of tokens that use vote-escrow locking mechanics (veCRV, veBAL, veVELO, vlCVX, and all their derivatives). For these tokens, source the unlocked circulating supply separately rather than relying on CMC's total reported circulating supply.

**Rationale:** In the ve-token model, users lock their tokens for periods of 1 week to 4 years to receive governance voting power and protocol revenue. Locked tokens cannot be sold. However, many ve-token implementations still count locked tokens as "circulating" in CMC reporting (because they are not burned — they will be returned to the holder when the lock expires). This means apparent supply changes in ve-tokens track changes in locking activity, not genuine issuance.

**Data source:** DefiLlama provides protocol-level TVL data that can be used to estimate locked supply for most major ve-token protocols. For CRV specifically, Dune Analytics has dashboards tracking total veCRV and the time distribution of lock expiries.

**Implementation priority:** ve-tokens appear frequently in the low-inflation long basket because locking activity can temporarily suppress apparent circulating supply. Without correcting for this, the model is longing tokens that may be about to see their locked supply expire (the ultimate reverse of the lock: locked supply returns to circulating, true inflation resumes).

---

### Priority 4: Total Supply Cap Check

**What:** At each snapshot, verify that `circulating_supply <= total_supply * 1.05`. If the reported circulating supply exceeds 105% of total supply, mark that period as a data error and exclude it from signal computation for that token in that period.

**Implementation:**

```python
# CMC provides total_supply in the raw data
df['supply_cap_ok'] = (
    df['circulating_supply'] <= df['total_supply'] * 1.05
)

# Mark data errors
df.loc[~df['supply_cap_ok'], 'circulating_supply'] = np.nan
```

**Evidence from the dataset:** This check catches documented CMC data errors definitively. BTC shows 178 periods where derived circulating supply exceeds 21M (the immutable hard cap). ETH shows 152 such periods, BNB shows 145 such periods. These are periods where the `market_cap / price` derivation produces a mathematically impossible number. The 5% tolerance accommodates minor rounding and reporting delays without allowing grossly erroneous data to propagate.

**Note on the limit:** For tokens without a hard-capped total supply (e.g., ETH post-merge with its dynamic issuance/burn), the "total_supply" figure on CMC may itself be inconsistently reported. Apply this check only to tokens where CMC reports a specific finite total supply figure that the project confirms as a hard cap.

---

### Priority 5: Multi-Period Signal Smoothing

**What:** Instead of using a single point-in-time supply read to compute `supply_inf_13w`, compute the signal using the median of the trailing 4 weekly supply readings as the "current" supply value.

**Implementation:**

```python
# Compute rolling 4-week median of circulating supply
df['supply_smooth'] = df.groupby('symbol')['circulating_supply'].transform(
    lambda s: s.rolling(4, min_periods=2).median()
)

# Use smoothed supply for signal computation
df['supply_inf_13w_smooth'] = (
    df.groupby('symbol')['supply_smooth'].pct_change(13)
)
```

**Rationale:** A single-period spike in the supply series (bridge event, custody reclassification, one-week data error) will be damped by the 4-period median. A genuine sustained supply increase will persist across 4 weeks and survive the median computation. This trades some signal responsiveness (genuine rapid dilution events are detected with a 2-week lag) for substantially better noise rejection (single-period artifacts are suppressed by approximately 75%).

**Limitation:** This mitigation does not help with sustained artifacts like emission pauses (which produce sustained flat readings) or token migrations (where the new supply level persists across multiple weeks). It specifically targets single-period and short-burst artifacts. Combined with Priority 1 (WoW filter) and Priority 4 (cap check), multi-period smoothing provides a third layer of defense against point-in-time data errors.

---

### Implementation Priority Summary

| Priority | Mitigation | Artifacts Addressed | Implementation Effort | Expected Impact |
|----------|-----------|---------------------|----------------------|-----------------|
| 1 | WoW ±50% stability filter | Bridge events, token migrations, custody reclassifications, derived supply noise | Low (one `pct_change` computation, one mask) | **Highest** — removes the majority of extreme artifacts pre-signal |
| 2 | Emission pause detection and handling | Emission gaming, stale CMC data feeds | Low-Medium (pattern detection on supply series) | **High** — directly closes the 13-week gaming window |
| 3 | ve-token locked supply sourcing | ve-token locking mechanics, apparent deflation artifacts | Medium (external data integration required) | **Medium** — affects a specific but growing token class |
| 4 | Total supply cap check | Derived supply mathematical impossibilities, CMC data errors for BTC/ETH/BNB | Low (one comparison per row) | **Medium** — definitive error detection for capped-supply tokens |
| 5 | 4-week median smoothing | Single-period bridge spikes, one-week custody moves | Low (rolling median) | **Medium** — complements Priority 1 for sustained short-duration artifacts |

---

### What These Mitigations Do Not Solve

Even with all five mitigations implemented, the following categories of supply data corruption remain unaddressable without switching to on-chain data sources:

1. **Fake burns / treasury locks:** Indistinguishable from genuine burns in CMC data without on-chain wallet analysis. Requires Nansen entity labeling or Dune Analytics wallet classification.

2. **Scheduled airdrop timing (partially):** The WoW filter catches the most extreme single-week spikes, but correctly structured airdrops with pre-disclosed vesting might not exceed the 50% threshold. Messari unlock calendar integration is required to pre-screen for known claim window events.

3. **CMC reporting latency:** The fundamental lag between on-chain events and CMC weekly snapshots cannot be corrected within the current data architecture. This requires daily or sub-daily on-chain supply data (Glassnode primary source, Dune Analytics alternative).

4. **Cross-chain staking ratio changes for PoS L1s:** ATOM, DOT, KSM staking-ratio-driven apparent supply changes cannot be filtered by WoW thresholds alone if the changes are gradual (e.g., 5% of staked supply unbonding over a 13-week period appears as 3% cumulative apparent inflation, below any practical threshold). Requires chain-specific staking data overlays.

The single highest-leverage upgrade for live deployment remains the one noted in the strategy methodology documents: **replace CMC-derived circulating supply with Glassnode on-chain circulating supply**. Glassnode independently sources supply from chain state, not from project self-reporting. This eliminates categories 1 (genuine burns are verified on-chain), 3 (latency drops from 7 days to hours), and partially addresses category 4 (staking data is available per-chain for major L1s).

---

*Document generated: 2026-03-03. Based on analysis of 135,652 rows across 2,267 symbols, 477 weekly snapshots, CoinMarketCap data 2017-01-01 to 2026-02-22.*
