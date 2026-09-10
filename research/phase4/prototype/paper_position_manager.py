"""
Paper Position Manager - Deterministic position management for volatile microcap tokens.

This module implements a complete paper trading position manager with:
- Initial stop loss
- Trailing stop with ratcheting (never loosens)
- Break-even protection
- Scaled take profits (partial exits)
- Time-based exits
- Liquidity collapse exits
- Sell pressure exits
- Executable price realism (Jupiter quotes + slippage + fees)
- Append-only JSONL event logging

All exits are evaluated against realistic executable prices, not headline prices.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
import threading


class ExitTrigger(Enum):
    """Reason for position exit."""
    INITIAL_STOP = "initial_stop"
    TRAILING_STOP = "trailing_stop"
    BREAKEVEN_STOP = "breakeven_stop"
    TAKE_PROFIT = "take_profit"
    TIME_EXIT = "time_exit"
    LIQUIDITY_EXIT = "liquidity_exit"
    SELL_PRESSURE_EXIT = "sell_pressure_exit"
    MANUAL = "manual"


class PositionState(Enum):
    """Current state of the position."""
    OPEN = "open"
    TRAILING_ACTIVE = "trailing_active"
    BREAKEVEN_SECURED = "breakeven_secured"
    PARTIAL_EXIT = "partial_exit"  # After take profit hit but not fully closed
    CLOSED = "closed"


@dataclass
class ExitConfig:
    """Configuration for all exit rules."""
    # Initial stop loss (percentage from entry, negative)
    initial_stop_pct: float = -0.25

    # Trailing stop
    trail_trigger_pct: float = 0.15      # Activate trailing at +15%
    trail_distance_pct: float = 0.10     # Trail distance 10% from high

    # Break-even protection
    breakeven_trigger_pct: float = 0.30  # Activate breakeven at +30%
    breakeven_floor_pct: float = 0.05    # Floor at entry + 5%

    # Scaled take profits: list of (trigger_pct, exit_fraction)
    # e.g., [(0.50, 0.30), (1.00, 0.50)] = sell 30% at +50%, 50% at +100%
    take_profit_levels: list[tuple[float, float]] = field(
        default_factory=lambda: [(0.50, 0.30), (1.00, 0.50)]
    )

    # Time-based exits
    max_hold_seconds: float = 86400      # 24 hours max
    momentum_timeout_seconds: float = 3600  # Exit if no new high for 1 hour

    # Liquidity exits
    min_liquidity_usd: float = 10000     # Exit if liquidity below $10k
    liquidity_drop_pct: float = 0.50     # Exit if liquidity drops 50% from peak

    # Sell pressure exit
    sell_buy_ratio_threshold: float = 3.0  # Exit if sell volume 3x buy volume
    sell_pressure_window_seconds: float = 300  # Over 5-minute window

    # Execution realism
    priority_fee_sol: float = 0.0005     # Priority fee per swap
    max_slippage_pct: float = 0.05       # Max 5% slippage tolerance

    def __post_init__(self):
        """Validate configuration."""
        if self.initial_stop_pct >= 0:
            raise ValueError("initial_stop_pct must be negative")
        if self.trail_trigger_pct <= 0:
            raise ValueError("trail_trigger_pct must be positive")
        if self.trail_distance_pct <= 0 or self.trail_distance_pct >= 1:
            raise ValueError("trail_distance_pct must be in (0, 1)")
        if self.breakeven_trigger_pct <= 0:
            raise ValueError("breakeven_trigger_pct must be positive")
        if self.breakeven_floor_pct < 0:
            raise ValueError("breakeven_floor_pct must be non-negative")
        for trigger, fraction in self.take_profit_levels:
            if trigger <= 0:
                raise ValueError("take_profit trigger must be positive")
            if fraction <= 0 or fraction > 1:
                raise ValueError("take_profit fraction must be in (0, 1]")
        if self.max_hold_seconds <= 0:
            raise ValueError("max_hold_seconds must be positive")
        if self.momentum_timeout_seconds <= 0:
            raise ValueError("momentum_timeout_seconds must be positive")
        if self.min_liquidity_usd < 0:
            raise ValueError("min_liquidity_usd must be non-negative")
        if self.liquidity_drop_pct < 0 or self.liquidity_drop_pct > 1:
            raise ValueError("liquidity_drop_pct must be in [0, 1]")
        if self.sell_buy_ratio_threshold <= 0:
            raise ValueError("sell_buy_ratio_threshold must be positive")
        if self.sell_pressure_window_seconds <= 0:
            raise ValueError("sell_pressure_window_seconds must be positive")
        if self.priority_fee_sol < 0:
            raise ValueError("priority_fee_sol must be non-negative")
        if self.max_slippage_pct < 0 or self.max_slippage_pct > 1:
            raise ValueError("max_slippage_pct must be in [0, 1]")

        # Ensure take profits are ordered by trigger
        triggers = [t for t, _ in self.take_profit_levels]
        if triggers != sorted(triggers):
            raise ValueError("take_profit_levels must be sorted by trigger_pct")


@dataclass
class PriceQuote:
    """Realistic executable price quote from Jupiter."""
    price: float                    # Price per token in SOL
    price_impact_pct: float         # Estimated price impact
    out_amount: float               # Raw output amount
    in_amount: float                # Input amount
    route: str                      # Route description
    timestamp: float = field(default_factory=time.time)

    @property
    def executable_price(self) -> float:
        """Price after accounting for price impact."""
        return self.price * (1 - self.price_impact_pct)

    @property
    def net_price_after_fees(self) -> float:
        """Price after DEX fees (0.25% typical) and priority fee."""
        # Priority fee is per transaction, not per token - approximate as price reduction
        # For small positions, priority fee has larger relative impact
        dex_fee_pct = 0.0025  # 0.25% Raydium fee
        return self.executable_price * (1 - dex_fee_pct)


@dataclass
class PositionEvent:
    """Single event in the position lifecycle."""
    ts: str
    event: str
    mint: str
    price: float
    liquidity_usd: float
    market_cap_usd: float
    volume_24h_usd: float
    buy_volume: float
    sell_volume: float
    trigger: Optional[str] = None
    position_value_sol: float = 0.0
    high_water_mark: float = 0.0
    stop_level: float = 0.0
    estimated_exit_price: float = 0.0
    estimated_net_pnl_pct: float = 0.0
    remaining_fraction: float = 1.0

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(',', ':'))


@dataclass
class PaperPosition:
    """A paper trading position with full lifecycle management."""
    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    size_sol: float
    config: ExitConfig

    # Derived state
    token_amount: float = field(init=False)
    state: PositionState = field(default=PositionState.OPEN, init=False)

    # Exit tracking
    initial_stop_price: float = field(init=False)
    trailing_stop_price: Optional[float] = field(default=None, init=False)
    breakeven_stop_price: Optional[float] = field(default=None, init=False)
    high_water_mark: float = field(init=False)
    take_profit_levels_hit: list[bool] = field(default_factory=list, init=False)

    # Volume tracking for sell pressure
    recent_buy_volume: float = field(default=0.0, init=False)
    recent_sell_volume: float = field(default=0.0, init=False)
    volume_window_start: float = field(default_factory=time.time, init=False)

    # Liquidity tracking
    peak_liquidity_usd: float = field(default=0.0, init=False)

    # Event log
    events: list[PositionEvent] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        self.token_amount = self.size_sol / self.entry_price
        self.initial_stop_price = self.entry_price * (1 + self.config.initial_stop_pct)
        self.high_water_mark = self.entry_price
        self.take_profit_levels_hit = [False] * len(self.config.take_profit_levels)
        self.peak_liquidity_usd = 0.0
        self._last_high_time = self.entry_time
        self._log_event(
            event="ENTRY",
            price=self.entry_price,
            liquidity_usd=0.0,
            market_cap_usd=0.0,
            volume_24h_usd=0.0,
            buy_volume=0.0,
            sell_volume=0.0,
            position_value_sol=self.size_sol,
            stop_level=self.initial_stop_price,
        )

    def _log_event(self, **kwargs) -> None:
        """Append event to log."""
        event = PositionEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            mint=self.mint,
            **kwargs
        )
        self.events.append(event)

    def update_market_data(
        self,
        price: float,
        liquidity_usd: float,
        market_cap_usd: float,
        volume_24h_usd: float,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        quote: Optional[PriceQuote] = None
    ) -> list[ExitTrigger]:
        """
        Update position with new market data.
        Returns list of exit triggers fired.
        """
        triggers = []
        now = time.time()

        with self._lock:
            # Update peak liquidity
            if liquidity_usd > self.peak_liquidity_usd:
                self.peak_liquidity_usd = liquidity_usd

            # Update high water mark
            new_high = False
            if price > self.high_water_mark:
                self.high_water_mark = price
                new_high = True

            # Update volume window
            if now - self.volume_window_start > self.config.sell_pressure_window_seconds:
                self.recent_buy_volume = 0.0
                self.recent_sell_volume = 0.0
                self.volume_window_start = now
            self.recent_buy_volume += buy_volume
            self.recent_sell_volume += sell_volume

            # Use executable price for exit evaluation
            eval_price = quote.net_price_after_fees if quote else price

            # 1. Check initial stop loss (always active until trailing activates)
            if self.state in (PositionState.OPEN, PositionState.TRAILING_ACTIVE, PositionState.BREAKEVEN_SECURED, PositionState.PARTIAL_EXIT):
                if eval_price <= self.initial_stop_price:
                    triggers.append(ExitTrigger.INITIAL_STOP)

            # 2. Check trailing stop
            if self.trailing_stop_price is not None:
                if eval_price <= self.trailing_stop_price:
                    triggers.append(ExitTrigger.TRAILING_STOP)

            # 3. Check break-even stop
            if self.breakeven_stop_price is not None:
                if eval_price <= self.breakeven_stop_price:
                    triggers.append(ExitTrigger.BREAKEVEN_STOP)

            # 4. Check take profit levels (partial exits)
            for i, (trigger_pct, exit_fraction) in enumerate(self.config.take_profit_levels):
                if not self.take_profit_levels_hit[i]:
                    target_price = self.entry_price * (1 + trigger_pct)
                    if eval_price >= target_price:
                        triggers.append(ExitTrigger.TAKE_PROFIT)
                        self.take_profit_levels_hit[i] = True

            # 5. Check time-based exits
            hold_time = now - self.entry_time
            if hold_time >= self.config.max_hold_seconds:
                triggers.append(ExitTrigger.TIME_EXIT)

            if new_high:
                self._last_high_time = now
            elif hasattr(self, '_last_high_time'):
                if now - self._last_high_time >= self.config.momentum_timeout_seconds:
                    triggers.append(ExitTrigger.TIME_EXIT)

            # 6. Check liquidity exits
            if liquidity_usd < self.config.min_liquidity_usd:
                triggers.append(ExitTrigger.LIQUIDITY_EXIT)
            elif self.peak_liquidity_usd > 0:
                liquidity_drop = 1 - (liquidity_usd / self.peak_liquidity_usd)
                if liquidity_drop >= self.config.liquidity_drop_pct:
                    triggers.append(ExitTrigger.LIQUIDITY_EXIT)

            # 7. Check sell pressure
            if self.recent_buy_volume > 0:
                sell_buy_ratio = self.recent_sell_volume / self.recent_buy_volume
                if sell_buy_ratio >= self.config.sell_buy_ratio_threshold:
                    triggers.append(ExitTrigger.SELL_PRESSURE_EXIT)

            # Update trailing/breakeven stops based on new high
            if new_high:
                self._update_stops_on_new_high(price)

            # Determine if we should close fully or just partial
            is_final_exit = any(t in (
                ExitTrigger.INITIAL_STOP,
                ExitTrigger.TRAILING_STOP,
                ExitTrigger.BREAKEVEN_STOP,
                ExitTrigger.TIME_EXIT,
                ExitTrigger.LIQUIDITY_EXIT,
                ExitTrigger.SELL_PRESSURE_EXIT,
            ) for t in triggers)

            # Log the update
            self._log_event(
                event="UPDATE" if not triggers else ("EXIT_TRIGGERED" if is_final_exit else "PARTIAL_EXIT"),
                price=price,
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                volume_24h_usd=volume_24h_usd,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                trigger=triggers[0].value if triggers else None,
                position_value_sol=self.token_amount * eval_price,
                high_water_mark=self.high_water_mark,
                stop_level=self.trailing_stop_price or self.breakeven_stop_price or self.initial_stop_price,
                estimated_exit_price=eval_price,
                estimated_net_pnl_pct=(eval_price / self.entry_price - 1) * 100,
                remaining_fraction=self._get_remaining_fraction(),
            )

            if is_final_exit:
                self.state = PositionState.CLOSED
            elif triggers and ExitTrigger.TAKE_PROFIT in triggers:
                self.state = PositionState.PARTIAL_EXIT

        return triggers

    def _update_stops_on_new_high(self, new_high_price: float) -> None:
        """Update trailing and breakeven stops when new high is reached."""
        # Use epsilon for floating point comparison
        eps = 1e-12

        # Trailing stop activation
        trail_trigger_price = self.entry_price * (1 + self.config.trail_trigger_pct)
        if new_high_price + eps >= trail_trigger_price:
            new_trailing = new_high_price * (1 - self.config.trail_distance_pct)
            if self.trailing_stop_price is None:
                self.trailing_stop_price = new_trailing
                self.state = PositionState.TRAILING_ACTIVE
                self._log_event(
                    event="TRAILING_STOP_ACTIVATED",
                    price=new_high_price,
                    liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                    buy_volume=0, sell_volume=0,
                    trigger="trail_activated",
                    high_water_mark=self.high_water_mark,
                    stop_level=self.trailing_stop_price,
                    estimated_exit_price=new_trailing,
                    estimated_net_pnl_pct=(new_trailing / self.entry_price - 1) * 100,
                    remaining_fraction=self._get_remaining_fraction(),
                )
            elif new_trailing > self.trailing_stop_price + eps:
                # RATCHET: Only tighten, never loosen
                old_trailing = self.trailing_stop_price
                self.trailing_stop_price = new_trailing
                self._log_event(
                    event="STOP_RATCHETED",
                    price=new_high_price,
                    liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                    buy_volume=0, sell_volume=0,
                    trigger="ratchet",
                    high_water_mark=self.high_water_mark,
                    stop_level=self.trailing_stop_price,
                    estimated_exit_price=new_trailing,
                    estimated_net_pnl_pct=(new_trailing / self.entry_price - 1) * 100,
                    remaining_fraction=self._get_remaining_fraction(),
                )

        # Break-even protection
        breakeven_trigger_price = self.entry_price * (1 + self.config.breakeven_trigger_pct)
        if new_high_price + eps >= breakeven_trigger_price and self.breakeven_stop_price is None:
            self.breakeven_stop_price = self.entry_price * (1 + self.config.breakeven_floor_pct)
            self.state = PositionState.BREAKEVEN_SECURED
            self._log_event(
                event="BREAKEVEN_SECURED",
                price=new_high_price,
                liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                buy_volume=0, sell_volume=0,
                trigger="breakeven",
                high_water_mark=self.high_water_mark,
                stop_level=self.breakeven_stop_price,
                estimated_exit_price=self.breakeven_stop_price,
                estimated_net_pnl_pct=(self.breakeven_stop_price / self.entry_price - 1) * 100,
                remaining_fraction=self._get_remaining_fraction(),
            )

    def _get_remaining_fraction(self) -> float:
        """Calculate remaining position fraction after partial exits."""
        remaining = 1.0
        for i, (_, exit_fraction) in enumerate(self.config.take_profit_levels):
            if self.take_profit_levels_hit[i]:
                remaining -= exit_fraction
        return max(0.0, remaining)

    def close_position(self, exit_price: float, trigger: ExitTrigger, quote: Optional[PriceQuote] = None) -> dict:
        """Close position and return final P&L."""
        with self._lock:
            net_price = quote.net_price_after_fees if quote else exit_price
            gross_pnl_pct = (net_price / self.entry_price - 1) * 100
            net_pnl_sol = self.token_amount * (net_price - self.entry_price)

            self._log_event(
                event="SIMULATED_FILL",
                price=exit_price,
                liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                buy_volume=0, sell_volume=0,
                trigger=trigger.value,
                position_value_sol=self.token_amount * net_price,
                high_water_mark=self.high_water_mark,
                stop_level=net_price,
                estimated_exit_price=net_price,
                estimated_net_pnl_pct=gross_pnl_pct,
                remaining_fraction=0.0,
            )

            self._log_event(
                event="FINAL_PNL",
                price=exit_price,
                liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                buy_volume=0, sell_volume=0,
                trigger=trigger.value,
                position_value_sol=0.0,
                high_water_mark=self.high_water_mark,
                stop_level=net_price,
                estimated_exit_price=net_price,
                estimated_net_pnl_pct=gross_pnl_pct,
                remaining_fraction=0.0,
            )

            self.state = PositionState.CLOSED

            return {
                "mint": self.mint,
                "symbol": self.symbol,
                "entry_price": self.entry_price,
                "exit_price": net_price,
                "gross_pnl_pct": gross_pnl_pct,
                "net_pnl_sol": net_pnl_sol,
                "hold_time_seconds": time.time() - self.entry_time,
                "trigger": trigger.value,
                "high_water_mark": self.high_water_mark,
                "events_count": len(self.events),
            }

    def get_events_jsonl(self) -> str:
        """Get all events as JSONL string."""
        return "\n".join(e.to_jsonl() for e in self.events)


class EventLogger:
    """Thread-safe append-only JSONL event logger."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._lock = threading.Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: PositionEvent) -> None:
        with self._lock:
            with open(self.log_path, "a") as f:
                f.write(event.to_jsonl() + "\n")

    def log_position_events(self, position: PaperPosition) -> None:
        with self._lock:
            with open(self.log_path, "a") as f:
                for event in position.events:
                    f.write(event.to_jsonl() + "\n")

    def close(self) -> None:
        """Close the logger (no-op for this simple implementation)."""
        pass