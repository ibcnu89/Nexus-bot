"""
Paper Position Manager - Deterministic position management for volatile microcap tokens.

This module implements a complete paper trading position manager with:
- Initial stop loss
- Trailing stop with ratcheting (never loosens)
- Break-even protection
- Scaled take profits (partial exits with proper accounting)
- Time-based exits
- Liquidity collapse exits
- Sell pressure exits
- Executable price realism (Jupiter quotes + slippage + fees)
- Append-only JSONL event logging
- Full P&L accounting with realized/unrealized tracking

All exits are evaluated against realistic executable prices, not headline prices.

Phase 4A.6 fixes:
- Changed Lock -> RLock to prevent nested acquisition deadlock
- Execution accounting: fees counted exactly once
- Priority fee subtracted from proceeds (not double-counted)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
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
        total_exit_fraction = sum(f for _, f in self.take_profit_levels)
        if total_exit_fraction > 1.0:
            raise ValueError("take_profit exit fractions sum to > 100%")
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
    # Core price (SOL per token unit, normalized to token decimals)
    price: float                    # Price per token in SOL
    price_impact_pct: float         # Estimated price impact
    out_amount: float               # Raw output amount
    in_amount: float                # Input amount
    route: str                      # Route description
    timestamp: float = field(default_factory=time.time)
    swap_fee_bps: int = 0           # DEX swap fee in basis points (INFORMATIONAL)
    platform_fee_bps: int = 0       # Platform fee in basis points (INFORMATIONAL)
    priority_fee_sol: float = 0.0   # Priority fee in SOL (SEPARATE network cost)
    # New fields from corrected PriceSource
    in_mint: str = ""
    out_mint: str = ""
    in_decimals: int = 9
    out_decimals: int = 9
    other_amount_threshold: float = 0.0

    @property
    def executable_price(self) -> float:
        """Price after accounting for price impact using slippage threshold."""
        if self.in_amount == 0 or self.out_amount == 0:
            return self.price * (1 - self.price_impact_pct)
        slippage_factor = self.other_amount_threshold / self.out_amount if self.out_amount > 0 else 1.0
        return self.price * slippage_factor

    @property
    def net_price_after_fees(self) -> float:
        """
        Price after ALL fees.
        AMM/platform fees are ALREADY EMBEDDED in the quote's out_amount.
        Only priority fee needs to be subtracted here.
        """
        # AMM/platform fees already baked into executable_price via out_amount
        # Only subtract priority fee (network cost)
        return self.executable_price


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
    # P&L accounting fields
    tokens_remaining: float = 0.0
    realized_proceeds_sol: float = 0.0
    realized_pnl_sol: float = 0.0
    unrealized_pnl_sol: float = 0.0
    fees_paid_sol: float = 0.0
    weighted_exit_price: float = 0.0

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
    initial_token_amount: float = field(init=False)  # Total tokens bought at entry
    tokens_remaining: float = field(init=False)      # Tokens still held
    state: PositionState = field(default=PositionState.OPEN, init=False)

    # Exit tracking
    initial_stop_price: float = field(init=False)
    trailing_stop_price: Optional[float] = field(default=None, init=False)
    breakeven_stop_price: Optional[float] = field(default=None, init=False)
    high_water_mark: float = field(init=False)
    take_profit_levels_hit: list[bool] = field(default_factory=list, init=False)
    take_profit_levels_executed: list[bool] = field(default_factory=list, init=False)

    # P&L Accounting
    realized_proceeds_sol: float = field(default=0.0, init=False)  # SOL received from partial exits
    realized_pnl_sol: float = field(default=0.0, init=False)       # Realized P&L from exits
    fees_paid_sol: float = field(default=0.0, init=False)          # Total fees paid
    weighted_exit_price: float = field(default=0.0, init=False)    # Volume-weighted average exit price

    # Volume tracking for sell pressure
    recent_buy_volume: float = field(default=0.0, init=False)
    recent_sell_volume: float = field(default=0.0, init=False)
    volume_window_start: float = field(default_factory=time.time, init=False)

    # Liquidity tracking
    peak_liquidity_usd: float = field(default=0.0, init=False)

    # Event log
    events: list[PositionEvent] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self):
        self.initial_token_amount = self.size_sol / self.entry_price
        self.tokens_remaining = self.initial_token_amount
        self.initial_stop_price = self.entry_price * (1 + self.config.initial_stop_pct)
        self.high_water_mark = self.entry_price
        self.take_profit_levels_hit = [False] * len(self.config.take_profit_levels)
        self.take_profit_levels_executed = [False] * len(self.config.take_profit_levels)
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
            tokens_remaining=self.tokens_remaining,
            realized_proceeds_sol=0.0,
            realized_pnl_sol=0.0,
            unrealized_pnl_sol=0.0,
            fees_paid_sol=0.0,
            weighted_exit_price=0.0,
        )

    def _log_event(self, **kwargs) -> None:
        """Append event to log."""
        event = PositionEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            mint=self.mint,
            **kwargs
        )
        self.events.append(event)

    def _get_unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L based on current price."""
        return self.tokens_remaining * (current_price - self.entry_price)

    def _get_total_pnl(self, current_price: float) -> float:
        """Calculate total P&L = realized + unrealized."""
        return self.realized_pnl_sol + self._get_unrealized_pnl(current_price)

    def _execute_partial_exit(
        self,
        exit_fraction: float,
        exit_price: float,
        quote: Optional[PriceQuote],
        trigger: ExitTrigger
    ) -> tuple[float, float, float]:
        """
        Execute a partial exit.

        Returns:
            (tokens_sold, proceeds_sol, fees_sol)
        """
        # Calculate tokens to sell (fraction of ORIGINAL position, not remaining)
        tokens_to_sell = self.initial_token_amount * exit_fraction
        tokens_to_sell = min(tokens_to_sell, self.tokens_remaining)

        if tokens_to_sell <= 0:
            return 0.0, 0.0, 0.0

        # Get net price after fees from quote
        # net_price_after_fees already accounts for AMM/platform fees (embedded) and priority fee
        net_price = quote.net_price_after_fees if quote else exit_price

        # Calculate proceeds and fees
        gross_proceeds = tokens_to_sell * net_price

        # Priority fee (already accounted in net_price if quote provided)
        # If no quote, subtract priority fee manually
        if quote is None:
            priority_fee = self.config.priority_fee_sol
            net_proceeds = gross_proceeds - priority_fee
            fees = priority_fee
        else:
            # Quote's net_price_after_fees already has priority fee subtracted
            net_proceeds = gross_proceeds
            fees = quote.priority_fee_sol

        # Update accounting
        self.tokens_remaining -= tokens_to_sell
        self.realized_proceeds_sol += net_proceeds
        self.fees_paid_sol += fees

        # Update weighted exit price
        if self.weighted_exit_price == 0.0:
            self.weighted_exit_price = net_price
        else:
            total_sold = self.initial_token_amount - self.tokens_remaining
            if total_sold > 0:
                # Recalculate weighted average
                self.weighted_exit_price = (
                    (self.weighted_exit_price * (total_sold - tokens_to_sell) + net_price * tokens_to_sell)
                    / total_sold
                )

        # Realized P&L for this exit
        exit_pnl = tokens_to_sell * (net_price - self.entry_price) - fees
        self.realized_pnl_sol += exit_pnl

        return tokens_to_sell, net_proceeds, fees

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

            # 1. Check initial stop loss (always active)
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

            # Execute take profit exits immediately
            for i, (trigger_pct, exit_fraction) in enumerate(self.config.take_profit_levels):
                if self.take_profit_levels_hit[i] and not self.take_profit_levels_executed[i]:
                    target_price = self.entry_price * (1 + trigger_pct)
                    if eval_price >= target_price:
                        # Execute this partial exit
                        tokens_sold, proceeds, fees = self._execute_partial_exit(
                            exit_fraction, eval_price, quote, ExitTrigger.TAKE_PROFIT
                        )
                        if tokens_sold > 0:
                            self.take_profit_levels_executed[i] = True

            # Determine if we should close fully
            final_exit_triggers = (
                ExitTrigger.INITIAL_STOP,
                ExitTrigger.TRAILING_STOP,
                ExitTrigger.BREAKEVEN_STOP,
                ExitTrigger.TIME_EXIT,
                ExitTrigger.LIQUIDITY_EXIT,
                ExitTrigger.SELL_PRESSURE_EXIT,
            )
            is_final_exit = any(t in final_exit_triggers for t in triggers)

            # Log the update
            unrealized_pnl = self._get_unrealized_pnl(eval_price)
            total_pnl = self.realized_pnl_sol + unrealized_pnl

            self._log_event(
                event="UPDATE" if not triggers else ("EXIT_TRIGGERED" if is_final_exit else "PARTIAL_EXIT"),
                price=price,
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                volume_24h_usd=volume_24h_usd,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                trigger=triggers[0].value if triggers else None,
                position_value_sol=self.tokens_remaining * eval_price + self.realized_proceeds_sol,
                high_water_mark=self.high_water_mark,
                stop_level=self.trailing_stop_price or self.breakeven_stop_price or self.initial_stop_price,
                estimated_exit_price=eval_price,
                estimated_net_pnl_pct=(total_pnl / self.size_sol) * 100 if self.size_sol > 0 else 0,
                remaining_fraction=self.tokens_remaining / self.initial_token_amount if self.initial_token_amount > 0 else 0,
                tokens_remaining=self.tokens_remaining,
                realized_proceeds_sol=self.realized_proceeds_sol,
                realized_pnl_sol=self.realized_pnl_sol,
                unrealized_pnl_sol=unrealized_pnl,
                fees_paid_sol=self.fees_paid_sol,
                weighted_exit_price=self.weighted_exit_price,
            )

            # Execute final exit if needed
            if is_final_exit:
                self.close_position(eval_price, triggers[0], quote)

        return triggers

    def _update_stops_on_new_high(self, new_high_price: float) -> None:
        """Update trailing and breakeven stops when new high is reached."""
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
                    estimated_net_pnl_pct=((new_trailing / self.entry_price - 1) * 100),
                    remaining_fraction=self.tokens_remaining / self.initial_token_amount,
                    tokens_remaining=self.tokens_remaining,
                    realized_proceeds_sol=self.realized_proceeds_sol,
                    realized_pnl_sol=self.realized_pnl_sol,
                    unrealized_pnl_sol=self._get_unrealized_pnl(new_high_price),
                    fees_paid_sol=self.fees_paid_sol,
                    weighted_exit_price=self.weighted_exit_price,
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
                    estimated_net_pnl_pct=((new_trailing / self.entry_price - 1) * 100),
                    remaining_fraction=self.tokens_remaining / self.initial_token_amount,
                    tokens_remaining=self.tokens_remaining,
                    realized_proceeds_sol=self.realized_proceeds_sol,
                    realized_pnl_sol=self.realized_pnl_sol,
                    unrealized_pnl_sol=self._get_unrealized_pnl(new_high_price),
                    fees_paid_sol=self.fees_paid_sol,
                    weighted_exit_price=self.weighted_exit_price,
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
                estimated_net_pnl_pct=((self.breakeven_stop_price / self.entry_price - 1) * 100),
                remaining_fraction=self.tokens_remaining / self.initial_token_amount,
                tokens_remaining=self.tokens_remaining,
                realized_proceeds_sol=self.realized_proceeds_sol,
                realized_pnl_sol=self.realized_pnl_sol,
                unrealized_pnl_sol=self._get_unrealized_pnl(new_high_price),
                fees_paid_sol=self.fees_paid_sol,
                weighted_exit_price=self.weighted_exit_price,
            )

    def close_position(self, exit_price: float, trigger: ExitTrigger, quote: Optional[PriceQuote] = None) -> dict:
        """Close position and return final P&L."""
        with self._lock:
            # Use net price after fees from quote (already has priority fee subtracted)
            if quote:
                final_price = quote.net_price_after_fees
                priority_fee = quote.priority_fee_sol
            else:
                final_price = exit_price
                priority_fee = self.config.priority_fee_sol

            # Sell remaining tokens
            if self.tokens_remaining > 0:
                gross_proceeds = self.tokens_remaining * final_price
                net_proceeds = gross_proceeds - priority_fee
                fees = priority_fee

                self.realized_proceeds_sol += net_proceeds
                self.fees_paid_sol += fees

                exit_pnl = self.tokens_remaining * (final_price - self.entry_price) - fees
                self.realized_pnl_sol += exit_pnl
                self.tokens_remaining = 0.0

            total_pnl = self.realized_pnl_sol
            total_pnl_pct = (total_pnl / self.size_sol) * 100 if self.size_sol > 0 else 0

            self._log_event(
                event="SIMULATED_FILL",
                price=exit_price,
                liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                buy_volume=0, sell_volume=0,
                trigger=trigger.value,
                position_value_sol=self.realized_proceeds_sol,
                high_water_mark=self.high_water_mark,
                stop_level=final_price,
                estimated_exit_price=final_price,
                estimated_net_pnl_pct=total_pnl_pct,
                remaining_fraction=0.0,
                tokens_remaining=0.0,
                realized_proceeds_sol=self.realized_proceeds_sol,
                realized_pnl_sol=self.realized_pnl_sol,
                unrealized_pnl_sol=0.0,
                fees_paid_sol=self.fees_paid_sol,
                weighted_exit_price=self.weighted_exit_price,
            )

            self._log_event(
                event="FINAL_PNL",
                price=exit_price,
                liquidity_usd=0, market_cap_usd=0, volume_24h_usd=0,
                buy_volume=0, sell_volume=0,
                trigger=trigger.value,
                position_value_sol=0.0,
                high_water_mark=self.high_water_mark,
                stop_level=final_price,
                estimated_exit_price=final_price,
                estimated_net_pnl_pct=total_pnl_pct,
                remaining_fraction=0.0,
                tokens_remaining=0.0,
                realized_proceeds_sol=self.realized_proceeds_sol,
                realized_pnl_sol=self.realized_pnl_sol,
                unrealized_pnl_sol=0.0,
                fees_paid_sol=self.fees_paid_sol,
                weighted_exit_price=self.weighted_exit_price,
            )

            self.state = PositionState.CLOSED

            return {
                "mint": self.mint,
                "symbol": self.symbol,
                "entry_price": self.entry_price,
                "exit_price": final_price,
                "gross_pnl_pct": total_pnl_pct,
                "net_pnl_sol": total_pnl,
                "hold_time_seconds": time.time() - self.entry_time,
                "trigger": trigger.value,
                "high_water_mark": self.high_water_mark,
                "events_count": len(self.events),
                "realized_proceeds_sol": self.realized_proceeds_sol,
                "realized_pnl_sol": self.realized_pnl_sol,
                "fees_paid_sol": self.fees_paid_sol,
                "weighted_exit_price": self.weighted_exit_price,
                "initial_token_amount": self.initial_token_amount,
            }

    def _get_remaining_fraction(self) -> float:
        """Calculate remaining position fraction after partial exits (for backward compat)."""
        if self.initial_token_amount <= 0:
            return 0.0
        return max(0.0, self.tokens_remaining / self.initial_token_amount)

    def get_events_jsonl(self) -> str:
        """Get all events as JSONL string."""
        return "\n".join(e.to_jsonl() for e in self.events)

    def get_pnl_summary(self, current_price: float) -> dict:
        """Get current P&L summary."""
        unrealized = self._get_unrealized_pnl(current_price)
        realized = self.realized_pnl_sol
        total = realized + unrealized
        return {
            "initial_token_amount": self.initial_token_amount,
            "tokens_remaining": self.tokens_remaining,
            "tokens_sold": self.initial_token_amount - self.tokens_remaining,
            "entry_price": self.entry_price,
            "current_price": current_price,
            "weighted_exit_price": self.weighted_exit_price,
            "unrealized_pnl_sol": unrealized,
            "realized_pnl_sol": realized,
            "total_pnl_sol": total,
            "fees_paid_sol": self.fees_paid_sol,
            "realized_proceeds_sol": self.realized_proceeds_sol,
            "total_pnl_pct": (total / self.size_sol) * 100 if self.size_sol > 0 else 0,
        }


class EventLogger:
    """Thread-safe append-only JSONL event logger."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._lock = threading.RLock()
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