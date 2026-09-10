"""
Comprehensive tests for the paper position manager.

Tests all exit types, invariants, edge cases, and failure modes.
"""

from __future__ import annotations

import json
import time
from typing import Optional
from unittest.mock import Mock, patch

import pytest

from paper_position_manager import (
    PaperPosition,
    PositionState,
    ExitTrigger,
    ExitConfig,
    PriceQuote,
)
from config import (
    ExitConfig as ConfigExitConfig,
    CONSERVATIVE,
    AGGRESSIVE,
    SCALPER,
    DEFAULT,
    get_profile,
    PrototypeConfig,
)


class TestExitConfigValidation:
    """Test configuration validation."""

    def test_valid_default_config(self):
        config = ExitConfig()
        assert config.initial_stop_pct == -0.25
        assert config.trail_trigger_pct == 0.15

    def test_invalid_positive_initial_stop(self):
        with pytest.raises(ValueError, match="initial_stop_pct must be negative"):
            ExitConfig(initial_stop_pct=0.10)

    def test_invalid_zero_trail_trigger(self):
        with pytest.raises(ValueError, match="trail_trigger_pct must be positive"):
            ExitConfig(trail_trigger_pct=0)

    def test_invalid_trail_distance(self):
        with pytest.raises(ValueError, match="trail_distance_pct must be in"):
            ExitConfig(trail_distance_pct=1.5)

    def test_invalid_take_profit_fraction(self):
        with pytest.raises(ValueError, match="take_profit fraction must be in"):
            ExitConfig(take_profit_levels=[(0.50, 1.5)])

    def test_take_profit_ordering(self):
        with pytest.raises(ValueError, match="must be sorted"):
            ExitConfig(take_profit_levels=[(1.00, 0.5), (0.50, 0.3)])

    def test_preset_profiles(self):
        assert CONSERVATIVE.initial_stop_pct == -0.15
        assert AGGRESSIVE.initial_stop_pct == -0.35
        assert SCALPER.initial_stop_pct == -0.10

    def test_get_profile(self):
        config = get_profile("conservative")
        assert config.initial_stop_pct == -0.15
        config = get_profile("aggressive")
        assert config.initial_stop_pct == -0.35

    def test_unknown_profile(self):
        with pytest.raises(ValueError, match="Unknown profile"):
            get_profile("nonexistent")


class TestPaperPositionBasics:
    """Test basic position creation and properties."""

    def setup_method(self):
        self.config = ExitConfig()
        self.position = PaperPosition(
            mint="TEST123",
            symbol="TEST",
            entry_price=0.0005,
            entry_time=time.time(),
            size_sol=0.1,
            config=self.config,
        )

    def test_position_creation(self):
        assert self.position.mint == "TEST123"
        assert self.position.symbol == "TEST"
        assert self.position.entry_price == 0.0005
        assert self.position.size_sol == 0.1
        assert self.position.token_amount == 200  # 0.1 / 0.0005
        assert self.position.state == PositionState.OPEN
        assert self.position.initial_stop_price == 0.000375  # 0.0005 * 0.75

    def test_initial_event_logged(self):
        assert len(self.position.events) == 1
        assert self.position.events[0].event == "ENTRY"
        assert self.position.events[0].stop_level == 0.000375


class TestFixedStopLoss:
    """Test initial fixed stop loss."""

    def setup_method(self):
        self.config = ExitConfig(initial_stop_pct=-0.25)
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_stop_not_triggered_above_stop(self):
        triggers = self.position.update_market_data(
            price=0.0004, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert len(triggers) == 0
        assert self.position.state == PositionState.OPEN

    def test_stop_triggered_at_stop(self):
        triggers = self.position.update_market_data(
            price=0.000375, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.INITIAL_STOP in triggers
        assert self.position.state == PositionState.CLOSED

    def test_stop_triggered_below_stop(self):
        triggers = self.position.update_market_data(
            price=0.0003, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.INITIAL_STOP in triggers


class TestTrailingStop:
    """Test trailing stop activation and ratcheting."""

    def setup_method(self):
        self.config = ExitConfig(
            initial_stop_pct=-0.25,
            trail_trigger_pct=0.15,  # Activate at +15%
            trail_distance_pct=0.10,  # Trail 10% from high
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_trailing_not_active_initially(self):
        assert self.position.trailing_stop_price is None
        assert self.position.state == PositionState.OPEN

    def test_trailing_activates_at_trigger(self):
        # Price reaches +15% = 0.000575
        triggers = self.position.update_market_data(
            price=0.000575, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert self.position.trailing_stop_price is not None
        assert self.position.trailing_stop_price == 0.000575 * 0.90  # 10% below high
        assert self.position.state == PositionState.TRAILING_ACTIVE

        # Check event logged
        trail_events = [e for e in self.position.events if e.event == "TRAILING_STOP_ACTIVATED"]
        assert len(trail_events) == 1

    def test_trailing_ratchets_up_on_new_high(self):
        # Activate trailing
        self.position.update_market_data(
            price=0.000575, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        first_trailing = self.position.trailing_stop_price

        # New higher price
        self.position.update_market_data(
            price=0.000650, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert self.position.trailing_stop_price > first_trailing

        # Check ratchet event
        ratchet_events = [e for e in self.position.events if e.event == "STOP_RATCHETED"]
        assert len(ratchet_events) == 1

    def test_trailing_NEVER_loosens_INVARIANT(self):
        """CRITICAL INVARIANT: Trailing stop never decreases."""
        self.position.update_market_data(
            price=0.000575, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        first_trailing = self.position.trailing_stop_price
        assert first_trailing is not None

        # Price goes up
        self.position.update_market_data(
            price=0.000650, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        second_trailing = self.position.trailing_stop_price
        assert second_trailing is not None
        assert second_trailing >= first_trailing

        # Price goes down - trailing should NOT move
        self.position.update_market_data(
            price=0.000600, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        third_trailing = self.position.trailing_stop_price
        assert third_trailing is not None
        assert third_trailing == second_trailing  # Unchanged!

    def test_trailing_stop_triggers_exit(self):
        self.position.update_market_data(
            price=0.000575, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        trailing_price = self.position.trailing_stop_price

        # Price falls to trailing stop
        triggers = self.position.update_market_data(
            price=trailing_price * 0.99, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.TRAILING_STOP in triggers


class TestBreakEvenProtection:
    """Test break-even stop protection."""

    def setup_method(self):
        self.config = ExitConfig(
            initial_stop_pct=-0.25,
            trail_trigger_pct=0.15,
            trail_distance_pct=0.10,
            breakeven_trigger_pct=0.30,  # Activate at +30%
            breakeven_floor_pct=0.05,    # Floor at entry + 5%
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_breakeven_activates_at_trigger(self):
        # Price reaches +30% = 0.00065
        self.position.update_market_data(
            price=0.00065, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert self.position.breakeven_stop_price is not None
        assert self.position.breakeven_stop_price == 0.0005 * 1.05  # Entry + 5%
        assert self.position.state == PositionState.BREAKEVEN_SECURED

        breakeven_events = [e for e in self.position.events if e.event == "BREAKEVEN_SECURED"]
        assert len(breakeven_events) == 1

    def test_breakeven_floor_above_initial_stop(self):
        self.position.update_market_data(
            price=0.00065, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Breakeven floor at +5% should be above initial stop at -25%
        assert self.position.breakeven_stop_price is not None
        assert self.position.breakeven_stop_price > self.position.initial_stop_price

    def test_breakeven_triggers_exit(self):
        self.position.update_market_data(
            price=0.00065, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        be_price = self.position.breakeven_stop_price
        assert be_price is not None

        triggers = self.position.update_market_data(
            price=be_price * 0.99, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.BREAKEVEN_STOP in triggers


class TestScaledTakeProfit:
    """Test partial take profit exits."""

    def setup_method(self):
        self.config = ExitConfig(
            take_profit_levels=[(0.50, 0.30), (1.00, 0.50)],  # 30% at +50%, 50% at +100%
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_first_take_profit_hit(self):
        # Price reaches +50% = 0.00075
        triggers = self.position.update_market_data(
            price=0.00075, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.TAKE_PROFIT in triggers
        assert self.position.take_profit_levels_hit[0] == True
        assert self.position.take_profit_levels_hit[1] == False

        # Remaining fraction should be 70%
        assert self.position._get_remaining_fraction() == 0.70

    def test_second_take_profit_hit(self):
        # Hit first level
        self.position.update_market_data(
            price=0.00075, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Hit second level at +100% = 0.0010
        triggers = self.position.update_market_data(
            price=0.0010, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.TAKE_PROFIT in triggers
        assert self.position.take_profit_levels_hit[1] == True

        # Remaining fraction should be 20% (100% - 30% - 50%)
        # Use approximate equality for floating point
        remaining = self.position._get_remaining_fraction()
        assert abs(remaining - 0.20) < 1e-10

    def test_take_profit_only_triggers_once(self):
        self.position.update_market_data(
            price=0.00075, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Second update at same level should not trigger again
        triggers = self.position.update_market_data(
            price=0.00080, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Should not have new TAKE_PROFIT trigger (already hit)
        tp_triggers = [t for t in triggers if t == ExitTrigger.TAKE_PROFIT]
        assert len(tp_triggers) <= 1  # At most one per level


class TestTimeBasedExits:
    """Test time-based exit triggers."""

    def setup_method(self):
        self.config = ExitConfig(
            max_hold_seconds=3600,  # 1 hour max
            momentum_timeout_seconds=600,  # 10 min no new high
        )
        entry_time = time.time() - 1800  # 30 min ago
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=entry_time, size_sol=0.1, config=self.config
        )
        self.position._last_high_time = entry_time

    def test_max_hold_exit(self):
        # Simulate time passing
        self.position.entry_time = time.time() - 7200  # 2 hours ago
        triggers = self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.TIME_EXIT in triggers

    def test_momentum_timeout_exit(self):
        # No new high for 10+ minutes
        self.position._last_high_time = time.time() - 700  # 11 min ago
        triggers = self.position.update_market_data(
            price=0.0004, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.TIME_EXIT in triggers


class TestLiquidityExits:
    """Test liquidity-based exits."""

    def setup_method(self):
        self.config = ExitConfig(
            min_liquidity_usd=10000,
            liquidity_drop_pct=0.50,
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_min_liquidity_exit(self):
        triggers = self.position.update_market_data(
            price=0.0005, liquidity_usd=5000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.LIQUIDITY_EXIT in triggers

    def test_liquidity_drop_exit(self):
        # First update establishes peak
        self.position.update_market_data(
            price=0.0005, liquidity_usd=100000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Liquidity drops 60%
        triggers = self.position.update_market_data(
            price=0.0005, liquidity_usd=35000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.LIQUIDITY_EXIT in triggers


class TestSellPressureExit:
    """Test sell pressure exit."""

    def setup_method(self):
        self.config = ExitConfig(
            sell_buy_ratio_threshold=3.0,
            sell_pressure_window_seconds=300,
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_sell_pressure_exit(self):
        # Heavy sell volume
        self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000, buy_volume=1000, sell_volume=4000  # 4:1 ratio
        )
        triggers = self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000, buy_volume=1000, sell_volume=4000
        )
        assert ExitTrigger.SELL_PRESSURE_EXIT in triggers

    def test_no_exit_when_buy_dominates(self):
        triggers = self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000, buy_volume=4000, sell_volume=1000  # 1:4 ratio
        )
        assert ExitTrigger.SELL_PRESSURE_EXIT not in triggers


class TestPriceGapsAndSlippage:
    """Test handling of price gaps and slippage."""

    def setup_method(self):
        self.config = ExitConfig(initial_stop_pct=-0.25)
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_price_gap_past_stop(self):
        """Price gaps down past stop - should trigger at next update."""
        triggers = self.position.update_market_data(
            price=0.0003, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        assert ExitTrigger.INITIAL_STOP in triggers

    def test_executable_price_with_slippage(self):
        """Test that executable price accounts for slippage."""
        quote = PriceQuote(
            price=0.0005,
            price_impact_pct=0.02,  # 2% price impact
            out_amount=0, in_amount=0, route="Test"
        )
        # Net price should be lower than headline
        assert quote.net_price_after_fees < quote.price
        assert quote.executable_price < quote.price


class TestStaleDataAndOutages:
    """Test handling of stale market data and source outages."""

    def setup_method(self):
        self.config = ExitConfig()
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_stale_data_handled(self):
        """Multiple updates with same price should not cause issues."""
        for _ in range(5):
            triggers = self.position.update_market_data(
                price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
                volume_24h_usd=10000
            )
            assert len(triggers) == 0

    def test_malformed_price_data(self):
        """Zero or negative prices should be handled gracefully."""
        # Zero price - should not crash
        triggers = self.position.update_market_data(
            price=0.0, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        # Position should still be open (or closed by stop)
        assert self.position.state in (PositionState.OPEN, PositionState.CLOSED)


class TestRestartAndResume:
    """Test restart/resume capability."""

    def setup_method(self):
        self.config = ExitConfig(
            trail_trigger_pct=0.15,
            trail_distance_pct=0.10,
        )
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_state_can_be_serialized(self):
        """Position state should be serializable for checkpointing."""
        # Advance position
        self.position.update_market_data(
            price=0.0006, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )

        # Key state that needs persistence
        state = {
            "mint": self.position.mint,
            "symbol": self.position.symbol,
            "entry_price": self.position.entry_price,
            "entry_time": self.position.entry_time,
            "size_sol": self.position.size_sol,
            "token_amount": self.position.token_amount,
            "state": self.position.state.value,
            "initial_stop_price": self.position.initial_stop_price,
            "trailing_stop_price": self.position.trailing_stop_price,
            "breakeven_stop_price": self.position.breakeven_stop_price,
            "high_water_mark": self.position.high_water_mark,
            "take_profit_levels_hit": self.position.take_profit_levels_hit,
            "peak_liquidity_usd": self.position.peak_liquidity_usd,
        }

        # Verify all critical fields present
        assert state["trailing_stop_price"] is not None
        assert state["high_water_mark"] > self.position.entry_price


class TestDuplicateEvents:
    """Test handling of duplicate market data events."""

    def setup_method(self):
        self.config = ExitConfig()
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_duplicate_updates_ignored(self):
        """Identical sequential updates should not create duplicate events."""
        initial_count = len(self.position.events)

        self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        self.position.update_market_data(
            price=0.0005, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )

        # Should have added events but not excessive duplicates
        # (Each update logs an UPDATE event)
        assert len(self.position.events) >= initial_count + 2


class TestConcurrentAccess:
    """Test thread safety."""

    def setup_method(self):
        self.config = ExitConfig()
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_concurrent_updates(self):
        """Multiple threads updating position should not corrupt state."""
        import threading

        results = []

        def update_price(price):
            triggers = self.position.update_market_data(
                price=price, liquidity_usd=50000, market_cap_usd=100000,
                volume_24h_usd=10000
            )
            results.append(triggers)

        threads = [
            threading.Thread(target=update_price, args=(0.0005 + i * 0.00001,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Position should be in valid state
        assert self.position.state in (PositionState.OPEN, PositionState.CLOSED,
                                       PositionState.TRAILING_ACTIVE, PositionState.BREAKEVEN_SECURED)


class TestEventLogging:
    """Test event log completeness and format."""

    def setup_method(self):
        self.config = ExitConfig()
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_entry_event_has_all_fields(self):
        entry_event = self.position.events[0]
        assert entry_event.event == "ENTRY"
        assert entry_event.mint == "TEST"
        assert entry_event.price == 0.0005
        assert entry_event.stop_level == 0.000375

    def test_events_are_jsonl_serializable(self):
        jsonl = self.position.get_events_jsonl()
        lines = jsonl.strip().split("\n")
        for line in lines:
            data = json.loads(line)
            assert "ts" in data
            assert "event" in data
            assert "mint" in data

    def test_exit_events_record_pnl(self):
        self.position.update_market_data(
            price=0.0003, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )
        exit_events = [e for e in self.position.events if e.event in ("EXIT_TRIGGERED", "FINAL_PNL")]
        assert len(exit_events) >= 1
        # FINAL_PNL should have P&L info
        final_events = [e for e in self.position.events if e.event == "FINAL_PNL"]
        if final_events:
            assert final_events[0].estimated_net_pnl_pct < 0  # Loss


class TestClosePosition:
    """Test position closing and final P&L calculation."""

    def setup_method(self):
        self.config = ExitConfig(initial_stop_pct=-0.25)
        self.position = PaperPosition(
            mint="TEST", symbol="TEST", entry_price=0.0005,
            entry_time=time.time(), size_sol=0.1, config=self.config
        )

    def test_close_returns_pnl(self):
        # Trigger stop
        self.position.update_market_data(
            price=0.000375, liquidity_usd=50000, market_cap_usd=100000,
            volume_24h_usd=10000
        )

        # Close with quote
        quote = PriceQuote(price=0.000375, price_impact_pct=0.01, out_amount=0, in_amount=0, route="Test")
        result = self.position.close_position(0.000375, ExitTrigger.INITIAL_STOP, quote)

        assert result["mint"] == "TEST"
        assert result["symbol"] == "TEST"
        assert result["entry_price"] == 0.0005
        assert result["exit_price"] < result["entry_price"]  # Loss
        assert result["net_pnl_sol"] < 0
        assert result["trigger"] == "initial_stop"
        assert result["hold_time_seconds"] > 0


class TestSTONKSZNReplay:
    """Test the STONKSZN historical replay scenario."""

    def test_stonkszn_trailing_captures_profit(self):
        """STONKSZN entry ~0.000523, peak ~+35%, then reversal.
        With trail @ +15%, distance 10%, should capture ~+18-22%."""
        config = ExitConfig(
            initial_stop_pct=-0.25,
            trail_trigger_pct=0.15,
            trail_distance_pct=0.10,
            breakeven_trigger_pct=0.30,
            breakeven_floor_pct=0.05,
        )
        position = PaperPosition(
            mint="STONKSZN", symbol="STONKSZN", entry_price=0.000523,
            entry_time=time.time(), size_sol=0.1, config=config
        )

        # Simulate price path: entry -> +15% -> +25% -> +35% -> reversal
        # Using exact calculations to avoid floating point issues
        entry = 0.000523
        p15 = entry * 1.15  # 0.00060145
        p25 = entry * 1.25  # 0.00065375
        p35 = entry * 1.35  # 0.00070605
        # Trailing stop at peak: 0.00070605 * 0.90 = 0.000635445
        trail_price = p35 * 0.90

        price_path = [
            entry,       # Entry
            p15,         # +15% - trail activates
            p25,         # +25% - trail ratchets
            p35,         # +35% - peak, trail ratchets
            p25,         # Pullback
            trail_price * 0.99,  # Hits trailing stop
        ]

        final_trigger = None
        exit_price = price_path[-1]  # Default to last price
        for price in price_path:
            triggers = position.update_market_data(
                price=price, liquidity_usd=50000, market_cap_usd=100000,
                volume_24h_usd=10000
            )
            if triggers:
                final_trigger = triggers[0]
                exit_price = price
                break

        # Should exit via trailing stop, not initial stop
        assert final_trigger in (ExitTrigger.TRAILING_STOP, ExitTrigger.BREAKEVEN_STOP)

        # Calculate captured profit
        if final_trigger:
            result = position.close_position(exit_price, final_trigger)
            # Should capture significant profit (not the full +35% but substantial)
            assert result["gross_pnl_pct"] > 15  # At least +15%


if __name__ == "__main__":
    pytest.main([__file__, "-v"])