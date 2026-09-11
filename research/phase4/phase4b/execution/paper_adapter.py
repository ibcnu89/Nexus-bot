"""
Paper Execution Adapter - Bridge between Phase 4B strategy and Paper Position Manager.

Implements the PaperExecutionAdapter boundary as designed in Phase 4A.6.
Consumes order intents, uses realistic quotes, simulates fills with realistic costs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, List, Callable
from enum import Enum
import logging

from research.phase4.prototype.paper_position_manager import (
    PaperPosition,
    ExitConfig,
    PositionState,
    ExitTrigger,
    PriceQuote as PrototypePriceQuote,
)
from research.phase4.prototype.price_source import PriceSource, TokenDecimalsError
from research.phase4.phase4b.execution.execution_accounting import (
    ExecutionQuote,
    ExitSettlement,
    PositionLedger,
    Side,
)

logger = logging.getLogger(__name__)


class OrderIntent(Enum):
    """Order intent from strategy."""
    ENTRY = "entry"
    EXIT = "exit"


@dataclass
class EntryIntent:
    """Intent to enter a position."""
    mint: str
    symbol: str
    size_sol: float
    entry_price_hint: float  # Strategy's price estimate
    max_slippage_bps: int = 50
    max_priority_fee_sol: float = 0.0005


@dataclass
class ExitIntent:
    """Intent to exit (reduce/close) a position."""
    mint: str
    tokens_to_sell: Decimal  # Actual token quantity
    max_slippage_bps: int = 50
    max_priority_fee_sol: float = 0.0005


@dataclass
class FillResult:
    """Result of a simulated fill."""
    success: bool
    mint: str
    side: str
    tokens_filled: Decimal
    avg_price: float
    gross_proceeds: Decimal
    network_costs: Decimal
    net_proceeds: Decimal
    realized_pnl: Decimal
    settlement: Optional["ExitSettlement"] = None
    error: Optional[str] = None


class PaperExecutionAdapter:
    """
    Paper execution adapter for Phase 4B.
    
    Implements the boundary: Strategy -> ExecutionAdapter -> PaperPositionManager
    
    Key responsibilities:
    - Translate strategy order intents into executable quotes
    - Simulate realistic fills using actual quotes
    - Apply deterministic exit rules
    - Maintain position ledgers with full accounting
    - No signing keys, no transaction broadcasting
    """
    
    def __init__(
        self,
        price_source: PriceSource,
        default_exit_config: Optional[ExitConfig] = None,
        max_open_positions: int = 10,
        max_new_positions_per_hour: int = 20,
        position_size_sol: float = 0.02,
    ):
        self.price_source = price_source
        self.default_exit_config = default_exit_config or ExitConfig()
        self.max_open_positions = max_open_positions
        self.max_new_positions_per_hour = max_new_positions_per_hour
        self.position_size_sol = position_size_sol
        
        # Active positions (mint -> PaperPosition)
        self.positions: Dict[str, PaperPosition] = {}
        
        # Unified ledgers (mint -> PositionLedger)
        self.ledgers: Dict[str, "PositionLedger"] = {}
        
        # Position entry tracking
        self.position_entry_times: Dict[str, float] = {}
        self.new_positions_this_hour: List[float] = []
        
        # Execution stats
        self.stats = {
            "entries": 0,
            "exits": 0,
            "partial_exits": 0,
            "final_exits": 0,
            "rejected_entry": 0,
            "rejected_exit": 0,
            "quote_failures": 0,
            "total_priority_fees_sol": 0.0,
            "total_realized_pnl_sol": 0.0,
        }
        
        # Import PositionLedger from execution_accounting
        from ..execution.execution_accounting import PositionLedger as ExecPositionLedger
        self.PositionLedger = ExecPositionLedger
        
        # Callback for when position closes
        self.on_position_close: Optional[Callable[[str, dict], None]] = None
        
        # Import from prototype
        from ...prototype.paper_position_manager import (
            PaperPosition as PrototypePaperPosition,
            ExitConfig as PrototypeExitConfig,
            ExitTrigger,
            PriceQuote as PrototypePriceQuote,
        )
        self.PrototypePaperPosition = PrototypePaperPosition
        self.PrototypeExitConfig = PrototypeExitConfig
        self.ExitTrigger = ExitTrigger
        self.PrototypePriceQuote = PrototypePriceQuote
    
    def can_open_position(self) -> bool:
        """Check if we can open a new position."""
        if len(self.positions) >= self.max_open_positions:
            return False
        
        # Check hourly rate limit
        now = time.time()
        hour_ago = now - 3600
        recent = [t for t in self.position_entry_times.values() if t > hour_ago]
        if len(recent) >= self.max_new_positions_per_hour:
            return False
        
        return True
    
    async def execute_entry(self, intent: "EntryIntent") -> FillResult:
        """
        Execute a paper entry.
        
        1. Validate intent
        2. Get executable quote
        3. Simulate fill at quoted price
        4. Create PaperPosition and ledger
        5. Return fill result
        """
        # Check capacity
        if not self.can_open_position():
            self.stats["rejected_entry"] += 1
            return FillResult(
                success=False,
                mint=intent.mint,
                side="buy",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error="Position limit reached"
            )
        
        # Check if position already exists
        if intent.mint in self.positions:
            self.stats["rejected_entry"] += 1
            return FillResult(
                success=False,
                mint=intent.mint,
                side="buy",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error="Position already exists"
            )
        
        try:
            # Get executable quote
            quote = await self.price_source.get_buy_quote(
                intent.mint,
                intent.size_sol,
                intent.max_slippage_bps
            )
            
            if quote is None:
                self.stats["rejected_entry"] += 1
                self.stats["quote_failures"] += 1
                return FillResult(
                    success=False,
                    mint=intent.mint,
                    side="buy",
                    tokens_filled=Decimal(0),
                    avg_price=0.0,
                    gross_proceeds=Decimal(0),
                    network_costs=Decimal(0),
                    net_proceeds=Decimal(0),
                    realized_pnl=Decimal(0),
                    error="Failed to get executable quote"
                )
            
            # Calculate tokens received
            tokens_received = quote.get_token_amount()
            
            # Priority fee
            priority_fee = min(intent.max_priority_fee_sol, 0.001)  # Cap at 0.001 SOL
            
            # Net cost = intent.size_sol + priority_fee
            net_cost_sol = intent.size_sol + priority_fee
            
            # Create prototype PriceQuote for PaperPosition
            proto_quote = self.PrototypePriceQuote(
                price=quote.executable_price,  # Use executable_price from PriceQuote
                price_impact_pct=quote.price_impact_pct,
                out_amount=0,
                in_amount=0,
                route=quote.route,
                swap_fee_bps=quote.swap_fee_bps,
                platform_fee_bps=quote.platform_fee_bps,
                priority_fee_sol=priority_fee,
            )
            
            # Create PaperPosition
            position = self.PrototypePaperPosition(
                mint=intent.mint,
                symbol=intent.symbol,
                entry_price=quote.executable_price,  # Use executable_price
                entry_time=time.time(),
                size_sol=net_cost_sol,
                config=self.default_exit_config,
            )
            
            # Create unified ledger
            from ..execution.execution_accounting import PositionLedger as ExecPositionLedger
            ledger = self.PositionLedger(
                mint=intent.mint,
                symbol=intent.symbol,
                initial_tokens=tokens_received,
                entry_price_sol_per_token=Decimal(str(quote.executable_price)),  # Use executable_price
                entry_cost_basis_sol=Decimal(str(net_cost_sol)),
                entry_fees_sol=Decimal(str(priority_fee)),
            )
            
            # Store
            self.positions[intent.mint] = position
            self.ledgers[intent.mint] = ledger
            self.position_entry_times[intent.mint] = time.time()
            self.new_positions_this_hour.append(time.time())
            
            # Update stats
            self.stats["entries"] += 1
            self.stats["total_priority_fees_sol"] += priority_fee
            
            logger.info(f"Paper entry: {intent.symbol} ({intent.mint[:8]}...) "
                       f"{tokens_received:.4f} tokens @ {quote.executable_price:.8f} SOL, "
                       f"cost {net_cost_sol:.6f} SOL")
            
            return FillResult(
                success=True,
                mint=intent.mint,
                side="buy",
                tokens_filled=tokens_received,
                avg_price=quote.quoted_execution_price,
                gross_proceeds=Decimal(str(tokens_received)) * Decimal(str(quote.quoted_execution_price)),
                network_costs=Decimal(str(priority_fee)),
                net_proceeds=Decimal(0),  # No proceeds on entry
                realized_pnl=Decimal(0),
            )
            
        except TokenDecimalsError as e:
            self.stats["rejected_entry"] += 1
            return FillResult(
                success=False,
                mint=intent.mint,
                side="buy",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error=f"Token decimals unavailable: {e}"
            )
        except Exception as e:
            self.stats["rejected_entry"] += 1
            self.stats["quote_failures"] += 1
            logger.error(f"Entry execution failed for {intent.mint}: {e}")
            return FillResult(
                success=False,
                mint=intent.mint,
                side="buy",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error=f"Execution error: {e}"
            )
    
    async def execute_exit(self, intent: "ExitIntent") -> FillResult:
        """
        Execute a paper exit (partial or final).
        
        1. Validate position exists
        2. Get executable SELL quote using ACTUAL token inventory
        3. Simulate fill
        4. Update PaperPosition and ledger
        5. Return fill result
        """
        if intent.mint not in self.positions:
            self.stats["rejected_exit"] += 1
            return FillResult(
                success=False,
                mint=intent.mint,
                side="sell",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error="Position not found"
            )
        
        position = self.positions[intent.mint]
        ledger = self.ledgers[intent.mint]
        
        # Validate we have enough tokens
        if intent.tokens_to_sell > ledger.tokens_remaining:
            self.stats["rejected_exit"] += 1
            return FillResult(
                success=False,
                mint=intent.mint,
                side="sell",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error="Insufficient tokens remaining"
            )
        
        try:
            # Get executable SELL quote using ACTUAL token amount
            quote = await self.price_source.get_sell_quote(
                intent.mint,
                float(intent.tokens_to_sell),
                intent.max_slippage_bps
            )
            
            if quote is None:
                self.stats["rejected_exit"] += 1
                self.stats["quote_failures"] += 1
                return FillResult(
                    success=False,
                    mint=intent.mint,
                    side="sell",
                    tokens_filled=Decimal(0),
                    avg_price=0.0,
                    gross_proceeds=Decimal(0),
                    network_costs=Decimal(0),
                    net_proceeds=Decimal(0),
                    realized_pnl=Decimal(0),
                    error="Failed to get executable sell quote"
                )
            
            # Priority fee
            priority_fee = min(intent.max_priority_fee_sol, 0.001)
            
            # Create ExecutionQuote for accounting
            exec_quote = ExecutionQuote(
                mint=intent.mint,
                side=Side.SELL,
                in_amount=quote.in_amount,
                out_amount=quote.out_amount,
                other_amount_threshold=quote.other_amount_threshold,
                in_mint=quote.in_mint,
                out_mint=quote.out_mint,
                in_decimals=quote.in_decimals,
                out_decimals=quote.out_decimals,
                route=quote.route,
                price_impact_pct=quote.price_impact_pct,
                swap_fee_bps=quote.swap_fee_bps,
                platform_fee_bps=quote.platform_fee_bps,
                priority_fee_sol=min(intent.max_priority_fee_sol, 0.001),
                timestamp=time.time(),
            )
            exec_quote._price_impact_pct = quote.price_impact_pct
            
            # Settle exit using unified accounting
            settlement = exec_quote.settle_exit(
                tokens_to_sell=intent.tokens_to_sell,
                entry_cost_basis_per_token=ledger.entry_price_sol_per_token,
                priority_fee_sol=priority_fee
            )
            
            # Apply to ledger
            ledger.apply_exit(settlement)
            
            # Also update prototype PaperPosition for exit logic
            # Create prototype quote for PaperPosition update
            proto_quote = self.PrototypePriceQuote(
                price=quote.quoted_execution_price,
                price_impact_pct=quote.price_impact_pct,
                out_amount=0,
                in_amount=0,
                route=quote.route,
                swap_fee_bps=quote.swap_fee_bps,
                platform_fee_bps=quote.platform_fee_bps,
                priority_fee_sol=priority_fee,
            )
            
            # Update PaperPosition (triggers exit logic)
            triggers = position.update_market_data(
                price=quote.quoted_execution_price,
                liquidity_usd=0,  # Would come from enrichment
                market_cap_usd=0,
                volume_24h_usd=0,
                quote=proto_quote
            )
            
            # If position closed, clean up
            if position.state.name == "CLOSED":
                self._close_position(intent.mint)
            
            # Update stats
            self.stats["exits"] += 1
            if position.state.name == "CLOSED":
                self.stats["final_exits"] += 1
            else:
                self.stats["partial_exits"] += 1
            self.stats["total_priority_fees_sol"] += priority_fee
            self.stats["total_realized_pnl_sol"] += float(settlement.realized_pnl_sol)
            
            logger.info(f"Paper exit: {position.symbol} ({intent.mint[:8]}...) "
                       f"sold {intent.tokens_to_sell:.4f} tokens @ {quote.quoted_execution_price:.8f} SOL, "
                       f"net {float(settlement.net_proceeds_sol):.6f} SOL, "
                       f"P&L {float(settlement.realized_pnl_sol):.6f} SOL")
            
            return FillResult(
                success=True,
                mint=intent.mint,
                side="sell",
                tokens_filled=intent.tokens_to_sell,
                avg_price=quote.quoted_execution_price,
                gross_proceeds=settlement.gross_proceeds_sol,
                network_costs=Decimal(str(priority_fee)),
                net_proceeds=settlement.net_proceeds_sol,
                realized_pnl=settlement.realized_pnl_sol,
                settlement=settlement,
            )
            
        except Exception as e:
            self.stats["rejected_exit"] += 1
            self.stats["quote_failures"] += 1
            logger.error(f"Exit execution failed for {intent.mint}: {e}")
            return FillResult(
                success=False,
                mint=intent.mint,
                side="sell",
                tokens_filled=Decimal(0),
                avg_price=0.0,
                gross_proceeds=Decimal(0),
                network_costs=Decimal(0),
                net_proceeds=Decimal(0),
                realized_pnl=Decimal(0),
                error=f"Execution error: {e}"
            )
    
    def _close_position(self, mint: str) -> None:
        """Clean up closed position."""
        if mint in self.positions:
            position = self.positions.pop(mint)
            ledger = self.ledgers.pop(mint)
            self.position_entry_times.pop(mint, None)
            
            if self.on_position_close:
                self.on_position_close(mint, ledger.to_dict())
    
    def update_positions(self, market_data: Dict[str, Dict]) -> Dict[str, List[ExitTrigger]]:
        """
        Update all open positions with market data.
        
        market_data: Dict[mint -> {price, liquidity_usd, market_cap_usd, volume_24h_usd, ...}]
        
        Returns: Dict[mint -> list of ExitTrigger]
        """
        all_triggers = {}
        
        for mint, position in list(self.positions.items()):
            if mint not in market_data:
                continue
            
            data = market_data[mint]
            proto_quote = None
            
            # We'd ideally get a fresh quote here, but for efficiency
            # we can use the last known quote or skip quote for update
            triggers = position.update_market_data(
                price=data.get("price", 0),
                liquidity_usd=data.get("liquidity_usd", 0),
                market_cap_usd=data.get("market_cap_usd", 0),
                volume_24h_usd=data.get("volume_24h_usd", 0),
                buy_volume=data.get("buy_volume", 0),
                sell_volume=data.get("sell_volume", 0),
                quote=None  # No fresh quote for periodic updates
            )
            
            if triggers:
                all_triggers[mint] = triggers
                
                # Check for final exits
                final_triggers = [t for t in triggers if t in (
                    self.ExitTrigger.INITIAL_STOP,
                    self.ExitTrigger.TRAILING_STOP,
                    self.ExitTrigger.BREAKEVEN_STOP,
                    self.ExitTrigger.TIME_EXIT,
                    self.ExitTrigger.LIQUIDITY_EXIT,
                    self.ExitTrigger.SELL_PRESSURE_EXIT,
                )]
                
                if final_triggers:
                    # Position will be closed by PaperPosition internally
                    pass
            
            # Check if position was closed
            if position.state.name == "CLOSED":
                self._close_position(mint)
        
        return all_triggers
    
    def get_position_summary(self, mint: str) -> Optional[Dict]:
        """Get summary for a specific position."""
        if mint not in self.positions:
            return None
        
        position = self.positions[mint]
        ledger = self.ledgers[mint]
        
        return {
            "mint": mint,
            "symbol": position.symbol,
            "state": position.state.name,
            "entry_price": position.entry_price,
            "entry_time": position.entry_time,
            "tokens_remaining": float(ledger.tokens_remaining),
            "initial_tokens": float(ledger.initial_tokens),
            "realized_pnl_sol": float(ledger.realized_pnl_sol),
            "realized_proceeds_sol": float(ledger.realized_proceeds_sol),
            "unrealized_pnl_sol": 0.0,  # Would need current price
            "fees_paid_sol": float(ledger.total_network_costs_sol),
            "weighted_exit_price": position.weighted_exit_price,
        }
    
    def get_all_summaries(self) -> List[Dict]:
        """Get summaries for all open positions."""
        return [self.get_position_summary(m) for m in self.positions.keys()]
    
    def get_stats(self) -> Dict:
        """Get execution statistics."""
        return {
            **self.stats,
            "open_positions": len(self.positions),
            "total_pnl_sol": self.stats["total_realized_pnl_sol"],
        }


class LiveExecutionAdapter:
    """
    Placeholder for future live execution adapter.
    DISABLED in Phase 4B.
    """
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Live execution disabled in Phase 4B")


def create_paper_adapter(
    price_source: PriceSource,
    position_size_sol: float = 0.02,
    max_positions: int = 10,
) -> PaperExecutionAdapter:
    """Factory for creating paper execution adapter."""
    return PaperExecutionAdapter(
        price_source=price_source,
        position_size_sol=position_size_sol,
        max_open_positions=max_positions,
    )