# Phase 4A.6 Zero-Cost Budget Analysis

## Executive Summary

**RESULT: BUDGET EXCEEDED — Phase 4B discovery cannot proceed on Helius free tier alone**

**Utilization: 152% of free tier credits (estimated 50,650 vs 33,333 credits/day)**

**Safety Ceiling (70%): 23,333 credits/day — EXCEEDED by 117%**

---

## Helius Free Tier (Verified 2025-09-10)

| Resource | Limit |
|----------|-------|
| Monthly Credits | 1,000,000 |
| Daily Credits (30-day) | 33,333 |
| RPC Rate Limit | 10 req/s |
| Standard WSS | Included (metered: 2 credits/0.1 MB) |
| Concurrent WS Connections | 5 |

---

## Estimated Daily Discovery Load

### WebSocket Streaming (Primary Cost)
| Stream | Data Rate | Daily Volume | Credits (2/0.1 MB) |
|--------|-----------|--------------|-------------------|
| `logsSubscribe` (Pump.fun program) | ~2.5 MB/day | 2.5 MB | **50,000** |

**This single stream exceeds the entire daily budget.**

### RPC Calls (Secondary)
| Operation | Frequency | Credits/Call | Daily Credits |
|-----------|-----------|--------------|---------------|
| `getSignaturesForAddress` (discovery verification) | 100/day | 1 | 100 |
| `getTransaction` (decode create/buy/sell) | 500/day | 1 | 500 |
| `getTokenAccountsByOwner` (holder analysis) | 50/day | 1 | 50 |
| `getMultipleAccounts` (batch reads) | 20/day × 5 accts | 1/acct | 100 |

**RPC Subtotal: 750 credits/day (2.2% of budget)**

### External APIs (Zero Credits)
| Provider | Cost |
|----------|------|
| Jupiter Quote/Price | Free (rate limited, not credit metered) |
| PumpPortal Price/WS | Free (data) / Metered (trade streams) |

---

## Total Daily Estimate

| Component | Credits/Day | % of Budget |
|-----------|-------------|-------------|
| Helius WSS (logsSubscribe) | 50,000 | 150% |
| Helius RPC | 750 | 2.2% |
| **TOTAL** | **50,750** | **152%** |

---

## Safety Ceiling Analysis

| Threshold | Credits/Day | Status |
|-----------|-------------|--------|
| 100% (full budget) | 33,333 | EXCEEDED |
| 70% (safety ceiling) | 23,333 | EXCEEDED by 117% |
| 50% (comfortable) | 16,667 | EXCEEDED by 204% |

---

## Mitigation Options

### Option 1: Reduce WebSocket Bandwidth (Target: <23,333 credits)
**Required reduction: 54%**

Strategies:
1. **Filter at source** — Use `logsSubscribe` with exact program ID + instruction discriminator filter
2. **Sample instead of stream** — Poll `getSignaturesForAddress` every 30s instead of continuous WS
3. **Hybrid** — WS for new token detection only (first 5 min), then poll
4. **Compress** — Helius credits are per 0.1 MB uncompressed; enable compression if supported

**Feasibility:** Polling loses real-time advantage; filtering may miss events.

### Option 2: Multi-Provider Free Tier Pool
| Provider | Free Tier | Credits/Day Equivalent |
|----------|-----------|------------------------|
| Helius | 1M credits/mo | 33,333 |
| QuickNode | 10M req/mo | 333,333 req (but no WS on free) |
| Alchemy | 300M CU/mo | ~10M CU/day (different metric) |
| Solana Public RPC | Unlimited rate-limited | No WS, no guarantees |
| Triton | 1M req/mo | 33,333 |
| GenesysGo | 10M req/mo | 333,333 |

**Pool Strategy:** Use Helius WS for new token detection only (~5 MB/day = 100k credits), QuickNode/Alchemy for RPC batching.

**Risk:** Multiple free tiers = operational complexity, no SLAs.

### Option 3: Accept Paid Tier (Recommended for Phase 4B)
| Tier | Cost | Credits/Month | Daily Budget | Headroom |
|------|------|---------------|--------------|----------|
| Helius Developer | $49/mo | 10M | 333,333 | 6.5x |
| Helius Business | $499/mo | 100M | 3.3M | 65x |

**$49/mo Developer tier provides 10x headroom for discovery + enrichment.**

### Option 4: Self-Hosted Indexer (Geyser/Yellowstone)
- Requires infrastructure ($50-200/mo for VPS)
- Unlimited historical + real-time
- Operational burden

---

## PumpPortal Cost Impact

| Stream | Classification | Daily Cost (est.) |
|--------|---------------|-------------------|
| `subscribeNewToken` | FREE | 0 |
| `subscribeMigration` | FREE | 0 |
| `subscribeTokenTrade` | METERED_CRYPTO | 0.01 SOL per 10k events |
| `subscribeAccountTrade` | METERED_CRYPTO | 0.01 SOL per 10k events |

**For paper trading:** Only FREE streams needed. Trade streams only for live execution.

---

## Jupiter Cost Impact

| Endpoint | Free Tier | Paper Trading Cost |
|----------|-----------|-------------------|
| `/swap/v1/quote` | 100 req/s | 0 |
| `/price/v3` | 100 req/s | 0 |
| `/tokens/all` | Unlimited | 0 |

**Zero cost for paper trading.**

---

## Phase 4B Budget Recommendation

### Minimum Viable Budget (Paper-Only)
| Item | Monthly Cost | Notes |
|------|--------------|-------|
| Helius Developer | $49 | 10M credits, 50 req/s, enhanced WS |
| **Total** | **$49/mo** | **Fits "low-cost" not "zero-cost"** |

### With Safety Margin
| Item | Monthly Cost | Notes |
|------|--------------|-------|
| Helius Developer | $49 | Primary RPC/WS |
| QuickNode Free | $0 | Backup RPC |
| Alchemy Free | $0 | Backup RPC |
| **Total** | **$49/mo** | **Redundancy included** |

---

## Conclusion

**Zero-cost ($0/month) discovery is NOT VIABLE for continuous Pump.fun monitoring** due to Helius WebSocket metering (2 credits/0.1 MB).

**Required decision for Phase 4B:**
1. **Accept $49/mo Helius Developer** — Recommended, unblocks discovery
2. **Implement aggressive filtering/polling** — Complex, loses real-time, may still exceed
3. **Self-host Geyser** — Higher infra cost, more control

**Without budget approval, Phase 4B discovery scope must be reduced to polling-only (no WS), significantly limiting alpha capture.**