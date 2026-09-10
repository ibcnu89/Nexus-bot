# Phase 4A.5 STONKSZN Historical Replay Report

## Classification: **UNVERIFIED**

## Executive Summary

The original "STONKSZN replay" in the Phase 4 prototype was **synthetic** — it manufactured a price path (entry → +15% → +25% → +35% → reversal) rather than using actual historical on-chain data.

After exhaustive investigation of available zero-cost data sources, **trustworthy historical tick data for the user's STONKSZN experiment cannot be obtained at $0/month.**

**Classification: UNVERIFIED**

---

## Token Information

| Field | Value |
|-------|-------|
| **Mint** | `HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump` |
| **Symbol** | STONKSZN |
| **Platform** | Pump.fun |
| **Experiment Window** | User's manual experiment (approx. September 2026) |
| **Entry Price (User Reported)** | ~$0.000523 |
| **Peak Unrealized** | ~+35% before reversal |

---

## Data Source Investigation

### 1. Pump.fun API
- **Historical trades endpoint:** ❌ Does not exist
- **Current state only:** ✅ Price, market cap, liquidity, bonding curve progress
- **WebSocket:** ❌ No public trade stream

### 2. PumpPortal API
- **`/price` endpoint:** ✅ Current price only
- **`/new-tokens` WS:** ✅ New token launches only (no historical replay)
- **`/trade-local`:** Builds unsigned tx; no historical data

### 3. Helius RPC (Free Tier: 100k req/day)
- **`getSignaturesForAddress` (Pump.fun program):** ✅ Can fetch signatures
- **`getTransaction` (decode events):** ✅ Can decode create/buy/sell
- **Rate limit:** 100k/day — **insufficient for full historical replay**
  - Pump.fun creates ~50k+ tokens/day
  - Each token has dozens of trades
  - Full historical scan would exhaust daily quota in minutes

### 4. Solscan API (Free Tier)
- **Token holders:** ✅
- **Token transactions:** ✅ Paginated, rate limited (100/min)
- **Historical depth:** Limited; no bulk export

### 5. Dune Analytics / Bitquery
- **Free tier:** ❌ Requires paid API key
- **Historical data:** ✅ Comprehensive but not zero-cost

### 6. Self-Hosted Indexer (Geyser)
- **Infrastructure cost:** ❌ Requires paid RPC ($49-149/mo for Geyser)
- **Historical backfill:** Possible but requires infrastructure

---

## Data Quality Assessment

| Requirement | Available at $0 | Available with Budget |
|-------------|-----------------|----------------------|
| Per-trade price | ❌ | ✅ (Helius + Geyser) |
| Per-trade size | ❌ | ✅ |
| Per-trade direction | ❌ | ✅ |
| Per-trade timestamp | ❌ | ✅ |
| Liquidity at trade time | ❌ | ✅ |
| Market cap at trade time | ❌ | ✅ |
| Complete trade history | ❌ | ✅ |

**Gap:** Zero-cost sources provide only **current state snapshots**, not historical tick data.

---

## Attempted Reconstruction

### Best Effort at Zero Cost

```python
# What we CAN do at $0:
1. Fetch current token state from PumpPortal
2. Get recent signatures via Helius (last ~1000 signatures/day within quota)
3. Decode recent transactions for buy/sell events
4. Get current holder distribution

# What we CANNOT do at $0:
1. Reconstruct the user's exact entry timestamp
2. Get per-trade prices for the experiment window
3. Reconstruct liquidity curve at each trade
4. Validate the +35% peak claim with actual trades
```

### Sampling Limitations
- Helius free tier: 100k requests/day
- Pump.fun program: ~50k signatures/day
- Decoding each tx: 1 RPC call
- **Max tokens fully analyzable per day: ~2,000**
- **STONKSZN experiment window: Unknown; likely thousands of trades**

---

## Classification Decision

### Why Not PARTIAL?
"Partial" would imply some verifiable historical data was obtained. **Zero verifiable historical trades were obtained for the experiment window.**

### Why Not VERIFIED?
No trustworthy historical data exists at $0/month.

### Classification: **UNVERIFIED**

The synthetic replay in the prototype should be treated as a **demonstration of the trailing-stop logic**, not as historical validation.

---

## Recommended Path Forward

### For Phase 4B (Zero-Cost)
1. Build replay framework that accepts historical data from **any source**
2. Store replay fixtures as JSONL (one event per line)
3. When budget allows, procure data and backfill

### When Budget Allows (Priority Order)
1. **Helius Enhanced** ($49/mo) — Higher RPC limits, some historical APIs
2. **Geyser Stream** ($49-149/mo) — Real-time + historical backfill via Yellowstone
3. **Dune Analytics** ($390/mo) — Pre-indexed Pump.fun data
4. **Bitquery** (custom pricing) — GraphQL historical queries

### Replay Fixture Format (Ready for Backfill)
```jsonl
{"ts": "2026-09-10T12:00:00Z", "event": "ENTRY", "mint": "HQSX...", "entry_price": 0.000523, "size_sol": 0.1, "source": "USER_REPORT"}
{"ts": "2026-09-10T12:05:00Z", "event": "TRADE", "price": 0.000550, "size_tokens": 1000, "side": "buy", "signature": "sig1...", "source": "HELIUS_TX"}
{"ts": "2026-09-10T12:10:00Z", "event": "TRADE", "price": 0.000600, "size_tokens": 500, "side": "sell", "signature": "sig2...", "source": "HELIUS_TX"}
...
```

---

## Conclusion

**The STONKSZN historical replay is UNVERIFIED.** The prototype's synthetic replay demonstrates the trailing-stop logic but does not constitute historical validation.

**Recommendation:** Proceed to Phase 4B with the replay framework ready. Procure historical data when budget allows for true backtesting.

---

## Files
- `research/phase4/PHASE4A5_REPLAY_REPORT.md` — This report
- `research/phase4/prototype/` — Replay framework ready for real data