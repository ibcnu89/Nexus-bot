"""
Price Source - venue-aware Jupiter and Pump.fun paper quotes.

Provides realistic price quotes using Jupiter's swap API for actual swap routes,
including price impact, fees, and slippage estimation.

Key fixes from Phase 4A.6 audit:
- Current Jupiter API endpoints (swap/v1/quote, price/v3, tokens V2)
- Correct buy/sell amount dimensionality (SOL atomic vs TOKEN atomic)
- Proper SPL token decimal handling - never defaults to 9 decimals for executable sizing
- Execution accounting: AMM/platform fees counted exactly once (embedded in outAmount)
- Price impact counted exactly once (via otherAmountThreshold for slippage)
- Priority/network fee counted exactly once (separate from quote)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict
import os
import aiohttp
import logging

logger = logging.getLogger(__name__)


# Current Jupiter quote-only endpoint. Omitting `taker` prevents transaction creation.
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v2/order"
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"

PUMPPORTAL_WS_API = "wss://pumpportal.fun/api/data"

# Solana constants
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
PUMPFUN_BONDING_CURVE_FEE_BPS = 125
MAX_BONDING_CURVE_STATE_AGE_SECONDS = 15.0


class TokenDecimalsError(Exception):
    """Raised when token decimals cannot be resolved for executable sizing."""
    pass


@dataclass
class TokenInfo:
    """Token metadata including decimals."""
    mint: str
    symbol: str
    name: str
    decimals: int
    logo_uri: Optional[str] = None


@dataclass(frozen=True)
class BondingCurveState:
    """Observed Pump.fun bonding-curve reserves in atomic units."""

    mint: str
    bonding_curve_key: str
    token_decimals: int
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: Optional[int] = None
    real_sol_reserves: Optional[int] = None
    token_total_supply: Optional[int] = None
    complete: bool = False
    observed_at: float = field(default_factory=time.time)
    source: str = "on_chain"

    def __post_init__(self) -> None:
        if not 0 <= self.token_decimals <= 18:
            raise ValueError(f"Invalid token decimals: {self.token_decimals}")
        if self.virtual_token_reserves <= 0 or self.virtual_sol_reserves <= 0:
            raise ValueError("Bonding-curve virtual reserves must be positive")


@dataclass
class PriceQuote:
    """
    Realistic executable price quote from Jupiter.

    CRITICAL ACCOUNTING INVARIANTS:
    - outAmount is AFTER AMM/platform fees (do NOT subtract again)
    - priceImpactPct is informational; use otherAmountThreshold for slippage bounds
    - swap_fee_bps from route is INFORMATIONAL only (already in outAmount)
    - platform_fee_bps is INFORMATIONAL only (already in outAmount)
    - priority_fee_sol is SEPARATE (network cost, not in quote)
    """
    # Core price (SOL per token unit, normalized to token decimals)
    price_sol_per_token: float
    # Raw quote amounts in atomic units
    in_amount: int              # Input amount in atomic units of in_mint
    out_amount: int             # Output amount in atomic units of out_mint
    other_amount_threshold: int # Minimum output after slippage
    in_mint: str                # Input mint
    out_mint: str               # Output mint
    in_decimals: int            # Input token decimals
    out_decimals: int           # Output token decimals
    # Route info
    route: str
    price_impact_pct: float
    # Fee info from route (INFORMATIONAL - already embedded in out_amount)
    swap_fee_bps: int = 0
    platform_fee_bps: int = 0
    route_fee_sol: Optional[float] = None
    # Priority fee (NOT in quote - separate network cost)
    priority_fee_sol: float = 0.0
    venue_state_source: Optional[str] = None
    venue_state_observed_at: Optional[float] = None
    # Timestamp
    timestamp: float = field(default_factory=time.time)

    @property
    def executable_price(self) -> float:
        """Price after accounting for price impact (using slippage threshold)."""
        if self.in_amount == 0:
            return self.price_sol_per_token
        # Use other_amount_threshold for worst-case slippage scenario
        slippage_factor = self.other_amount_threshold / self.out_amount if self.out_amount > 0 else 1.0
        if slippage_factor <= 0:
            return self.price_sol_per_token
        if self.out_mint == SOL_MINT:
            return self.price_sol_per_token * slippage_factor
        return self.price_sol_per_token / slippage_factor

    @property
    def net_price_after_fees(self) -> float:
        """
        Price after ALL fees.
        AMM/platform fees are ALREADY EMBEDDED in out_amount.
        Only priority fee needs to be subtracted here.
        """
        # AMM/platform fees already baked into out_amount -> executable_price
        # Only subtract priority fee (network cost)
        if self.out_mint == SOL_MINT:
            # SELL: output is SOL, priority fee reduces SOL received
            token_amount = self.in_amount / (10 ** self.in_decimals)
            priority_fee_per_token = self.priority_fee_sol / token_amount if token_amount > 0 else 0
            return max(0.0, self.executable_price - priority_fee_per_token)
        else:
            # BUY: output is token, priority fee is SOL cost separate from token amount
            return self.executable_price

    def get_sol_amount(self) -> Decimal:
        """Get SOL amount from quote (handles both buy/sell)."""
        if self.out_mint == SOL_MINT:
            return Decimal(self.out_amount) / Decimal(LAMPORTS_PER_SOL)
        elif self.in_mint == SOL_MINT:
            return Decimal(self.in_amount) / Decimal(LAMPORTS_PER_SOL)
        return Decimal(0)

    def get_token_amount(self) -> Decimal:
        """Get token amount from quote."""
        if self.out_mint != SOL_MINT:
            return Decimal(self.out_amount) / Decimal(10 ** self.out_decimals)
        elif self.in_mint != SOL_MINT:
            return Decimal(self.in_amount) / Decimal(10 ** self.in_decimals)
        return Decimal(0)


class PriceSource:
    """
    Unified price source using Jupiter for graduated tokens and PumpPortal for bonding curve tokens.

    Key design decisions:
    - BUY (SOL -> token): input amount in SOL atomic units (lamports), output in token atomic units
    - SELL (token -> SOL): input amount in token atomic units, output in SOL atomic units (lamports)
    - All prices normalized to SOL per human-readable token
    - Token decimals supplied by authoritative on-chain mint data and cached
    - Failure to resolve decimals fails closed for executable sizing
    """

    def __init__(
        self,
        jupiter_api_url: str = JUPITER_QUOTE_API,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.jupiter_api_url = jupiter_api_url
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.jupiter_api_key = os.environ.get("JUPITER_API_KEY")
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_decimals_cache: Dict[str, int] = {}
        self._token_info_cache: Dict[str, TokenInfo] = {}
        self._decimals_resolved: set = set()  # Track which mints we've resolved
        self._bonding_curve_states: Dict[str, BondingCurveState] = {}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    def cache_token_decimals(self, mint: str, decimals: int) -> None:
        """Cache authoritative on-chain mint decimals for executable sizing."""
        if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 18:
            raise TokenDecimalsError(f"Invalid token decimals for {mint}: {decimals!r}")
        self._token_decimals_cache[mint] = decimals
        self._decimals_resolved.add(mint)

    def get_token_decimals(self, mint: str) -> int:
        """Get token decimals. For SOL, returns 9. For unknown, raises for executable sizing."""
        if mint == SOL_MINT:
            return 9
        if mint in self._token_decimals_cache:
            return self._token_decimals_cache[mint]
        # For executable sizing, we MUST have decimals - fail closed
        raise TokenDecimalsError(f"Token decimals unknown for {mint}. Cannot size executable order.")

    def get_token_decimals_or_none(self, mint: str) -> Optional[int]:
        """Get token decimals, returning None if unknown (for display only)."""
        if mint == SOL_MINT:
            return 9
        return self._token_decimals_cache.get(mint)

    def get_token_info(self, mint: str) -> Optional[TokenInfo]:
        """Get full token info."""
        if mint == SOL_MINT:
            return TokenInfo(mint=SOL_MINT, symbol="SOL", name="Solana", decimals=9)
        return self._token_info_cache.get(mint)

    def cache_bonding_curve_state(self, state: BondingCurveState) -> None:
        """Cache an observed curve state and its authoritative token decimals."""
        self.cache_token_decimals(state.mint, state.token_decimals)
        self._bonding_curve_states[state.mint] = state

    def get_bonding_curve_state(self, mint: str) -> Optional[BondingCurveState]:
        return self._bonding_curve_states.get(mint)

    @staticmethod
    def _curve_state_is_fresh(state: BondingCurveState) -> bool:
        age = max(0.0, time.time() - state.observed_at)
        return age <= MAX_BONDING_CURVE_STATE_AGE_SECONDS

    @staticmethod
    def _validate_quote_inputs(amount: float, slippage_bps: int) -> None:
        if amount <= 0:
            raise ValueError("Quote amount must be positive")
        if not 0 <= slippage_bps < 10_000:
            raise ValueError("slippage_bps must be between 0 and 9999")

    def get_bonding_curve_sell_quote(
        self,
        state: BondingCurveState,
        token_amount_human: float,
        slippage_bps: int = 50,
        fee_bps: int = PUMPFUN_BONDING_CURVE_FEE_BPS,
    ) -> Optional[PriceQuote]:
        """Calculate a deterministic token-to-SOL paper quote from observed reserves."""
        self._validate_quote_inputs(token_amount_human, slippage_bps)
        if state.complete:
            return None

        token_scale = 10 ** state.token_decimals
        token_in = int(Decimal(str(token_amount_human)) * token_scale)
        if token_in <= 0:
            return None

        token_reserves = state.virtual_token_reserves
        sol_reserves = state.virtual_sol_reserves
        gross_sol_out = token_in * sol_reserves // (token_reserves + token_in)
        fee_lamports = gross_sol_out * fee_bps // 10_000
        net_sol_out = gross_sol_out - fee_lamports
        if net_sol_out <= 0:
            return None

        min_sol_out = net_sol_out * (10_000 - slippage_bps) // 10_000
        human_token_in = token_in / token_scale
        price = (net_sol_out / LAMPORTS_PER_SOL) / human_token_in
        spot = (sol_reserves / LAMPORTS_PER_SOL) / (token_reserves / token_scale)
        gross_price = (gross_sol_out / LAMPORTS_PER_SOL) / human_token_in
        price_impact = max(0.0, (spot - gross_price) / spot * 100) if spot else 0.0

        return PriceQuote(
            price_sol_per_token=price,
            in_amount=token_in,
            out_amount=net_sol_out,
            other_amount_threshold=min_sol_out,
            in_mint=state.mint,
            out_mint=SOL_MINT,
            in_decimals=state.token_decimals,
            out_decimals=9,
            route="PumpFunBondingCurve",
            price_impact_pct=price_impact,
            swap_fee_bps=fee_bps,
            route_fee_sol=fee_lamports / LAMPORTS_PER_SOL,
            venue_state_source=state.source,
            venue_state_observed_at=state.observed_at,
        )

    def get_bonding_curve_buy_quote(
        self,
        state: BondingCurveState,
        sol_amount: float,
        slippage_bps: int = 50,
        fee_bps: int = PUMPFUN_BONDING_CURVE_FEE_BPS,
    ) -> Optional[PriceQuote]:
        """Calculate a deterministic SOL-to-token paper quote from observed reserves."""
        self._validate_quote_inputs(sol_amount, slippage_bps)
        if state.complete:
            return None

        sol_in = int(Decimal(str(sol_amount)) * LAMPORTS_PER_SOL)
        fee_lamports = sol_in * fee_bps // 10_000
        effective_sol_in = sol_in - fee_lamports
        if effective_sol_in <= 0:
            return None

        token_reserves = state.virtual_token_reserves
        sol_reserves = state.virtual_sol_reserves
        token_out = effective_sol_in * token_reserves // (sol_reserves + effective_sol_in)
        if state.real_token_reserves is not None:
            token_out = min(token_out, state.real_token_reserves)
        if token_out <= 0:
            return None

        min_token_out = token_out * (10_000 - slippage_bps) // 10_000
        token_scale = 10 ** state.token_decimals
        human_token_out = token_out / token_scale
        price = (sol_in / LAMPORTS_PER_SOL) / human_token_out
        spot = (sol_reserves / LAMPORTS_PER_SOL) / (token_reserves / token_scale)
        price_impact = max(0.0, (price - spot) / spot * 100) if spot else 0.0

        return PriceQuote(
            price_sol_per_token=price,
            in_amount=sol_in,
            out_amount=token_out,
            other_amount_threshold=min_token_out,
            in_mint=SOL_MINT,
            out_mint=state.mint,
            in_decimals=9,
            out_decimals=state.token_decimals,
            route="PumpFunBondingCurve",
            price_impact_pct=price_impact,
            swap_fee_bps=fee_bps,
            route_fee_sol=fee_lamports / LAMPORTS_PER_SOL,
            venue_state_source=state.source,
            venue_state_observed_at=state.observed_at,
        )

    async def _fetch_with_retry(self, url: str, params: dict) -> dict:
        """Fetch with exponential backoff retry."""
        if url.startswith("https://api.jup.ag/") and not self.jupiter_api_key:
            raise RuntimeError("JUPITER_API_KEY is required for Jupiter quotes")
        session = await self._get_session()
        for attempt in range(self.max_retries):
            try:
                headers = (
                    {"x-api-key": self.jupiter_api_key}
                    if self.jupiter_api_key and url.startswith("https://api.jup.ag/")
                    else {}
                )
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning(f"Rate limited, waiting {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status} from {url}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching {url} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Error fetching {url}: {e}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Failed to fetch {url} after {self.max_retries} retries")

    async def get_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
    ) -> Optional[PriceQuote]:
        """
        Get a swap quote from Jupiter.

        CRITICAL: amount MUST be in atomic units of the INPUT mint.
        - For BUY (SOL -> token): amount in lamports (SOL atomic units)
        - For SELL (token -> SOL): amount in token atomic units (base units)
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }

        try:
            data = await self._fetch_with_retry(self.jupiter_api_url, params)

            # Parse Jupiter Swap V2 quote-only response.
            in_amount = int(data["inAmount"])
            out_amount = int(data["outAmount"])
            other_amount_threshold = int(data.get("otherAmountThreshold", out_amount))
            price_impact = float(data.get("priceImpact", 0))

            # Extract fee info from route (INFORMATIONAL - already in out_amount)
            swap_fee_bps = int(data.get("feeBps", 0) or 0)
            platform_fee_bps = 0
            router = data.get("router", "unknown")
            route_label = f"Jupiter/{router}"
            if data.get("routePlan"):
                amm_label = data["routePlan"][0].get("swapInfo", {}).get("label")
                if amm_label:
                    route_label = f"{route_label}:{amm_label}"

            platform_fee_bps = data.get("platformFee", {}).get("feeBps", 0) if data.get("platformFee") else 0

            # Get token decimals (will raise if unknown and needed for executable sizing)
            in_decimals = self.get_token_decimals(input_mint)
            out_decimals = self.get_token_decimals(output_mint)

            # Calculate price: SOL per human-readable token
            if output_mint == SOL_MINT:
                # SELL: token -> SOL
                sol_out = out_amount / LAMPORTS_PER_SOL
                token_in = in_amount / (10 ** in_decimals)
                price_sol_per_token = sol_out / token_in if token_in > 0 else 0
            else:
                # BUY: SOL -> token
                sol_in = in_amount / LAMPORTS_PER_SOL
                token_out = out_amount / (10 ** out_decimals)
                price_sol_per_token = sol_in / token_out if token_out > 0 else 0

            return PriceQuote(
                price_sol_per_token=price_sol_per_token,
                price_impact_pct=price_impact,
                in_amount=in_amount,
                out_amount=out_amount,
                other_amount_threshold=other_amount_threshold,
                in_mint=input_mint,
                out_mint=output_mint,
                in_decimals=in_decimals,
                out_decimals=out_decimals,
                route=route_label,
                swap_fee_bps=swap_fee_bps,
                platform_fee_bps=platform_fee_bps,
            )
        except TokenDecimalsError:
            raise
        except Exception as e:
            logger.warning(f"Jupiter quote failed for {input_mint}->{output_mint}: {e}")
            return None

    async def get_buy_quote(
        self,
        mint: str,
        sol_amount: float,
        slippage_bps: int = 50,
    ) -> Optional[PriceQuote]:
        """
        Get quote for BUY: SOL -> token.

        Input: SOL amount in human units (e.g., 0.1 SOL)
        Returns quote with input in lamports, output in token atomic units.
        """
        state = self.get_bonding_curve_state(mint)
        if state and not state.complete:
            if not self._curve_state_is_fresh(state):
                logger.warning(f"Stale bonding-curve state for {mint}; quote failed closed")
                return None
            return self.get_bonding_curve_buy_quote(state, sol_amount, slippage_bps)
        amount = int(Decimal(str(sol_amount)) * LAMPORTS_PER_SOL)
        return await self.get_jupiter_quote(SOL_MINT, mint, amount, slippage_bps)

    async def get_sell_quote(
        self,
        mint: str,
        token_amount_human: float,
        slippage_bps: int = 50,
    ) -> Optional[PriceQuote]:
        """
        Get quote for SELL: token -> SOL.

        Input: token amount in human units (e.g., 1000 tokens)
        Returns quote with input in token atomic units, output in lamports.
        """
        state = self.get_bonding_curve_state(mint)
        if state and not state.complete:
            if not self._curve_state_is_fresh(state):
                logger.warning(f"Stale bonding-curve state for {mint}; quote failed closed")
                return None
            return self.get_bonding_curve_sell_quote(state, token_amount_human, slippage_bps)
        decimals = self.get_token_decimals(mint)  # Fails closed if unknown
        amount = int(Decimal(str(token_amount_human)) * (10 ** decimals))
        return await self.get_jupiter_quote(mint, SOL_MINT, amount, slippage_bps)

    async def get_price(
        self,
        mint: str,
        side: str = "sell",
        size_sol: float = 0.1,
        is_graduated: bool = False,
    ) -> Optional[PriceQuote]:
        """
        Get executable price for a token.

        side: "buy" = SOL -> token, "sell" = token -> SOL
        size_sol: position size in SOL (for buy) or SOL value (for sell)

        For SELL: requires actual token inventory to quote correctly.
        """
        state = self.get_bonding_curve_state(mint)
        if state and not state.complete and not is_graduated:
            if not self._curve_state_is_fresh(state):
                logger.warning(f"Stale bonding-curve state for {mint}; price failed closed")
                return None
            if side == "buy":
                return await self.get_buy_quote(mint, size_sol)
            token_scale = 10 ** state.token_decimals
            spot = (
                (state.virtual_sol_reserves / LAMPORTS_PER_SOL)
                / (state.virtual_token_reserves / token_scale)
            )
            if spot <= 0:
                return None
            return await self.get_sell_quote(mint, size_sol / spot)

        if is_graduated or mint == SOL_MINT or state is None or state.complete:
            decimals = self.get_token_decimals_or_none(mint)
            if decimals is None:
                logger.warning(f"Unknown decimals for {mint}, cannot quote")
                return None

            if side == "sell":
                # Probe the actual token/SOL swap route. Jupiter Price V3 reports USD,
                # so it must not be treated as a SOL-denominated token price here.
                probe_token_amount = 1_000.0
                probe = await self.get_sell_quote(mint, probe_token_amount)
                if not probe or probe.price_sol_per_token <= 0:
                    return None
                token_amount_human = size_sol / probe.price_sol_per_token
                return await self.get_sell_quote(mint, token_amount_human)
            else:
                # BUY: SOL -> token
                return await self.get_buy_quote(mint, size_sol)
        return None

    async def get_multiple_prices(self, mints: list[str], vs_mint: str = SOL_MINT) -> dict[str, float]:
        """Get current prices for multiple tokens using Price API V3."""
        if not mints:
            return {}

        params = {"ids": ",".join(mints)}
        try:
            session = await self._get_session()
            async with session.get(JUPITER_PRICE_API, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {mint: float(info.get("usdPrice", 0)) for mint, info in data.items()}
        except Exception as e:
            logger.warning(f"Batch price fetch failed: {e}")
        return {}


class MockPriceSource:
    """Mock price source for testing with simulated volatility."""

    def __init__(
        self,
        base_price: float = 0.0005,
        volatility: float = 0.02,
        trend: float = 0.0,
    ):
        self.base_price = base_price
        self.volatility = volatility
        self.trend = trend
        self.current_price = base_price
        self.step = 0

    def get_price(self, mint: str, side: str = "sell", size_sol: float = 0.1, is_graduated: bool = False) -> PriceQuote:
        """Generate a simulated price quote with random walk."""
        import random

        change = random.gauss(self.trend, self.volatility)
        self.current_price *= (1 + change)
        self.current_price = max(self.current_price, 0.000001)
        self.step += 1

        price_impact = min(0.05, size_sol * 0.001)

        return PriceQuote(
            price_sol_per_token=self.current_price,
            price_impact_pct=price_impact,
            in_amount=0,
            out_amount=0,
            other_amount_threshold=0,
            in_mint=mint,
            out_mint=SOL_MINT,
            in_decimals=9,
            out_decimals=9,
            route="Mock",
            swap_fee_bps=0,
            platform_fee_bps=0,
        )

    def get_jupiter_quote(self, *args, **kwargs) -> PriceQuote:
        return self.get_price("", *args, **kwargs)
