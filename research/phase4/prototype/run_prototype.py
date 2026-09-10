#!/usr/bin/env python3
"""
Paper Position Manager Prototype - Live Demo

Demonstrates deterministic paper position management for volatile microcap tokens.
Uses live market data (or mock) and tracks position with full exit logic.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

from paper_position_manager import (
    PaperPosition,
    ExitConfig,
    ExitTrigger,
    PositionState,
    PriceQuote,
    EventLogger,
)
from price_source import PriceSource, MockPriceSource, PriceQuote as PriceSourceQuote
from config import PrototypeConfig, get_profile
from event_log import EventQuery


class PrototypeRunner:
    """Runs the paper trading prototype."""

    def __init__(self, config: PrototypeConfig):
        self.config = config
        self.running = False
        self.position: PaperPosition | None = None
        self.price_source: PriceSource | MockPriceSource | None = None
        self.event_logger: EventLogger | None = None
        self.update_count = 0

    async def initialize(self):
        """Initialize all components."""
        # Setup exit config
        exit_config = self.config.exit_config

        # Create position
        self.position = PaperPosition(
            mint=self.config.default_mint,
            symbol=self.config.default_symbol,
            entry_price=self.config.mock_base_price if self.config.use_mock_price else 0.0,
            entry_time=time.time(),
            size_sol=self.config.default_position_size_sol,
            config=exit_config,
        )

        # Create price source
        if self.config.use_mock_price:
            self.price_source = MockPriceSource(
                base_price=self.config.mock_base_price,
                volatility=self.config.mock_volatility,
                trend=self.config.mock_trend,
            )
        else:
            self.price_source = PriceSource()

        # Create event logger
        self.event_logger = EventLogger(self.config.log_dir)

        # Log initial events
        self.event_logger.log_position_events(self.position)

        print(f"Initialized position:")
        print(f"  Mint: {self.position.mint}")
        print(f"  Symbol: {self.position.symbol}")
        print(f"  Entry: {self.position.entry_price:.8f} SOL")
        print(f"  Size: {self.position.size_sol:.4f} SOL")
        print(f"  Tokens: {self.position.token_amount:,.0f}")
        print(f"  Initial Stop: {self.position.initial_stop_price:.8f} ({exit_config.initial_stop_pct:.1%})")
        print(f"  Trail Trigger: +{exit_config.trail_trigger_pct:.1%}")
        print(f"  Trail Distance: {exit_config.trail_distance_pct:.1%}")
        print(f"  Breakeven Trigger: +{exit_config.breakeven_trigger_pct:.1%}")
        print(f"  Breakeven Floor: +{exit_config.breakeven_floor_pct:.1%}")
        print(f"  Take Profits: {exit_config.take_profit_levels}")
        print(f"  Log Dir: {self.config.log_dir}")

    async def run_update_cycle(self):
        """Run a single update cycle."""
        if not self.position or self.position.state == PositionState.CLOSED:
            return False

        # Get current price
        if isinstance(self.price_source, MockPriceSource):
            quote = self.price_source.get_price(self.position.mint)
            price = quote.price
            liquidity = 50000 + (self.update_count * 100)  # Simulate growing liquidity
            market_cap = price * 1_000_000_000  # 1B supply
            volume = 10000
            buy_vol = 5000 + (self.update_count * 50)
            sell_vol = 3000 + (self.update_count * 30)
        else:
            quote = await self.price_source.get_price(
                self.position.mint,
                side="sell",
                size_sol=self.position.size_sol,
                is_graduated=False,
            )
            if quote is None:
                print("Failed to get price, skipping update")
                return True
            price = quote.price
            liquidity = 0  # Would need separate API call
            market_cap = 0
            volume = 0
            buy_vol = 0
            sell_vol = 0

        # Update position
        triggers = self.position.update_market_data(
            price=price,
            liquidity_usd=liquidity,
            market_cap_usd=market_cap,
            volume_24h_usd=volume,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            quote=quote,
        )

        # Log events
        self.event_logger.log_position_events(self.position)

        # Print status
        pnl_pct = (price / self.position.entry_price - 1) * 100
        state_str = self.position.state.value
        stop = self.position.trailing_stop_price or self.position.breakeven_stop_price or self.position.initial_stop_price

        print(f"[{self.update_count:4d}] Price: {price:.8f} | P&L: {pnl_pct:+.2f}% | State: {state_str:15s} | Stop: {stop:.8f} | Remaining: {self.position._get_remaining_fraction():.1%}")

        # Log trigger events
        for trigger in triggers:
            print(f"  >>> TRIGGER: {trigger.value}")

        # Handle exits
        if triggers:
            is_final = any(t in (
                ExitTrigger.INITIAL_STOP,
                ExitTrigger.TRAILING_STOP,
                ExitTrigger.BREAKEVEN_STOP,
                ExitTrigger.TIME_EXIT,
                ExitTrigger.LIQUIDITY_EXIT,
                ExitTrigger.SELL_PRESSURE_EXIT,
            ) for t in triggers)

            if is_final:
                trigger = triggers[0]
                result = self.position.close_position(price, trigger, quote)
                self.event_logger.log_position_events(self.position)

                print(f"\n{'='*60}")
                print(f"POSITION CLOSED")
                print(f"  Trigger: {trigger.value}")
                print(f"  Exit Price: {result['exit_price']:.8f}")
                print(f"  Gross P&L: {result['gross_pnl_pct']:+.2f}%")
                print(f"  Net P&L: {result['net_pnl_sol']:+.6f} SOL")
                print(f"  Hold Time: {result['hold_time_seconds']:.0f}s")
                print(f"  High Water: {result['high_water_mark']:.8f}")
                print(f"{'='*60}\n")
                return False

        self.update_count += 1
        return True

    async def run(self):
        """Main run loop."""
        self.running = True
        start_time = time.time()

        print(f"\nStarting paper trading prototype...")
        print(f"Update interval: {self.config.update_interval_seconds}s")
        if self.config.max_runtime_seconds:
            print(f"Max runtime: {self.config.max_runtime_seconds}s")
        print(f"Press Ctrl+C to stop\n")

        try:
            while self.running:
                cycle_start = time.time()

                # Check max runtime
                if self.config.max_runtime_seconds and (time.time() - start_time) >= self.config.max_runtime_seconds:
                    print("Max runtime reached")
                    break

                # Run update cycle
                continue_running = await self.run_update_cycle()
                if not continue_running:
                    break

                # Wait for next interval
                elapsed = time.time() - cycle_start
                sleep_time = max(0, self.config.update_interval_seconds - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nInterrupted by user")

        finally:
            # Final summary
            if self.position:
                print(f"\n{'='*60}")
                print(f"FINAL SUMMARY")
                print(f"  Total updates: {self.update_count}")
                print(f"  Final state: {self.position.state.value}")
                print(f"  Events logged: {len(self.position.events)}")
                print(f"  Log file: {self.config.log_dir}")
                print(f"{'='*60}")

            if self.event_logger:
                self.event_logger.close()

            # Cleanup price source
            if self.price_source:
                if hasattr(self.price_source, '_session') and self.price_source._session:
                    await self.price_source.__aexit__(None, None, None)
                elif hasattr(self.price_source, 'close'):
                    self.price_source.close()

    def stop(self):
        self.running = False


async def main():
    """Entry point."""
    # Load config from environment or use defaults
    config = PrototypeConfig.from_env()

    runner = PrototypeRunner(config)

    # Handle signals
    def signal_handler(sig, frame):
        print("\nShutdown signal received...")
        runner.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await runner.initialize()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())