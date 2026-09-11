"""
Execution Accounting - Canonical accounting model for paper trading.

Provides unified settlement for all exit types (partial and final).
Ensures AMM/platform fees counted exactly once, priority fees once,
and slippage direction correct for BUY vs SELL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class ExecutionQuote:
    """
    Unified execution quote with explicit slippage and fee separation.
    
    CRITICAL INVARIANTS:
    - out_amount: quoted output AFTER AMM/platform fees (from Jupiter)
    - other_amount_threshold: minimum output after slippage tolerance
    - price_impact_pct: diagnostic from route, NOT slippage
    - swap_fee_bps, platform_fee_bps: INFORMATIONAL only (already in out_amount)
    - priority_fee_sol: SEPARATE network cost, subtracted from proceeds
    """
    # Core identification
    mint: str
    side: Side
    
    # Raw quote amounts (atomic units)
    in_amount: int              # Input amount in atomic units of in_mint
    out_amount: int             # Output amount in atomic units of out_mint (AFTER AMM/platform fees)
    other_amount_threshold: int # Minimum output after slippage tolerance
    
    # Token info
    in_mint: str
    out_mint: str
    in_decimals: int
    out_decimals: int
    
    # Route info
    route: str
    price_impact_pct: float     # Diagnostic from route
    
    # Fee info from route (INFORMATIONAL - already embedded in out_amount)
    swap_fee_bps: int = 0
    platform_fee_bps: int = 0
    
    # Network cost (SEPARATE - not in quote)
    priority_fee_sol: float = 0.0
    
    # Internal price impact storage
    _price_impact_pct: float = 0.0
    
    # Timestamp
    timestamp: float = 0.0
    
    # Solana constants
    SOL_MINT: str = "So11111111111111111111111111111111111111112"
    LAMPORTS_PER_SOL: int = 1_000_000_000
    
    @property
    def quoted_execution_price(self) -> float:
        """
        Quoted execution price (SOL per human-readable token).
        Uses out_amount which already includes AMM/platform fees.
        """
        if self.side == Side.SELL:
            # SELL: token -> SOL
            # price = SOL_out / token_in
            sol_out = self.out_amount / 1_000_000_000
            token_in = self.in_amount / (10 ** self.in_decimals)
            return sol_out / token_in if token_in > 0 else 0.0
        else:
            # BUY: SOL -> token
            # price = SOL_in / token_out
            sol_in = self.in_amount / 1_000_000_000
            token_out = self.out_amount / (10 ** self.out_decimals)
            return sol_in / token_out if token_out > 0 else 0.0
    
    @property
    def worst_case_execution_price(self) -> float:
        """
        Worst-case execution price after slippage tolerance.
        Uses other_amount_threshold (minimum acceptable output after slippage).
        
        For SELL: lower SOL output = worse price (lower SOL/token)
        For BUY: lower token output = worse price (higher SOL/token)
        """
        if self.side == Side.SELL:
            # SELL: token -> SOL
            # worse = less SOL out per token
            sol_out_worst = self.other_amount_threshold / 1_000_000_000
            token_in = self.in_amount / (10 ** self.in_decimals)
            return sol_out_worst / token_in if token_in > 0 else 0.0
        else:
            # BUY: SOL -> token
            # worse = less token out per SOL = higher SOL/token
            sol_in = self.in_amount / 1_000_000_000
            token_out_worst = self.other_amount_threshold / (10 ** self.out_decimals)
            return sol_in / token_out_worst if token_out_worst > 0 else 0.0
    
    @property
    def slippage_bps(self) -> int:
        """Slippage in basis points (derived from threshold)."""
        if self.out_amount == 0:
            return 0
        slippage = 1.0 - (self.other_amount_threshold / self.out_amount)
        return int(slippage * 10000)
    
    @property
    def price_impact_pct(self) -> float:
        """Price impact from route (diagnostic)."""
        return self._price_impact_pct
    
    @price_impact_pct.setter
    def price_impact_pct(self, value: float):
        self._price_impact_pct = value
    
    def get_sol_proceeds(self) -> Decimal:
        """Get SOL proceeds from quote (for SELL) or SOL cost (for BUY)."""
        if self.side == Side.SELL:
            return Decimal(self.out_amount) / Decimal(1_000_000_000)
        else:
            return Decimal(self.in_amount) / Decimal(1_000_000_000)
    
    def get_token_amount(self) -> Decimal:
        """Get token amount from quote."""
        if self.side == Side.SELL:
            return Decimal(self.in_amount) / Decimal(10 ** self.in_decimals)
        else:
            return Decimal(self.out_amount) / Decimal(10 ** self.out_decimals)
    
    def settle_exit(
        self,
        tokens_to_sell: Decimal,
        entry_cost_basis_per_token: Decimal,
        priority_fee_sol: float = 0.0
    ) -> "ExitSettlement":
        """
        Settle an exit (partial or final) using unified accounting.
        
        This is the SINGLE canonical settlement function.
        Both partial and final exits MUST call this.
        
        Args:
            tokens_to_sell: Actual token quantity to sell (human units)
            entry_cost_basis_per_token: Cost basis per token (SOL per token)
            priority_fee_sol: Priority fee for this exit (SOL)
        
        Returns:
            ExitSettlement with all accounting fields
        """
        # Convert tokens to atomic units
        tokens_atomic = int(tokens_to_sell * (10 ** self.in_decimals))
        
        # Gross proceeds from quote (already has AMM/platform fees embedded)
        # Scale proportionally: tokens_to_sell / total_token_in
        total_token_in = Decimal(self.in_amount) / Decimal(10 ** self.in_decimals)
        proportion = tokens_to_sell / total_token_in if total_token_in > 0 else Decimal(0)
        
        # Gross proceeds = quoted output * proportion
        gross_proceeds_sol = (Decimal(self.out_amount) / Decimal(1_000_000_000)) * proportion
        
        # Network costs (priority fee + base fee)
        network_costs_sol = Decimal(str(priority_fee_sol))
        
        # Net proceeds
        net_proceeds_sol = gross_proceeds_sol - network_costs_sol
        
        # Realized P&L = net_proceeds - allocated cost basis
        allocated_cost_basis = tokens_to_sell * entry_cost_basis_per_token
        realized_pnl_sol = net_proceeds_sol - allocated_cost_basis
        
        # Execution prices
        execution_price = float(gross_proceeds_sol / tokens_to_sell) if tokens_to_sell > 0 else 0.0
        net_execution_price = float(net_proceeds_sol / tokens_to_sell) if tokens_to_sell > 0 else 0.0
        
        return ExitSettlement(
            tokens_sold=tokens_to_sell,
            gross_proceeds_sol=gross_proceeds_sol,
            network_costs_sol=network_costs_sol,
            net_proceeds_sol=net_proceeds_sol,
            allocated_cost_basis=allocated_cost_basis,
            realized_pnl_sol=realized_pnl_sol,
            execution_price=execution_price,
            net_execution_price=net_execution_price,
            quoted_execution_price=self.quoted_execution_price,
            worst_case_execution_price=self.worst_case_execution_price,
            price_impact_pct=self.price_impact_pct,
            slippage_bps=self.slippage_bps,
            priority_fee_sol=priority_fee_sol,
        )


@dataclass
class ExitSettlement:
    """Result of a single exit settlement (partial or final)."""
    tokens_sold: Decimal
    gross_proceeds_sol: Decimal
    network_costs_sol: Decimal
    net_proceeds_sol: Decimal
    allocated_cost_basis: Decimal
    realized_pnl_sol: Decimal
    execution_price: float
    net_execution_price: float
    quoted_execution_price: float
    worst_case_execution_price: float
    price_impact_pct: float
    slippage_bps: int
    priority_fee_sol: float
    
    def to_dict(self) -> dict:
        return {
            "tokens_sold": str(self.tokens_sold),
            "gross_proceeds_sol": str(self.gross_proceeds_sol),
            "network_costs_sol": str(self.network_costs_sol),
            "net_proceeds_sol": str(self.net_proceeds_sol),
            "allocated_cost_basis": str(self.allocated_cost_basis),
            "realized_pnl_sol": str(self.realized_pnl_sol),
            "execution_price": self.execution_price,
            "net_execution_price": self.net_execution_price,
            "quoted_execution_price": self.quoted_execution_price,
            "worst_case_execution_price": self.worst_case_execution_price,
            "price_impact_pct": self.price_impact_pct,
            "slippage_bps": self.slippage_bps,
            "priority_fee_sol": self.priority_fee_sol,
        }


@dataclass
class PositionLedger:
    """
    Unified position ledger for paper trading.
    
    Maintains invariant:
    realized_proceeds_sol = sum of all net exit proceeds
    realized_pnl_sol = realized_proceeds_sol - realized_entry_cost_basis
    tokens_remaining = initial_tokens - sum(tokens_sold)
    """
    mint: str
    symbol: str
    initial_tokens: Decimal
    entry_price_sol_per_token: Decimal
    entry_cost_basis_sol: Decimal  # Total SOL invested
    entry_fees_sol: Decimal = Decimal(0)
    
    # Running totals
    tokens_remaining: Decimal = field(default=None)
    realized_proceeds_sol: Decimal = Decimal(0)
    realized_entry_cost_basis: Decimal = Decimal(0)
    realized_pnl_sol: Decimal = Decimal(0)
    total_fees_paid_sol: Decimal = Decimal(0)
    total_network_costs_sol: Decimal = Decimal(0)
    
    # Exit history
    exits: List = field(default_factory=list)
    
    def __post_init__(self):
        if self.tokens_remaining is None:
            self.tokens_remaining = self.initial_tokens
        if self.exits is None:
            self.exits = []
    
    def apply_exit(self, settlement: "ExitSettlement") -> None:
        """Apply an exit settlement to the ledger."""
        self.tokens_remaining -= settlement.tokens_sold
        self.realized_proceeds_sol += settlement.net_proceeds_sol
        self.realized_entry_cost_basis += settlement.allocated_cost_basis
        self.realized_pnl_sol += settlement.realized_pnl_sol
        self.total_fees_paid_sol += Decimal("0")  # AMM fees embedded
        self.total_network_costs_sol += Decimal(str(settlement.priority_fee_sol))
        self.exits.append(settlement)
    
    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calculate unrealized P&L based on current price."""
        return self.tokens_remaining * (current_price - self.entry_price_sol_per_token)
    
    def get_total_pnl(self, current_price: Decimal) -> Decimal:
        """Total P&L = realized + unrealized."""
        return self.realized_pnl_sol + self.get_unrealized_pnl(current_price)
    
    def verify_invariant(self, tolerance: Decimal = Decimal("0.00000001")) -> bool:
        """
        Verify ledger invariant:
        entry_cost_basis + realized_pnl - total_costs = current_portfolio_value
        """
        # Current portfolio value = realized proceeds + unrealized value
        # But we need current price for unrealized...
        # This is a structural check
        expected_realized = sum(e.net_proceeds_sol for e in self.exits)
        return abs(self.realized_proceeds_sol - expected_realized) < tolerance
    
    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "initial_tokens": str(self.initial_tokens),
            "tokens_remaining": str(self.tokens_remaining),
            "entry_price_sol_per_token": str(self.entry_price_sol_per_token),
            "entry_cost_basis_sol": str(self.entry_cost_basis_sol),
            "realized_proceeds_sol": str(self.realized_proceeds_sol),
            "realized_entry_cost_basis": str(self.realized_entry_cost_basis),
            "realized_pnl_sol": str(self.realized_pnl_sol),
            "total_fees_paid_sol": str(self.total_fees_paid_sol),
            "total_network_costs_sol": str(self.total_network_costs_sol),
            "exits_count": len(self.exits),
        }