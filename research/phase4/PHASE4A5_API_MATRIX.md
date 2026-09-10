# Phase 4A.5 API Matrix — Current Provider Endpoints & Limits

## Jupiter Aggregator API

### Base URL
```
https://quote-api.jup.ag/v6
https://price.jup.ag/v4
https://tokens.jup.ag
```

### Endpoints

| Endpoint | Method | Version | Auth | Free Tier | Rate Limit | Use Case |
|----------|--------|---------|------|-----------|------------|----------|
| `/quote` | GET | v6 | None | ✅ | 100 req/s | Swap quotes with routes |
| `/price` | GET | v4 | None | ✅ | 100 req/s | Batch token prices |
| `/tokens` | GET | - | None | ✅ | Unlimited | Token list with decimals |

### `/quote` Request Parameters
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `inputMint` | string | ✅ | - | Input token mint address |
| `outputMint` | string | ✅ | - | Output token mint address |
| `amount` | string | ✅ | - | Input amount in **atomic units of input mint** |
| `slippageBps` | string | No | 50 | Slippage tolerance in basis points |
| `onlyDirectRoutes` | string | No | false | Restrict to direct routes only |
| `asLegacyTransaction` | string | No | false | Legacy transaction format |

### `/quote` Response Schema
```json
{
  "inputMint": "So11111111111111111111111111111111111111112",
  "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "inAmount": "100000000",
  "outAmount": "99500000",
  "otherAmountThreshold": "98505000",
  "swapMode": "ExactIn",
  "slippageBps": 50,
  "priceImpactPct": 0.015,
  "routePlan": [
    {
      "swapInfo": {
        "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
        "label": "Raydium",
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "inAmount": "100000000",
        "outAmount": "99500000",
        "feeAmount": "250000",
        "feeMint": "So11111111111111111111111111111111111111112",
        "feeBps": 25
      },
      "percent": 100
    }
  ],
  "contextSlot": 123456789,
  "timeTaken": 0.045
}
```

### Key Response Fields
| Field | Type | Description |
|-------|------|-------------|
| `inAmount` | string | Input amount in atomic units of `inputMint` |
| `outAmount` | string | Output amount in atomic units of `outputMint` |
| `priceImpactPct` | number | Price impact as decimal (0.015 = 1.5%) |
| `routePlan[].swapInfo.feeBps` | number | DEX swap fee in basis points |
| `routePlan[].swapInfo.label` | string | DEX/AMM name (Raydium, Orca, etc.) |

### `/price` Request Parameters
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `ids` | string | ✅ | - | Comma-separated token mint addresses |
| `vsToken` | string | No | SOL | Quote currency mint |

### `/price` Response Schema
```json
{
  "data": {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
      "id": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
      "type": "token",
      "price": "0.00001234",
      "vsToken": "So11111111111111111111111111111111111111112",
      "vsTokenSymbol": "SOL"
    }
  },
  "timeTaken": 0.012
}
```

### `/tokens` Response Schema
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

### Base URL
```
https://pumpportal.fun/api
```

### Endpoints

| Endpoint | Method | Auth | Free Tier | Rate Limit | Use Case |
|----------|--------|------|-----------|------------|----------|
| `/price` | GET | None | ✅ | ~5/s | Bonding curve price |
| `/trade-local` | POST | None* | ✅ | ~5/s | Build unsigned tx |
| `/new-tokens` | WS | None | ✅ | Continuous | New token stream |

*Requires funded wallet for actual broadcast; building tx is free.

### `/price` Request
```
GET /api/price?mint=HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump
```

### `/price` Response
```json
{
  "price": 0.000523,
  "marketCap": 523000,
  "liquidity": 15000,
  "volume24h": 50000,
  "bondingCurveProgress": 0.75
}
```

### `/trade-local` Request
```json
{
  "publicKey": "YOUR_WALLET_PUBLIC_KEY",
  "action": "buy",
  "mint": "TOKEN_MINT",
  "amount": 100000000,
  "denominatedInSol": true,
  "slippage": 50,
  "priorityFee": 0.0005,
  "pool": "pump"
}
```

### WebSocket `/new-tokens`
```javascript
ws = new WebSocket("wss://pumpportal.fun/api/data");
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // { mint, name, symbol, uri, bondingCurve, creator, timestamp }
};
```

---

## Helius RPC API

### Base URL
```
https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### Free Tier
- 100,000 requests/day
- WebSocket included
- No credit card required for free tier

### Key Methods Used
| Method | Purpose | Cost |
|--------|---------|------|
| `getSignaturesForAddress` | Pump.fun program signatures | 1 CU |
| `getTransaction` | Decode create/buy/sell events | 1 CU |
| `getTokenAccountsByOwner` | Holder analysis | 1 CU |
| `getMultipleAccounts` | Batch account reads | 1 CU per account |
| `logsSubscribe` | Real-time program logs | WS |

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

### Create Event Signature
```
Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]
Program log: Instruction: Create
Program log: mint: HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump
Program log: bondingCurve: ...
Program log: creator: ...
Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success
```

---

## Solscan API

### Base URL
```
https://public-api.solscan.io
https://pro-api.solscan.io (paid)
```

### Free Tier Limits
- 10 req/s burst
- 100 req/min sustained
- No authentication for basic endpoints

### Key Endpoints
| Endpoint | Purpose |
|----------|---------|
| `/token/holders?token=MINT` | Holder list with balances |
| `/token/transactions?token=MINT` | Transfer history |
| `/account/tokens?account=ADDRESS` | Wallet token balances |

---

## Rate Limit Summary

| Provider | Free Tier | Our Usage | Headroom | Backoff Strategy |
|----------|-----------|-----------|----------|------------------|
| Jupiter Quote | 100 req/s | 5/s | 20x | Token bucket (100 tokens, 100/s refill) |
| Jupiter Price | 100 req/s | 5/s | 20x | Token bucket |
| PumpPortal Price | ~5/s | 2/s | 2.5x | Exponential backoff |
| PumpPortal WS | Unlimited | 1 conn | N/A | Auto-reconnect |
| Helius RPC | 100k/day | 50k/day | 2x | Queue + backoff |
| Helius WS | Included | 1 conn | N/A | Auto-reconnect |
| Solscan | 100/min | 10/min | 10x | Queue + backoff |

---

## Cost Matrix (Per Trade)

| Component | Jupiter Quote | PumpPortal | Helius RPC |
|-----------|---------------|------------|------------|
| API Call | Free | Free | Free (within tier) |
| DEX Fee | Route-dependent (25-30 bps) | 0 (bonding curve) | N/A |
| Price Impact | In `outAmount` | ~1% estimate | N/A |
| Solana Base Fee | 0.000005 SOL | 0.000005 SOL | N/A |
| Priority Fee | Configurable | Configurable | N/A |
| Jito Tip | Optional | Optional | N/A |

---

## Version Compatibility

| Component | Current Version | Last Verified | Adapter Pattern |
|-----------|-----------------|---------------|-----------------|
| Jupiter Quote | v6 | 2025-09 | Base URL configurable |
| Jupiter Price | v4 | 2025-09 | Base URL configurable |
| Jupiter Tokens | - | 2025-09 | Base URL configurable |
| PumpPortal | - | 2025-09 | Base URL configurable |
| Helius RPC | - | 2025-09 | Standard JSON-RPC |

**All base URLs are configurable constants for future API version changes.**