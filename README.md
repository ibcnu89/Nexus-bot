# PowerTrader AI — Nexus-bot

![PowerTrader AI](web/static/icon-512.png)

**AI-powered cryptocurrency trading platform with FOMO social intelligence**

---

## 🎯 Overview

PowerTrader AI is a sophisticated cryptocurrency trading system that combines:

- **Algorithmic Trading Engine** — Pattern-matching kNN signals with multi-timeframe analysis
- **Paper Trading Execution** — Alpaca Markets integration (crypto)
- **FOMO Social Intelligence** — Real-time copy-trading insights from verified top performers
- **Mobile-First Web UI** — FOMO-inspired design with live charts, portfolio, and alerts

> **⚠️ Trading Safety**: FOMO data is **observational only**. It informs strategy but **never executes trades**. Alpaca paper trading is isolated from social signals.

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        PowerTrader AI                            │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Core Bot   │  │   FOMO API   │  │    Web Interface     │  │
│  │  (Python)    │  │  (Social)    │  │    (FastAPI/htmx)    │  │
│  ├──────────────┤  ├──────────────┤  ├──────────────────────┤  │
│  │ • pt_trader  │  │ • Leaderboard│  │ • Markets (50 coins) │  │
│  │ • pt_thinker │  │ • Profiles   │  │ • Search             │  │
│  │ • pt_trainer │  │ • Trades     │  │ • Portfolio          │  │
│  │ • pt_hub     │  │ • Holdings   │  │ • Feed (Social)      │  │
│  │              │  │ • Wallets    │  │ • Alerts (Real-time) │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │
│         │                 │                     │              │
│         └─────────────────┼─────────────────────┘              │
│                           ▼                                    │
│              ┌────────────────────────┐                        │
│              │   Alpaca Paper Trading  │                        │
│              │   (Execution Layer)      │                        │
│              └────────────────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

### 🤖 Core Trading Engine
- **Pattern Recognition** — kNN/kernel regression on multi-timeframe candles (1h–1w)
- **Online Learning** — Adjusts pattern weights after each candle closes
- **Multi-Coins** — 50+ crypto pairs via CoinGecko + Alpaca
- **DCA & Risk Management** — Configurable levels, trailing stops, allocation rules

### 📊 FOMO Social Intelligence (Phase 1 + 2)
| Feature | Endpoint | Description |
|---------|----------|-------------|
| **Leaderboards** | `GET /api/fomo/leaderboard` | 24h / 7d / 30d / All-time top traders |
| **Trader Profiles** | `GET /api/fomo/trader/{handle}` | PnL, stats, wallets, holdings, trade history |
| **Live Trades** | `GET /api/fomo/trader/{handle}/trades` | Open/closed positions with entry/exit/PnL |
| **Holdings** | `GET /api/fomo/trader/{handle}/holdings` | Multi-chain balances with USD values |
| **Wallets** | `GET /api/fomo/trader/{handle}/wallets` | Solana + EVM addresses |
| **Real-time Alerts** | WebSocket `/ws` → `fomo_alert` | Trade/thesis/whale events streamed live |

### 📱 Mobile-First Web UI
- **Bottom Navigation** — Markets, Search, Portfolio, Feed, Alerts
- **Live Charts** — Chart.js with WebSocket price updates (5s)
- **FOMO Feed** — Trader leaderboard with clickable profiles
- **Real-time Alerts** — Live trader activity with thesis, chain, value
- **PWA Ready** — Manifest, icons, offline-capable

### 🔐 Security & Safety
- **Credential Isolation** — `fomo_key.txt`, `alpaca_key.txt`, `alpaca_secret.txt` in `.gitignore`
- **Server-Side Only** — API keys never reach browser
- **Paper Trading Only** — No live execution capability
- **JWT Auth** — HttpOnly cookies + Bearer tokens
- **Rate Limiting** — 60 req/min client-side, exponential backoff

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- Alpaca paper trading account (free at [alpaca.markets](https://alpaca.markets))
- FOMO API key (free at [fomoapi.io](https://fomoapi.io) — 10k credits/mo)

### Installation
```bash
git clone https://github.com/ibcnu89/Nexus-bot.git
cd Nexus-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r web/requirements.txt

# Configure credentials
echo "YOUR_ALPACA_KEY" > alpaca_key.txt
echo "YOUR_ALPACA_SECRET" > alpaca_secret.txt
echo "YOUR_FOMO_KEY" > fomo_key.txt

# Start the web server
cd web && python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

### Access
- **Local**: `http://localhost:8080`
- **Mobile (SSH tunnel)**: `ssh -L 8080:localhost:8080 user@your-server`
- **First Login**: On first startup, a random admin password is generated and printed to console. Check server logs for credentials.

---

## 📡 API Reference

### FOMO Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/fomo/status` | Connection status |
| `GET` | `/api/fomo/leaderboard?window=24h&limit=50` | Top traders |
| `GET` | `/api/fomo/trader/{handle}` | Full profile |
| `GET` | `/api/fomo/trader/{handle}/trades?limit=25` | Trade history |
| `GET` | `/api/fomo/trader/{handle}/holdings` | Current positions |
| `GET` | `/api/fomo/trader/{handle}/wallets` | Wallet addresses |
| `GET` | `/api/fomo/ws/status` | WebSocket status |

### Market Data
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/markets` | All 50 coins with price/change/volume |
| `GET` | `/api/markets/{symbol}` | Detail + 200 candles |
| `GET` | `/api/klines/{symbol}?interval=1h&limit=200` | OHLCV data |

### Trading (Paper)
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/account` | Account summary |
| `GET` | `/api/positions` | Open positions |
| `GET` | `/api/orders/{symbol}` | Order history |
| `POST` | `/api/start` | Start bot |
| `POST` | `/api/stop` | Stop bot |

### WebSocket
Connect to `/ws` for real-time updates:
```json
// Subscribe to price updates
{"type": "subscribe", "symbols": ["BTCUSDT", "ETHUSDT"]}

// Subscribe to FOMO alerts
{"type": "fomo_subscribe"}

// Unsubscribe from FOMO alerts
{"type": "fomo_unsubscribe"}
```

Message types: `prices`, `status`, `fomo_alert`, `fomo_status`

---

## 🔧 Configuration

### Environment Variables
|| Variable | Default | Description ||
||----------|---------|-------------|
|| `SECRET_KEY` | *(required)* | JWT signing key — must be set in environment ||
|| `ALGORITHM` | `HS256` | JWT algorithm ||
|| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Token lifetime ||
|| `WEB_HOST` | `0.0.0.0` | Server host ||
|| `WEB_PORT` | `8080` | Server port ||
|| `BOT_DIR` | `/home/ibcnu/PowerTraderAI` | Project root ||

### GUI Settings (`gui_settings.json`)
```json
{
  "coins": ["BTC", "ETH", "BNB", "SOL", ...],
  "trade_start_level": 3,
  "start_allocation_pct": 0.5,
  "dca_multiplier": 2.0,
  "max_dca_buys_per_24h": 2,
  "trailing_gap_pct": 0.5
}
```

---

## 🧪 Testing

```bash
# Unit tests (FOMO client)
cd ~/Nexus-bot
python -c "
import asyncio
from fomo_client import FOMOClient

async def test():
    client = FOMOClient()
    try:
        lb = await client.get_leaderboard('24h', limit=3)
        print(f'Leaderboard: {len(lb.traders)} traders')
        trader = await client.get_trader(lb.traders[0].handle)
        print(f'Profile: {trader.display_name}')
        trades = await client.get_trades(trader.handle, limit=3)
        print(f'Trades: {len(trades)}')
        holdings = await client.get_holdings(trader.handle)
        print(f'Holdings: {len(holdings)}')
        print('All tests passed!')
    finally:
        await client.close()
asyncio.run(test())
"
```

### Test Coverage
- ✅ Live FOMO API (leaderboard, profiles, trades, holdings, wallets)
- ✅ WebSocket connection + message normalization
- ✅ FastAPI routes (auth, markets, FOMO, Alpaca)
- ✅ WebSocket (prices, status, FOMO alerts)
- ✅ Failure modes (401, 429, 404, 500, timeout, malformed)
- ✅ Regression (auth, markets, portfolio, feed, alerts, CoinGecko, Alpaca)

---

## 📁 Project Structure

```
Nexus-bot/
├── fomo_client.py              # FOMO API async client
├── pt_alpaca.py                # Alpaca paper trading wrapper
├── pt_hub.py                   # Tkinter GUI (original)
├── pt_thinker.py               # Pattern-matching signals
├── pt_trader.py                # Trading execution logic
├── pt_trainer.py               # Model training
├── requirements.txt            # Core dependencies
├── web/
│   ├── main.py                 # FastAPI app + routes
│   ├── requirements.txt        # Web dependencies
│   ├── templates/
│   │   ├── index.html          # Main layout + WS handler
│   │   ├── login.html          # Auth page
│   │   └── partials/
│   │       ├── alerts.html     # Real-time FOMO alerts
│   │       ├── feed.html       # Social feed + leaderboard
│   │       ├── portfolio.html  # Positions + P&L
│   │       └── ...             # Other tabs
│   └── static/
│       ├── icon*.png           # PWA icons
│       └── manifest.json       # PWA manifest
├── .gitignore                  # Excludes credentials, venv, data
├── LICENSE
└── README.md
```

---

## 🛣 Roadmap

### Phase 3: Proprietary Data Pipeline
- [ ] Hyperliquid CSV ingestion (perp fills, PnL)
- [ ] Solana RPC ingestion (spot trades, swaps)
- [ ] Custom PnL calculation engine
- [ ] Eliminate external API dependencies

### Phase 4: Advanced Intelligence
- [ ] Copy-trading signal generation
- [ ] Smart-money flow tracking
- [ ] Thesis sentiment analysis
- [ ] Cross-chain wallet clustering

### Phase 5: MCP + AI Integration
- [ ] MCP server for trading data access
- [ ] LLM-powered strategy synthesis
- [ ] Natural language trading interface

---

## ⚖️ License

MIT License — see [LICENSE](LICENSE)

---

## ⚠️ Disclaimer

**This software is for educational and research purposes only.**

- Not financial advice
- Paper trading only — no live execution
- FOMO data is third-party social intelligence
- Past performance ≠ future results
- Cryptocurrency trading involves substantial risk

**Use at your own risk. Always do your own research.**

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Ensure tests pass
4. Submit a PR with clear description

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/ibcnu89/Nexus-bot/issues)
- **FOMO API**: [fomoapi.io/docs](https://fomoapi.io/docs)
- **Alpaca Docs**: [alpaca.markets/docs](https://alpaca.markets/docs)

---

**Built with ❤️ for the crypto trading community**

*PowerTrader AI — Where algorithms meet social intelligence*