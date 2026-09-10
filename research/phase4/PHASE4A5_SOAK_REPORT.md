# Phase 4A.5 Soak Test Report

## Status: **NOT RUN** — Runner Prepared

## 24-Hour Paper Soak Test Plan

### Runner
`research/phase4/prototype/run_prototype.py`

### Configuration
```python
PrototypeConfig(
    risk_profile="default",
    use_mock_price=False,  # Live Jupiter/PumpPortal quotes
    default_position_size_sol=0.1,
    default_mint="HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump",
    default_symbol="STONKSZN",
    update_interval_seconds=5.0,
    max_runtime_seconds=86400,  # 24 hours
)
```

### Resumable Design
- Event log: Append-only JSONL (`logs/paper_positions/events_*.jsonl`)
- Rotation: 100MB per file, 30 files max, gzip compression
- State recovery: Position state serializable via `get_pnl_summary()`
- On restart: Reads last event log, reconstructs position

### Metrics to Collect

| Category | Metrics |
|----------|---------|
| **Discovery** | Tokens observed, new tokens detected, deduplication rate |
| **Quotes** | Quotes requested, Jupiter success/failure, PumpPortal success/failure, rate-limit events |
| **Connectivity** | RPC reconnects, WS reconnects, HTTP 429 events, partial outages |
| **Positions** | Opened, closed, currently open, exit causes (stop, trail, TP, time, liq, pressure) |
| **P&L** | Gross P&L, all costs (DEX, priority, network, price impact), net P&L |
| **Risk** | Maximum favorable excursion (MFE), maximum adverse excursion (MAE), max drawdown |
| **Execution** | Slippage estimates, fill latency, quote staleness |
| **Data Quality** | Data gaps, malformed prices, zero/negative prices, untradeable tokens |

### Output Format
- **Event log:** `logs/paper_positions/events_YYYYMMDD_HHMMSS.jsonl` (append-only)
- **Summary:** `logs/paper_positions/soak_summary_YYYYMMDD.json` (on completion)
- **Metrics:** Periodic JSON snapshots every 100 updates

### Running the Test
```bash
cd research/phase4/prototype
python run_prototype.py
# Runs until Ctrl+C or max_runtime_seconds
# Press Ctrl+C to gracefully stop and write summary
```

### If Runtime Limited
**Runner is resumable:**
1. Stop with Ctrl+C → writes final summary
2. Restart → reads last event log, reconstructs position
3. Continues until 24 hours cumulative

### Expected Results Template
```json
{
  "start_time": "2026-09-10T12:00:00Z",
  "end_time": "2026-09-11T12:00:00Z",
  "duration_seconds": 86400,
  "tokens_observed": 150,
  "quotes_requested": 25000,
  "quote_success_rate": 0.985,
  "rate_limit_events": 12,
  "reconnects": 3,
  "positions_opened": 8,
  "positions_closed": 7,
  "exit_causes": {
    "trailing_stop": 3,
    "take_profit": 2,
    "initial_stop": 1,
    "time_exit": 1
  },
  "gross_pnl_sol": 0.05,
  "total_costs_sol": 0.008,
  "net_pnl_sol": 0.042,
  "max_mfe_pct": 45.2,
  "max_mae_pct": 18.7,
  "avg_slippage_bps": 12,
  "avg_latency_ms": 180,
  "data_gaps": 0
}
```

### Next Steps
Run this soak test when ready to validate 24-hour continuous operation before Phase 4B.