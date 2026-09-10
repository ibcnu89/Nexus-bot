"""
Configuration for paper position manager.

Centralized configuration with validation and presets for different risk profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


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


# Risk profile presets
CONSERVATIVE = ExitConfig(
    initial_stop_pct=-0.15,
    trail_trigger_pct=0.10,
    trail_distance_pct=0.08,
    breakeven_trigger_pct=0.20,
    breakeven_floor_pct=0.02,
    take_profit_levels=[(0.25, 0.50), (0.50, 0.30), (1.00, 0.20)],
    max_hold_seconds=43200,  # 12 hours
    momentum_timeout_seconds=1800,  # 30 min
    min_liquidity_usd=20000,
    liquidity_drop_pct=0.30,
    sell_buy_ratio_threshold=2.0,
)

AGGRESSIVE = ExitConfig(
    initial_stop_pct=-0.35,
    trail_trigger_pct=0.20,
    trail_distance_pct=0.15,
    breakeven_trigger_pct=0.40,
    breakeven_floor_pct=0.10,
    take_profit_levels=[(0.75, 0.25), (1.50, 0.50), (3.00, 0.25)],
    max_hold_seconds=172800,  # 48 hours
    momentum_timeout_seconds=7200,  # 2 hours
    min_liquidity_usd=5000,
    liquidity_drop_pct=0.60,
    sell_buy_ratio_threshold=4.0,
)

SCALPER = ExitConfig(
    initial_stop_pct=-0.10,
    trail_trigger_pct=0.05,
    trail_distance_pct=0.03,
    breakeven_trigger_pct=0.15,
    breakeven_floor_pct=0.01,
    take_profit_levels=[(0.15, 0.50), (0.30, 0.30), (0.50, 0.20)],
    max_hold_seconds=3600,  # 1 hour
    momentum_timeout_seconds=600,  # 10 min
    min_liquidity_usd=15000,
    liquidity_drop_pct=0.25,
    sell_buy_ratio_threshold=1.5,
)

DEFAULT = ExitConfig()

# Profile registry
PROFILES = {
    "conservative": CONSERVATIVE,
    "aggressive": AGGRESSIVE,
    "scalper": SCALPER,
    "default": DEFAULT,
}


def get_profile(name: str) -> ExitConfig:
    """Get a risk profile by name."""
    if name not in PROFILES:
        raise ValueError(f"Unknown profile: {name}. Available: {list(PROFILES.keys())}")
    return PROFILES[name]


@dataclass
class PrototypeConfig:
    """Full prototype configuration."""

    # Risk profile
    risk_profile: str = "default"
    exit_config: ExitConfig = field(default_factory=lambda: DEFAULT)

    # Price source
    use_mock_price: bool = True
    mock_base_price: float = 0.000523
    mock_volatility: float = 0.02
    mock_trend: float = 0.0

    # Jupiter settings
    jupiter_slippage_bps: int = 50
    jupiter_timeout: float = 10.0

    # Logging
    log_dir: Path = Path("logs/paper_positions")
    max_log_size_mb: int = 100
    max_log_files: int = 30

    # Position defaults
    default_position_size_sol: float = 0.1
    default_mint: str = "HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump"  # STONKSZN
    default_symbol: str = "STONKSZN"

    # Runtime
    update_interval_seconds: float = 5.0
    max_runtime_seconds: Optional[float] = None

    def __post_init__(self):
        if isinstance(self.exit_config, str):
            self.exit_config = get_profile(self.exit_config)
        elif self.exit_config is None:
            self.exit_config = get_profile(self.risk_profile)

    @classmethod
    def from_env(cls) -> "PrototypeConfig":
        """Create config from environment variables."""
        import os

        return cls(
            risk_profile=os.getenv("RISK_PROFILE", "default"),
            use_mock_price=os.getenv("USE_MOCK_PRICE", "true").lower() == "true",
            mock_base_price=float(os.getenv("MOCK_BASE_PRICE", "0.000523")),
            mock_volatility=float(os.getenv("MOCK_VOLATILITY", "0.02")),
            mock_trend=float(os.getenv("MOCK_TREND", "0.0")),
            default_position_size_sol=float(os.getenv("POSITION_SIZE_SOL", "0.1")),
            default_mint=os.getenv("TOKEN_MINT", "HQSXsxD2BhpA8v21TwTjv6Tbkr8yYH3B8RkGS1Bspump"),
            default_symbol=os.getenv("TOKEN_SYMBOL", "STONKSZN"),
            update_interval_seconds=float(os.getenv("UPDATE_INTERVAL", "5.0")),
        )