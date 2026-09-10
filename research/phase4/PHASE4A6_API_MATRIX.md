# Phase 4A.6 API Matrix — Current Provider Endpoints & Limits (Verified 2026-09-10)

> **Corrected 2026-09-10**: Updated verification dates from 2025 to 2026. Added PumpPortal free discovery finding.

## Jupiter Aggregator API

### Base URLs
```
Quote (Swap V1):  https://api.jup.ag/swap/v1/quote
Price V3:         https://api.jup.ag/price/v3
Tokens:           https://tokens.jup.ag/all
```

### Endpoints

| Endpoint | Method | Version | Auth | Free Tier | Rate Limit | Use Case |
|----------|--------|---------|------|-----------|------------|----------|
| `/swap/v1/quote` | GET | V1 | Optional (x-api-key) | ✅ | 100 req/s | Swap quotes with routes |
| `/price/v3` | GET | V3 | Optional (x-api-key) | ✅ | 100 req/s | Batch token prices (USD) |
| `/tokens/all` | GET | V2/legacy | None | ✅ | Unlimited | Token list with decimals |

### `/swap/v1/quote` Request Parameters
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `inputMint` | string | ✅ | - | Input token mint address |
| `outputMint` | string | ✅ | - | Output token mint address |
| `amount` | string | ✅ | - | Input amount in **atomic units of input mint** |
| `slippageBps` | string | No | 50 | Slippage tolerance in basis points |
| `restrictIntermediateTokens` | string | No | true | Restrict to direct routes |
| `onlyDirectRoutes` | string | No | false | Only direct routes |
| `asLegacyTransaction` | string | No | false | Legacy transaction format |
| `instructionVersion` | string | No | V2 | V1 or V2 |
| `maxAccounts` | string | No | 64 | Max accounts in transaction |

### `/swap/v1/quote` Response Schema (Current V1)
```json
{
  "inputMint": "So11111111111111111111111111111111111111112",
  "inAmount": "100000000",
  "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "outAmount": "16198753",
  "otherAmountThreshold": "16117760",
  "swapMode": "ExactIn",
  "slippageBps": 50,
  "platformFee": {"feeBps": 0, "amount": "0"},
  "priceImpactPct": "0",
  "routePlan": [
    {
      "swapInfo": {
        "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
        "label": "Raydium",
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "inAmount": "100000000",
        "outAmount": "16198753",
        "feeAmount": "250000",
        "feeMint": "So11111111111111111111111111111111111111112",
        "feeBps": 25
      },
      "percent": 100
    }
  ],
  "contextSlot": 123456789,
  "timeTaken": 0.015,
  "mostReliableAmmsQuoteReport": { ... }
}
```

### Critical Response Fields
| Field | Type | Description | Accounting Treatment |
|-------|------|-------------|---------------------|
| `inAmount` | string | Input amount in atomic units of `inputMint` | Reference only |
| `outAmount` | string | **BEST output AFTER AMM/platform fees** | Use directly — do NOT subtract fees again |
| `otherAmountThreshold` | string | Minimum output after slippage | Use for executable price (slippage bound) |
| `priceImpactPct` | string | Price impact as decimal string | INFORMATIONAL only |
| `routePlan[].swapInfo.feeBps` | number | DEX swap fee in basis points | INFORMATIONAL — already in outAmount |
| `routePlan[].swapInfo.label` | string | DEX/AMM name | For logging |
| `platformFee.feeBps` | number | Jupiter platform fee in bps | INFORMATIONAL — already in outAmount |

### `/price/v3` Request Parameters
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `ids` | string | ✅ | - | Comma-separated token mint addresses (max 50) |

### `/price/v3` Response Schema
```json
{
  "So11111111111111111111111111111111111111112": {
    "usdPrice": 147.4789340738336,
    "blockId": 348004023,
    "decimals": 9,
    "priceChange24h": 1.2907622140620008
  },
  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": {
    "usdPrice": 0.4056018512541055,
    "blockId": 348004026,
    "decimals": 6,
    "priceChange24h": 0.5292887924920519
  }
}
```

### `/tokens/all` Response Schema
```json
[
  {
    "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "symbol": "USDC",
    "name": "USD Coin",
    "decimals": 6,
    "logoURI": "https://...",
    "tags": ["stablecoin", "verified"]
  }
]
```

---

## PumpPortal API

### Base URLs
```
REST Price:     https://pumpportal.fun/api/price
WebSocket:      wss://pumpportal.fun/api/data?api-key=YOUR_KEY
Trade Local:    https://pumpportal.fun/api/trade-local
```

### Endpoints

| Endpoint | Method | Auth | Classification | Rate Limit | Use Case |
|----------|--------|------|----------------|------------|----------|
| `/api/price` | GET | None | FREE | ~5/s | Bonding curve price |
| `/api/data` (WS) | WS | Optional (API Key) | See streams | 1 conn | Real-time data |
| `/api/trade-local` | POST | API Key | METERED_CRYPTO | ~5/s | Build unsigned tx |

### WebSocket Streams

| Stream | Classification | Cost | Wallet Required | API Key Required |
|--------|---------------|------|-----------------|------------------|
| `subscribeNewToken` | **FREE** | No charge | No | **Optional** (works without) |
| `subscribeMigration` | **FREE** | No charge | No | Optional |
| `subscribeTokenTrade` | METERED_CRYPTO | 0.01 SOL per 10k events | Yes (0.02 SOL) | Required |
| `subscribeAccountTrade` | METERED_CRYPTO | 0.01 SOL per 10k events | Yes (0.02 SOL) | Required |

**Key Finding (2026-09-10)**: `subscribeNewToken` works **without API key**, provides ~28 events/min, ~25 unique mints/min, sub-second latency, stable connection.

### `/api/price` Response
```json
{
  "price": 0.000523,
  "marketCap": 523000,
  "liquidity": 15000,
  "volume24h": 50000,
  "bondingCurveProgress": 0.75
}
```

### `/api/trade-local` Request
```json
{
  "publicKey": "WALLET_PUBLIC_KEY",
  "action": "buy",
  "mint": "TOKEN_MINT",
  "amount": 100000000,
  "denominatedInSol": true,
  "slippage": 50,
  "priorityFee": 0.0005,
  "pool": "pump"
}
```

### Local Transaction API Fees
| Fee Type | Rate | Notes |
|----------|------|-------|
| Local Transaction | 0.5% | Calculated before slippage |
| Pump.fun bonding curve | Implicit | Built into curve math |
| Solana network | 0.000005 SOL | Base fee |
| Priority fee | User specified | Jito tip optional |

---

## Helius RPC API

### Base URLs
```
RPC:     https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
WSS:     wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### Free Tier Limits (Verified 2026-09-10)
| Feature | Limit |
|---------|-------|
| Monthly Credits | 1,000,000 |
| RPC Rate Limit | 10 req/s |
| DAS API | 2 req/s |
| Enhanced APIs | 2 req/s |
| Standard WSS (logsSubscribe, accountSubscribe) | Included (metered: 2 credits/0.1 MB) |
| Enhanced WSS (transactionSubscribe, enhanced accountSubscribe) | NOT available |
| LaserStream gRPC | NOT available |
| Concurrent WS Connections | 5 |
| Webhooks | 5 (100k addresses each) |

### Credit Costs (Key Methods)
| Method | Credits | Notes |
|--------|---------|-------|
| Standard RPC (getAccountInfo, getBalance, etc.) | 1 | Most methods |
| getSignaturesForAddress | 1 | Standard |
| getTransaction | 1 | Standard |
| getTokenAccountsByOwner | 1 | Standard |
| getMultipleAccounts | 1 per account | Batched |
| getProgramAccounts | 10 | Heavy |
| simulateTransaction | 1 | Standard |
| DAS API (all) | 10 | Digital asset standard |
| Enhanced Transactions API | 100 | Parsed transactions |
| Webhook Events | 1 | Per event sent |
| **LaserStream WSS (standard methods)** | **2 per 0.1 MB** | Metered by data volume |
| LaserStream WSS (Helius extensions) | 2 per 0.1 MB | Not on free tier |

### WebSocket `logsSubscribe` for Pump.fun
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "logsSubscribe",
  "params": [
    {"mentions": ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},
    {"commitment": "processed"}
  ]
}
```

---

## Public Solana RPC (Fallback)

### Base URLs
```
RPC:     https://api.mainnet-beta.solana.com
WSS:     wss://api.mainnet-beta.solana.com
```

### Verified Status (2026-09-10)
| Feature | Status |
|---------|--------|
| `logsSubscribe` | ✅ Supported |
| HTTP RPC | ✅ Supported |
| Rate Limits | Unpublished, no SLA |
| SLA | None |

**Only `api.mainnet-beta.solana.com` supports `logsSubscribe` among free public endpoints tested. Alchemy demo and Ankr rejected connections.**

---

## Rate Limit Summary

| Provider | Free Tier | Expected Usage | Headroom | Backoff Strategy |
|----------|-----------|----------------|----------|------------------|
| Jupiter Quote | 100 req/s | 10/s | 10x | Token bucket (100 tokens, 100/s refill) |
| Jupiter Price V3 | 100 req/s | 10/s | 10x | Token bucket |
| PumpPortal Price | ~5/s | 2/s | 2.5x | Exponential backoff |
| PumpPortal WS (subscribeNewToken) | FREE, 1 conn | 1 conn | N/A | Auto-reconnect (0 in 10-min test) |
| Helius RPC | 10 req/s / 33k credits/day | ~5,440 credits/day (hybrid conservative) | **5.5x** | Queue + backoff |
| Helius WS | 5 conn | 0 (not used for discovery) | N/A | N/A |
| Public RPC (api.mainnet-beta) | Unlimited rate-limited | Fallback only | N/A | Exponential backoff |

---

## Cost Matrix (Per Trade)

| Component | Jupiter Quote | PumpPortal | Helius RPC |
|-----------|---------------|------------|------------|
| API Call | Free | Free | Free (within credits) |
| DEX Fee | Embedded in outAmount | 0 (bonding curve) | N/A |
| Platform Fee | Embedded in outAmount | 0.5% (trade-local) | N/A |
| Price Impact | Via otherAmountThreshold | ~1% estimate | N/A |
| Solana Base Fee | 0.000005 SOL | 0.000005 SOL | N/A |
| Priority Fee | User specified | User specified | N/A |

---

## Version Compatibility

| Component | Current Version | Last Verified | Adapter Pattern |
|-----------|-----------------|---------------|-----------------|
| Jupiter Quote | V1 (swap/v1) | 2026-09-10 | Base URL configurable |
| Jupiter Price | V3 | 2026-09-10 | Base URL configurable |
| Jupiter Tokens | V2 (legacy) | 2026-09-10 | Base URL configurable |
| PumpPortal | - | 2026-09-10 | Base URL configurable |
| Helius RPC | - | 2026-09-10 | Standard JSON-RPC |
| Public RPC | - | 2026-09-10 | Standard JSON-RPC |

**All base URLs are configurable constants for future API version changes.**