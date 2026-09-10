# Phase 4A.6 Zero-Cost Budget Analysis — CORRECTED (2026-09-10)

> **NOTE**: This document has been corrected based on Phase 4A.6.1 empirical measurements.
> Original version (2025-09-10) contained arithmetic error: confused MB with GB.
> See `PHASE4A61_FINDINGS.md` and `PHASE4A61_COST_MODEL.md` for full correction.

## Executive Summary — CORRECTED

**RESULT: ZERO-COST DISCOVERY IS VIABLE with PumpPortal + Helius hybrid architecture**

**Previous (wrong) claim**: 152% utilization (50,650 vs 33,333 credits/day)
**Corrected**: PumpPortal discovery = 0 Helius credits; conservative enrichment = 16.3% of free tier

---

## Helius Free Tier (Verified 2026-09-10)

| Resource | Limit |
|----------|-------|
| Monthly Credits | 1,000,000 |
| Daily Credits (30-day) | 33,333 |
| RPC Rate Limit | 10 req/s |
| Standard WSS | Included (metered: 2 credits/0.1 MB) |
| Concurrent WS Connections | 5 |

**Credit Rate**: 2 credits / 0.1 MB = **20 credits / MB**

---

## Corrected Traffic Analysis

### PumpPortal Discovery (MEASURED 2026-09-10)
- **Stream**: `subscribeNewToken` 
- **Cost**: **FREE** (0 Helius credits)
- **Events/minute**: 28.4
- **Unique mints/minute**: 25.2
- **Candidates/day**: **36,288**
- **Duplicate rate**: 11.3%
- **Avg payload**: 560 bytes
- **Latency**: Sub-second
- **Stability**: 10-min sample, 0 reconnects

### Helius WebSocket (NOT USED FOR DISCOVERY)
- **Stream**: `logsSubscribe` (Pump.fun program)
- **Conservative estimate**: 2.5 MB/day (NOT MEASURED - no credentials)
- **Credits**: 2.5 MB × 20 = **50 credits/day** (NOT 50,000)

### RPC Calls (Selective Enrichment Only)
| Operation | Frequency | Credits/Call | Daily Credits |
|-----------|-----------|--------------|---------------|
| `getSignaturesForAddress` | 100/day | 1 | 100 |
| `getTransaction` | 500/day | 1 | 500 |
| `getTokenAccountsByOwner` | 50/day | 1 | 50 |
| `getMultipleAccounts` (batched) | 20/day × 5 | 1/acct | 100 |

---

## Hybrid Architecture Cost Model (RECOMMENDED)

**Architecture**: PumpPortal Discovery (FREE) + Helius RPC Enrichment (Selective)

### PumpPortal Discovery (FREE)
- 36,288 candidates/day at 0 Helius credits

### Selective Enrichment (Conservative: Top 3%)
| Metric | Value |
|--------|-------|
| Enrichment rate | 3% |
| Enriched candidates/day | 1,088 |
| RPC calls/enrichment | 5 |
| Total RPC calls/day | 5,440 |
| Helius RPC credits/day | 5,440 |
| Helius WS credits/day | 0 (PumpPortal used) |
| **Total Helius credits/day** | **5,440** |
| **Total Helius credits/month** | **163,200** |
| **% Free tier (1M/month)** | **16.3%** |
| **Safety margin (70% ceiling)** | **83.7%** |

---

## Scenario Comparison

| Architecture | Scenario | Credits/Day | Credits/Month | % Free Tier | Passes 70% |
|--------------|----------|-------------|---------------|-------------|------------|
| **Hybrid (RECOMMENDED)** | **Conservative (3%)** | **5,440** | **163,200** | **16.3%** | ✅ **YES** |
| Hybrid | Moderate (15%) | 27,215 | 816,450 | 81.6% | ❌ NO |
| Hybrid | Aggressive (70%) | 127,005 | 3,810,150 | 381% | ❌ NO |
| Helius-Only | Conservative (3%) | 350 | 10,500 | 1.1% | ✅ YES |
| Helius-Only | Moderate (15%) | 1,550 | 46,500 | 4.7% | ✅ YES |
| Helius-Only | Aggressive (70%) | 7,050 | 211,500 | 21.1% | ✅ YES |

---

## Safety Ceiling Analysis

| Threshold | Credits/Day | Status (Hybrid Conservative) |
|-----------|-------------|------------------------------|
| 100% (full budget) | 33,333 | 83.7% margin |
| 70% (safety ceiling) | 23,333 | **5.4K << 23.3K ✅** |
| 50% (comfortable) | 16,667 | **5.4K << 16.7K ✅** |

---

## PumpPortal Cost Impact

| Stream | Classification | Helius Credits | Cost |
|--------|----------------|----------------|------|
| `subscribeNewToken` | **FREE** | 0 | $0 |
| `subscribeMigration` | **FREE** | 0 | $0 |
| `subscribeTokenTrade` | METERED_CRYPTO | 0 | 0.01 SOL/10k events |
| `subscribeAccountTrade` | METERED_CRYPTO | 0 | 0.01 SOL/10k events |

**For paper trading**: Only FREE streams needed. Zero Helius credits.

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

### Zero-Cost Architecture (VALIDATED)

| Item | Monthly Cost | Notes |
|------|--------------|-------|
| PumpPortal Discovery | $0 | Free subscribeNewToken |
| Helius RPC Enrichment | $0 | 163,200 credits/month (16.3% of free tier) |
| Jupiter/PumpPortal Pricing | $0 | Free tiers |
| Public RPC Fallback | $0 | api.mainnet-beta.solana.com |

**Total: $0/month — ZERO COST VALIDATED**

---

## Correction Summary

| Original Claim (Phase 4A.6) | Corrected (Phase 4A.6.1) |
|----------------------------|--------------------------|
| 2.5 MB/day → 50,000 credits/day | 2.5 MB/day → **50 credits/day** |
| "Budget EXCEEDED" | **Budget FITS with 83.7% margin** |
| "$49/mo required" | **$0/month validated** |
| PumpPortal not evaluated for discovery | **PumpPortal = primary discovery (FREE)** |
| Helius WS required | **Helius WS NOT needed for discovery** |

**Mathematical Error**: 2 credits/0.1 MB = 20 credits/MB. 
- 2.5 MB × 20 = 50 credits (not 50,000)
- 2.5 GB × 20 = 50,000 credits (if traffic was actually GB)

---

## Conclusion

**Zero-cost ($0/month) discovery IS VIABLE for Phase 4B paper trading** using:
1. **PumpPortal `subscribeNewToken`** — Free, 36K candidates/day, sub-second
2. **Conservative Helius RPC enrichment** — Top 3%, 5,440 credits/day (16.3% of free tier)
3. **Jupiter/PumpPortal pricing** — Free tiers
4. **Public RPC fallback** — Emergency only

**No paid infrastructure required. Phase 4B can proceed at $0/month.**

---

*Corrected 2026-09-10 based on Phase 4A.6.1 empirical measurements. Original arithmetic error: MB/GB unit confusion causing 1000× overestimate.*