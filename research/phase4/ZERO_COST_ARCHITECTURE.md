# Zero-Cost Architecture - $0/Month Infrastructure Plan

## Overview

This document details how to build and operate the microcap trading system at $0/month using free tiers, public APIs, and local infrastructure.

## Free Tier Inventory

| Service | Free Tier | Our Usage | Headroom | Fallback |
|---------|-----------|-----------|----------|----------|
| **Helius RPC** | 100k req/day | ~50k/day | 2x | Triton One, QuickNode, public RPC |
| **Helius WebSocket** | Included | Continuous | - | logsSubscribe on public RPC |
| **Jupiter Price API** | 100 req/s | ~10/s | 10x | Raydium SDK direct |
| **Jupiter Quote API** | 100 req/s | ~5/s | 20x | PumpPortal for bonding curve |
| **Pump.fun API** | Free | Continuous | - | logsSubscribe |
| **PumpPortal API** | Free | ~5/s | - | Geyser (paid) |
| **Solscan API** | Free tier | ~1k/day | - | Helius getTokenAccounts |
| **Twitter API** | 100 tweets/mo | Low | - | RSS/Reddit scraping |
| **Reddit API** | Free (rate limited) | Low | - | Pushshift |
| **Local LLM (Ollama)** | Unlimited (local HW) | Continuous | - | Smaller models |
| **SQLite/DuckDB** | Unlimited | All data | - | N/A |
| **GitHub Actions** | 2000 min/mo | CI/CD | - | Local CI |

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         LOCAL INFRASTRUCTURE (User Hardware)                │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  Ollama      │  │  SQLite/     │  │  Condor      │  │  Hummingbot  │   │
│  │  (Local LLM) │  │  DuckDB      │  │  (AI Engine) │  │  (Execution) │   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘   │
│         │                 │                 │                 │            │
│         └─────────────────┼─────────────────┼─────────────────┘            │
│                           ▼                 ▼                              │
│                  ┌──────────────────────────────────────┐                 │
│                  │         Event Log (JSONL)            │                 │
│                  └──────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FREE PUBLIC APIs                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │ Helius   │ │ Jupiter  │ │ Pump.fun │ │ PumpPortal│ │ Solscan  │         │
│  │ RPC/WS   │ │ Price/   │ │ API      │ │ API      │ │ API      │         │
│  │          │ │ Quote    │ │          │ │          │ │          │         │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘         │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Component-by-Component Free Tier Analysis

### 1. Solana RPC - Helius Free Tier
- **Limit**: 100,000 requests/day
- **Our Usage**: ~50,000/day (discovery + enrichment + price checks)
- **Headroom**: 2x
- **Endpoints Used**:
  - `getTokenAccountsByOwner` (holder analysis)
  - `getAccountInfo` (bonding curve, pool state)
  - `getMultipleAccounts` (batch reads)
  - `logsSubscribe` (Pump.fun create events)
- **Fallback**: Triton One (100k/day), QuickNode (50k/day), public RPC endpoints

### 2. Real-Time Token Discovery - Helius WebSocket (logsSubscribe)
- **Limit**: Included in RPC quota
- **Method**: Subscribe to Pump.fun program logs (6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P)
- **Latency**: ~1-2 slots after creation
- **Alternative**: PumpPortal WebSocket (free, higher latency)
- **Geyser (Paid)**: Only needed for sub-slot latency ($49/mo) - NOT required for $0 tier

### 3. Price Feeds - Jupiter Aggregator
- **Price API**: `https://price.jup.ag/v4/price?ids=MINT1,MINT2`
  - Limit: 100 req/s
  - Our usage: Batch 50 tokens every 10s = 5 req/s
- **Quote API**: `https://quote-api.jup.ag/v6/quote`
  - Limit: 100 req/s
  - Our usage: 1 quote per position update (~5/s)
- **Fallback**: Raydium SDK direct for AMM pools

### 4. Bonding Curve Prices - PumpPortal
- **API**: `https://pumpportal.fun/api/price?mint=MINT`
- **Limit**: Generous free tier
- **Usage**: Pre-graduation tokens only
- **Alternative**: Calculate from bonding curve account data via RPC

### 5. Token Metadata - Jupiter Token List + Helius
- **Jupiter**: `https://token.jup.ag/all` (cached, updated daily)
- **Helius**: `getAsset` / `getAssetBatch` for on-chain metadata
- **Fallback**: Solscan API, Pump.fun API

### 6. Holder Analysis - Helius RPC
- **Method**: `getTokenAccountsByOwner` for top holders
- **Batch**: `getMultipleAccounts` for holder balances
- **Quota**: ~1,000 holders/day within free tier
- **Fallback**: Solscan holder API

### 7. Smart Wallet Tracking - Custom + Dune (if needed)
- **Approach**: Track profitable wallets from our own trade history
- **Dune Free**: 100k row results/month - sufficient for wallet tagging
- **Alternative**: Build internal leaderboard from observed trades

### 8. Narrative Intelligence - Local LLM + Free Social APIs
- **Twitter**: Free tier (100 tweets/month) + RSS feeds
- **Reddit**: Free API (rate limited) + Pushshift archive
- **Telegram**: Public channel scraping (no API needed)
- **LLM**: Ollama (llama3.1:8b or qwen2.5:7b) - runs locally
- **Cost**: $0 (uses existing GPU/CPU)

### 9. Data Storage - SQLite + DuckDB
- **SQLite**: Primary storage (positions, events, tokens)
- **DuckDB**: Analytics queries (Parquet export for ML)
- **Capacity**: Unlimited (local disk)
- **Backup**: Git LFS for event logs, or rsync to external drive

### 10. Compute - Local Hardware
- **Discovery Worker**: 1 CPU core, 2GB RAM
- **Enrichment Worker**: 2 CPU cores, 4GB RAM
- **LLM Inference**: GPU (RTX 3060 12GB+) or CPU (8+ cores)
- **Execution Engine**: 1 CPU core, 1GB RAM
- **Total**: Runs on single modern desktop/laptop

## Rate Limit Management

```python
# Token bucket rate limiter (implemented in price_source.py)
class RateLimiter:
    def __init__(self, max_rps: float):
        self.max_rps = max_rps
        self.tokens = max_rps
        self.last_update = time.time()
    
    async def acquire(self):
        while True:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.max_rps, self.tokens + elapsed * self.max_rps)
            if self.tokens >= 1:
                self.tokens -= 1
                self.last_update = now
                return
            await asyncio.sleep(0.1)
```

### Per-Endpoint Limits
| Endpoint | Max RPS | Burst | Implementation |
|----------|---------|-------|----------------|
| Helius RPC | 100 | 200 | Token bucket |
| Helius WS | N/A | N/A | Single connection |
| Jupiter Price | 100 | 200 | Token bucket |
| Jupiter Quote | 100 | 200 | Token bucket |
| PumpPortal | 50 | 100 | Token bucket |
| Solscan | 10 | 20 | Token bucket |

## Failure Scenarios & Mitigations

| Failure | Detection | Mitigation |
|---------|-----------|------------|
| Helius RPC down | Health check fails | Switch to Triton One |
| Helius WS disconnect | No messages 30s | Reconnect with backoff |
| Jupiter rate limited | 429 response | Exponential backoff + cache |
| PumpPortal down | 5xx errors | Fall back to RPC calculation |
| LLM OOM | CUDA OOM error | Fallback to smaller model |
| Disk full | < 1GB free | Rotate/compress old logs |

## When Payment May Eventually Be Needed

| Trigger | Timeline | Service | Est. Cost |
|---------|----------|---------|-----------|
| >100k RPC/day sustained | Month 3-6 | Helius Pro | $49/mo |
| Sub-slot sniper latency needed | Month 6+ | Yellowstone Geyser | $49-149/mo |
| >100 Twitter API calls/mo | Month 1-2 | Twitter Basic | $100/mo |
| Dune queries >100k rows | Month 6+ | Dune Pro | $390/mo |
| GPU upgrade for 70B LLM | Year 1+ | Hardware | $1,500-3,000 |

**Key Principle**: None of these are required for Phase 4A/B development and paper trading validation.

## Monitoring Free Tier Health

```python
# Health check endpoints (run every 60s)
async def check_free_tier_health():
    checks = {
        "helius_rpc": await check_rpc("https://mainnet.helius-rpc.com"),
        "jupiter_price": await check_api("https://price.jup.ag/v4/price?ids=SOL"),
        "pumpportal": await check_api("https://pumpportal.fun/api/price?mint=SOL"),
        "disk_space": check_disk_free("/data") > 1_000_000_000,
        "memory": psutil.virtual_memory().percent < 85,
    }
    return checks
```

## Summary

**Total Monthly Cost: $0**

All core functionality achievable on free tiers:
- ✅ Token discovery (Helius logsSubscribe)
- ✅ Price feeds (Jupiter + PumpPortal)
- ✅ On-chain enrichment (Helius RPC)
- ✅ Narrative intelligence (Local LLM + free social)
- ✅ Paper trading execution (Hummingbot dry-run)
- ✅ Data storage (SQLite/DuckDB)
- ✅ AI orchestration (Condor + Ollama)

The system is designed to scale within free limits for 6-12 months of development before any paid infrastructure becomes necessary.