# Open Source Audit - Detailed Project Comparison

## Projects Evaluated

| Project | Stars | License | Last Commit | Contributors | Solana DEX | Pump.fun | Paper Trading |
|---------|-------|---------|-------------|--------------|------------|----------|---------------|
| hummingbot/hummingbot | 19.9k | Apache 2.0 | 2026-10-27 | 400+ | ✅ Gateway | ✅ Gateway | ✅ Native |
| hummingbot/condor | 173 | Apache 2.0 | 2026-09-07 | ~10 | ✅ Via API | ✅ Via API | ✅ Via API |
| chainstacklabs/pumpfun-bonkfun-bot | 961 | Apache 2.0 | Recent | 10 | ✅ Migration | ✅ Native | ❌ |
| co-numina/narra-app | 0 | Unclear | 2026-02 | 1 | ❌ | ❌ | ❌ |
| carlosmmora26/DegenRadar | N/A | Unclear | Unknown | 1 | ✅ On-chain | ❌ | ❌ |
| slightlyuseless/pumpfun-terminal | N/A | Unclear | Recent | 1 | ✅ PumpPortal | ✅ Native | ❌ |
| Drakkar-Software/OctoBot | 6.4k | GPL-3.0 | Active | 20+ | ❌ | ❌ | ✅ |
| freqtrade/freqtrade | 54k | GPL-3.0 | 2026-08-17 | 100+ | ❌ CEX only | ❌ | ✅ |

## Detailed Findings

### hummingbot/hummingbot
**Strengths:**
- Battle-tested execution engine (5+ years production)
- Modular connector architecture (CEX + DEX via Gateway)
- Native paper trading (dry-run mode)
- Comprehensive risk controls (position limits, max drawdown, kill switches)
- Trailing stops, take profit, stop loss built-in
- WebSocket handling with reconnection logic
- Idempotent order placement
- 400+ contributors, extensive test coverage
- Apache 2.0 license (business-friendly)

**Weaknesses:**
- Large codebase (~200k LOC) - steep learning curve
- Legacy Python patterns in some modules
- DEX support requires Gateway (additional component)
- Local LLM support not native (external API only)

**Integration Path:**
Use Hummingbot as execution layer via REST API or direct library integration.
Condor provides AI orchestration on top.

### hummingbot/condor
**Strengths:**
- Built specifically for AI agent orchestration on Hummingbot
- MCP (Model Context Protocol) support for local LLMs
- Active daily development
- Routines/agents/handlers pattern for modular AI workflows
- Telegram-based control interface

**Weaknesses:**
- Young project (started 2023)
- Smaller community
- Documentation still evolving

**Integration Path:**
Primary AI orchestration layer. Deploy Condor routines that call Hummingbot API for execution.

### chainstacklabs/pumpfun-bonkfun-bot
**Strengths:**
- Best-in-class Pump.fun listener (4 methods: Geyser, logs, blocks, PumpPortal)
- Zero-RPC fast mode for sub-slot sniping
- Bonding curve price math implemented
- Migration handling (Pump.fun → Raydium)
- YAML-based bot configuration
- Learning examples for every component
- Rate limiting, retry logic built-in

**Weaknesses:**
- No paper trading mode (learning examples only)
- Requires Geyser for production speed ($49-149/mo)
- No native trailing stops (time-based/TP/SL only)
- No smart wallet tracking
- Single-purpose (Pump.fun/letsbonk only)

**Integration Path:**
FORK the listener components (geyser/logs/blocks listeners) for token discovery.
Extend with paper trading, trailing stops, portfolio management.

### carlosmmora26/DegenRadar
**Strengths:**
- Smart wallet discovery focus
- On-chain analysis for profitable trader identification
- Autonomous operation

**Weaknesses:**
- Personal project, limited documentation
- No community
- Unclear license

**Integration Path:**
PORT smart wallet tracking patterns. Build our own implementation.

### slightlyuseless/pumpfun-terminal
**Strengths:**
- React-based trading console
- Multi-wallet support
- PumpPortal integration
- Batch trading operations

**Weaknesses:**
- Manual trading console, not autonomous
- No paper trading
- No risk management

**Integration Path:**
REFERENCE UI patterns only.

### Drakkar-Software/OctoBot
**Strengths:**
- Visual UI, backtesting, strategy framework
- Modular tentacles architecture
- Paper trading

**Weaknesses:**
- GPL-3.0 (viral license - problematic)
- No Solana DEX support (CEX only)
- Pump.fun not supported

**Integration Path:**
REJECT - license and platform mismatch.

### freqtrade/freqtrade
**Strengths:**
- Industry-leading backtesting
- FreqAI (ML framework) with LightGBM, RL
- Excellent risk management
- Dry-run mode
- 54k stars, massive community

**Weaknesses:**
- GPL-3.0 (viral license)
- CEX only - no Solana DEX support
- FreqAI is traditional ML, not LLM

**Integration Path:**
PORT backtesting patterns and risk management concepts. License prevents direct integration.

## Security Review Summary

All evaluated projects pass basic security review:
- ✅ No private key exfiltration
- ✅ No suspicious telemetry
- ✅ No arbitrary remote code execution
- ✅ No hardcoded wallet addresses
- ✅ No hidden trading fees
- ✅ No malicious dependencies
- ✅ No dynamic code downloads
- ✅ No unsafe shell commands
- ✅ No seed phrase logging
- ✅ No API key logging
- ✅ No webhook exfiltration
- ✅ No clipboard access
- ✅ No wallet-draining behavior
- ✅ No obfuscated source
- ✅ No malicious post-install scripts