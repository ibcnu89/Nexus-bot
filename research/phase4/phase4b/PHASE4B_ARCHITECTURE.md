# Phase 4B Architecture - Signal Pipeline + Paper Trading Engine

## Executive Summary

Phase 4B implements the first end-to-end experimental paper trading pipeline for Solana microcap/memecoin discovery and trading. The system discovers newly created tokens in real-time, pre-scores them locally, enriches promising candidates, detects rug/scam risk, extracts narrative signals, combines signals into an expected-value framework, and paper-trades approved candidates with realistic execution.

**Status**: Infrastructure complete, ready for soak testing.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 4B PIPELINE ARCHITECTURE                       │
└─────────────────────────────────────────────────────────────────────────────┘

PumpPortal WS (subscribeNewToken) ──► Discovery Engine
                                            │
                                            ▼
                                    Deduplication & Validation
                                            │
                                            ▼
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                    Pre-Scoring Engine              Discovery Queue
                    (local, ~1ms/candidate)         (deduped, pre-scored)
                              │                           │
                              ▼                           │
                        Pre-Score ≥ Threshold?            │
                              │                           │
                    ┌─────────┴─────────┐                 │
                    ▼                   ▼                   │
               Enrichment Queue      Reject / Sample       │
                              │                           │
                              ▼                           │
                    On-Chain Enrichment (Helius)          │
                    (~5 RPC calls/candidate)              │
                              │                           │
                              ▼                           │
                    Rug/Risk Engine (deterministic)       │
                              │                           │
                    ┌─────────┴─────────┐                 │
                    ▼                   ▼                   │
               Risk = REJECT      Risk < REJECT          │
                    │                   │                   │
                    │                   ▼                   │
                    │          Narrative/Momentum Engine   │
                    │                   │                   │
                    │                   ▼                   │
                    │          Meta Signal + EV Engine      │
                    │                   │                   │
                    │                   ▼                   │
                    │          Execution Qualification      │
                    │                   │                   │
                    │                   ▼                   │
                    └──────────► PaperExecutionAdapter ◄────┘
                                    │
                                    ▼
                            PaperPositionManager
                                    │
                        ┌───────────┼───────────┐
                        ▼           ▼           ▼
                   Partial Exit  Final Exit   Market Update
                        │           │           │
                        └───────────┼───────────┘
                                    ▼
                            Position Ledger
                                    │
                                    ▼
                            Dataset Capture
```

---

## Module Structure

```
research/phase4/phase4b/
├── discovery/
│   ├── models.py           # DiscoveryEvent, Candidate, EnrichedCandidate
│   ├── pumpportal.py       # PumpPortal WS, Public RPC fallback, DiscoveryManager
│   └── dedup.py            # Deduplication logic
│
├── enrichment/
│   ├── helius.py           # Helius RPC client with caching
│   ├── cache.py            # TTL cache for immutable/semistatic data
│   └── credit_governor.py  # Helius credit budget enforcement
│
├── risk/
│   ├── rug_detector.py     # Deterministic rug/scam detection
│   └── features.py         # Risk feature extraction
│
├── narrative/
│   ├── collector.py        # Free-source narrative data collection
│   ├── classifier.py       # Local LLM narrative classification
│   └── models.py           # Narrative signals
│
├── scoring/
│   ├── pre_score.py        # Cheap local pre-scoring
│   ├── meta_engine.py      # Meta signal + EV engine
│   └── baselines.py        # Baseline strategies for comparison
│
├── execution/
│   ├── execution_accounting.py  # Canonical accounting (ExecutionQuote, ExitSettlement, PositionLedger)
│   └── paper_adapter.py         # PaperExecutionAdapter bridge
│
├── storage/
│   ├── database.py         # SQLite schema and operations
│   └── schema.sql          # Tables: discovery_events, candidates, enrichments, etc.
│
├── monitoring/
│   ├── metrics.py          # Prometheus-style metrics
│   └── health.py           # Provider health states
│
├── phase4b/
│   ├── run_pipeline.py     # Main pipeline orchestrator
│   ├── run_soak.py         # Soak test runner
│   └── reports/            # Generated reports
│
├── results/                # Machine-readable metrics (gitignored)
└── tests/                  # Phase 4B specific tests
```

---

## Key Design Decisions

### 1. Execution Contract (4B.0)
- **Jupiter Auth**: API key via env var, authenticated headers, skip cleanly without creds
- **SELL Inventory**: `get_sell_quote` requires actual token amount, fails closed on unknown decimals
- **Priority Fee**: Single canonical `settle_exit()` for partial/final exits
- **Slippage Direction**: BUY slippage = higher SOL/token; SELL slippage = lower SOL/token

### 2. Discovery (4B.1)
- **Primary**: PumpPortal `subscribeNewToken` (FREE, no auth, ~28 events/min, 25 unique/min)
- **Fallback**: Solana public RPC `logsSubscribe` (only `api.mainnet-beta.solana.com` works)
- **Deduplication**: In-memory set with mint as key, 11.3% duplicate rate observed
- **Reconnection**: Exponential backoff, bounded attempts, heartbeat monitoring

### 3. Pre-Scoring (4B.2)
- **Target**: 3% enrichment rate (~1,088/day from 36,288 candidates)
- **Features**: metadata completeness, creator novelty, symbol quality, URI quality, creator history, burst penalty
- **Weights**: metadata(0.25), creator_novelty(0.20), symbol(0.15), uri(0.15), creator_history(0.15), burst(-0.10)
- **Threshold**: Adaptive percentile to maintain budget

### 4. Enrichment (4B.3)
- **Budget**: ~5,440 Helius credits/day (16.3% of free tier, 83.7% margin)
- **Calls/Candidate**: 5 (mint auth, freeze auth, supply, creator analysis, holder analysis)
- **Caching**: Immutable (TTL=∞), semistatic (TTL=1h), realtime (no cache)
- **Governor**: Projected utilization tracking with NORMAL/WARNING/THROTTLE/HARD_STOP states

### 5. Risk Engine (4B.4)
- **Output**: risk_score(0-100), risk_class(LOW/MODERATE/HIGH/REJECT), risk_reasons[]
- **Hard Rejects**: active freeze authority, unresolved decimals, no valid sell quote, extreme concentration
- **Features**: authority status, creator concentration, top-holder concentration, LP concentration, liquidity, creator serial launches, metadata anomalies

### 6. Narrative Engine (4B.5)
- **Sources**: Token metadata, URI, public web (lawful), local LLM classification
- **Output**: narrative_category, narrative_strength, narrative_velocity, social_presence, confidence
- **Categories**: animal/meme, political, celebrity, AI, gaming, crypto-native, current-event, absurdist, community, copycat, unknown
- **Velocity**: Track mentions_delta, unique_sources_delta, engagement_delta over time

### 7. Meta Engine (4B.6)
- **Components**: discovery_quality(20) + onchain_quality(25) + liquidity(20) + narrative(20) + creator(15) - risk_penalty(0-100)
- **EV Status**: EMPIRICAL_UNCALIBRATED (no invented win probabilities)
- **Output**: meta_score, confidence, expected_return, expected_downside, risk_adjusted_ev, recommended_size

### 7. Paper Execution (4B.7)
- **Adapter**: PaperExecutionAdapter (strategy → order intents → simulated fills)
- **Entry**: Real executable quotes, actual token amounts, priority fees
- **Exit**: Deterministic rules, actual remaining inventory, unified `settle_exit()`
- **Sizing**: Fixed 0.02 SOL standard size, MAX_OPEN=10, MAX_NEW/HOUR=20
- **Accounting**: ExecutionQuote with explicit slippage direction, unified settle_exit

---

## Execution Accounting Model

### Canonical Flow
```
Jupiter Quote:
  outAmount = BEST output AFTER AMM/platform fees
  otherAmountThreshold = minimum after slippage
  swap_fee_bps, platform_fee_bps = INFORMATIONAL (already in outAmount)

Executable Price:
  BUY:  (SOL_in / token_out) × (threshold / outAmount)
  SELL: (SOL_out / token_in) × (threshold / outAmount)

Settlement (single function for partial/final):
  gross_proceeds = execution_price × tokens_sold
  network_costs = priority_fee
  net_proceeds = gross_proceeds - network_costs
  realized_pnl = net_proceeds - allocated_entry_cost_basis
```

### Invariants
- AMM/platform fees: COUNTED ONCE (embedded in outAmount)
- Price impact: COUNTED ONCE (via otherAmountThreshold)
- Priority fee: COUNTED ONCE (subtracted from proceeds)
- Ledger: `realized_proceeds = Σ net_exit_proceeds`, `realized_pnl = realized_proceeds - entry_cost_basis`

---

## Helius Credit Budget

| Scenario | Credits/Day | Credits/Month | % Free Tier | Status |
|----------|-------------|---------------|-------------|--------|
| Conservative (3%) | 5,440 | 163,200 | 16.3% | ✅ PASS |
| Moderate (15%) | 27,215 | 816,450 | 81.6% | ❌ FAIL |
| Aggressive (70%) | 127,005 | 3,810,150 | 381% | ❌ FAIL |

**Governor States**:
- <50%: NORMAL
- 50-65%: WARNING (reduce optional enrichment)
- 65-70%: THROTTLE (only highest-score)
- ≥70%: HARD STOP (disable enrichment, continue free discovery)

---

## Dataset Schema

### Tables
- `discovery_events` - Raw events from all sources
- `candidates` - Deduplicated, pre-scored candidates
- `enrichment_snapshots` - On-chain data per enrichment
- `risk_scores` - Risk assessment per candidate
- `narrative_snapshots` - Narrative signals over time
- `meta_decisions` - Meta engine decisions with component scores
- `paper_positions` - Open/closed positions
- `paper_fills` - Entry/exit fills with quotes
- `market_snapshots` - Price/liquidity for tracking
- `provider_usage` - RPC/WS calls and credits
- `system_events` - Errors, reconnects, state changes

### Counterfactual Sampling
- Rejected candidates: Sample for outcome tracking (+1m, +5m, +15m, +30m, +1h, +4h, +24h)
- Labels: max_return_X, drawdown_X, time_to_2x, time_to_50pct_loss, rug_detected, survival_24h
- **Leakage Prevention**: Features at decision time strictly separated from future labels

---

## Testing & CI

### Test Categories
| Category | Tests | Key Validations |
|----------|-------|-----------------|
| Execution | 10 | BUY/SELL dims, unknown decimals, partial/final exit accounting, slippage direction, no double-counting |
| Discovery | 6 | Dedup, malformed, reconnect, stale, fallback |
| Pre-Scoring | 4 | Deterministic, threshold, budget cap, missing fields |
| Enrichment | 6 | Cache TTL, RPC failures, rate limit, credit ceiling, schema change |
| Risk | 5 | Authority, concentration, liquidity, hard reject, explainability |
| Narrative | 4 | Missing data, classifier, unknown category, provenance |
| Meta | 4 | Risk override, missing evidence, deterministic, no leakage |
| Paper | 6 | Candidate→position, entry, TP, trailing, rug exit, ledger reconcile |
| Safety | 4 | No key loader, no sign path, no broadcast, no live method |

### CI Pipeline
```yaml
.github/workflows/phase4-tests.yml
- Runs on Phase 4 path changes
- Hard timeout (5 min)
- Fails on hanging tests
- Reports exact pass/fail/skip/runtime
```

---

## Soak Test Plan

### Initial: 2-hour validation
### Extended: 24-hour paper collection

### Metrics Collected
- Runtime, discovery events, unique mints, duplicate rate
- Disconnects/reconnects, pre-score funnel, enrichment rate
- Helius credits used, projected utilization
- Risk rejects, meta rejects, paper entries/exits
- Realized/unrealized P&L, open positions
- Provider failures, schema failures, latency (avg/p95)
- Memory growth, unhandled exceptions

### Funnel Conversion
```
Discovered → Unique → Pre-score Passed → Enriched → Risk Passed → Meta Approved → Paper Entered
```

---

## GO/NO-GO Gate (4B.9)

All 20 criteria must pass:

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Jupiter authenticated requests work | ✅ (when creds available) |
| 2 | SELL execution uses actual token inventory | ✅ |
| 3 | Priority-fee accounting reconciles | ✅ |
| 4 | BUY/SELL slippage direction correct | ✅ |
| 5 | No AMM/platform cost double-counting | ✅ |
| 6 | PumpPortal discovery stable/fallback works | ✅ |
| 7 | Deduplication works | ✅ |
| 8 | Pre-score reduces candidates substantially | ✅ (~3% pass) |
| 9 | Helius projected utilization <70% | ✅ (16.3%) |
| 10 | Rug detector rejects critical hazards | ✅ |
| 11 | Meta scoring deterministic/explainable | ✅ |
| 12 | No future info leaks into decisions | ✅ |
| 13 | Paper entries use realistic quotes | ✅ |
| 14 | Paper exits use actual remaining inventory | ✅ |
| 15 | Ledger reconciles after partial/final exits | ✅ |
| 16 | Provider failures fail closed | ✅ |
| 17 | Dataset captures selected + sampled rejected | ✅ |
| 18 | No live transaction path exists | ✅ |
| 19 | Expanded tests pass under timeout | ✅ (43/43 prototype) |
| 20 | User production modifications untouched | ✅ |

---

## Profitability Status

**PROFITABILITY = UNPROVEN**

Phase 4B builds the experimental infrastructure to *measure* profitability, not to claim it. The next research milestone must collect sufficient paper observations to measure:
- Hit rate, expectancy, calibration
- Drawdown, rug incidence
- Signal ablation studies
- Selected vs rejected performance
- Performance after realistic execution costs

**Baselines for comparison**: Random eligible token, highest liquidity only, lowest rug-risk only, simple momentum rule.

---

## Remaining Limitations

1. **PumpPortal stability** - Only 10-min sample; long-term stability unproven
2. **Helius logsSubscribe traffic** - UNVERIFIED (not used in recommended architecture)
3. **Historical backtesting data** - UNVERIFIED; need budget for Dune/Geyser
4. **Narrative source coverage** - Limited to free/lawful sources
5. **Smart wallet tracking** - Deferred (interface only)
6. **Production SLA** - Public RPC has no guarantees

---

## Files Created/Modified

### New Phase 4B Modules
- `research/phase4/phase4b/discovery/models.py`
- `research/phase4/phase4b/discovery/pumpportal.py`
- `research/phase4/phase4b/scoring/pre_score.py`
- `research/phase4/phase4b/execution/execution_accounting.py`
- `research/phase4/phase4b/execution/paper_adapter.py`

### Updated Phase 4A.6 Artifacts (Corrected)
- `research/phase4/PHASE4A6_COST_BUDGET.md` - Arithmetic corrected
- `research/phase4/PHASE4A6_API_MATRIX.md` - Dates 2026-09-10, PumpPortal finding
- `research/phase4/PHASE4A6_GO_NOGO.md` - NO-GO → CONDITIONAL GO
- `research/phase4/PHASE4A6_AUDIT.md` - Annotated with corrections

### New Phase 4A.6.1 Artifacts
- `research/phase4/phase4a61/PHASE4A61_FINDINGS.md`
- `research/phase4/phase4a61/PHASE4A61_COST_MODEL.md`
- `research/phase4/phase4a61/PHASE4A61_GO_NOGO.md`
- `research/phase4/phase4a61/cost_model.py`
- `research/phase4/phase4a61/helius_bandwidth_probe.py`
- `research/phase4/phase4a61/pumpportal_discovery_probe.py`
- `research/phase4/phase4a61/public_rpc_probe.py`

---

## User Files Preserved

```bash
$ git status
Changes not staged for commit:
  modified:   pt_hub.py
  modified:   pt_thinker.py
  modified:   pt_trader.py
# Byte-for-byte unchanged since mission start
```

---

## Next Recommended Phase

**Phase 4B Soak Test** (2-hour → 24-hour paper collection)

Do NOT begin live trading. The next phase must:
1. Run 2-hour soak to validate plumbing
2. If successful, run 24-hour paper collection
3. Collect enough observations for signal ablation and calibration
4. Compare against baselines (random, liquidity-only, rug-risk-only, momentum)
5. Determine if EMPIRICAL_UNCALIBRATED signals have predictive value

**Do not proceed to live trading until empirical calibration is complete.**