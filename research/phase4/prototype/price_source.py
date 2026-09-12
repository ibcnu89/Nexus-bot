"""
Price Source - Jupiter + PumpPortal price feeds for executable price realism.

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


# Current Jupiter API endpoints (verified 2025-09-10)
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"

# PumpPortal API
PUMPPORTAL_PRICE_API = "https://pumpportal.fun/api/price"
PUMPPORTAL_WS_API = "wss://pumpportal.fun/api/data"

# Solana constants
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


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
    # Priority fee (NOT in quote - separate network cost)
    priority_fee_sol: float = 0.0
    # Timestamp
    timestamp: float = field(default_factory=time.time)

    @property
    def executable_price(self) -> float:
        """Price after accounting for price impact (using slippage threshold)."""
        if self.in_amount == 0:
            return self.price_sol_per_token
        # Use other_amount_threshold for worst-case slippage scenario
        slippage_factor = self.other_amount_threshold / self.out_amount if self.out_amount > 0 else 1.0
        return self.price_sol_per_token * slippage_factor

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
            priority_fee_per_token = self.priority_fee_sol / (self.out_amount / LAMPORTS_PER_SOL) if self.out_amount > 0 else 0
            return self.executable_price - priority_fee_per_token
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
        pumpportal_api_url: str = PUMPPORTAL_PRICE_API,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.jupiter_api_url = jupiter_api_url
        self.pumpportal_api_url = pumpportal_api_url
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.jupiter_api_key = os.environ.get("JUPITER_API_KEY")
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_decimals_cache: Dict[str, int] = {}
        self._token_info_cache: Dict[str, TokenInfo] = {}
        self._decimals_resolved: set = set()  # Track which mints we've resolved

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

    async def _fetch_with_retry(self, url: str, params: dict) -> dict:
        """Fetch with exponential backoff retry."""
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
            "restrictIntermediateTokens": "true",
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }

        try:
            data = await self._fetch_with_retry(self.jupiter_api_url, params)

            # Parse Jupiter quote response (current V1 schema)
            in_amount = int(data["inAmount"])
            out_amount = int(data["outAmount"])
            other_amount_threshold = int(data.get("otherAmountThreshold", out_amount))
            price_impact = float(data.get("priceImpactPct", 0))

            # Extract fee info from route (INFORMATIONAL - already in out_amount)
            swap_fee_bps = 0
            platform_fee_bps = 0
            route_label = "Jupiter"
            if data.get("routePlan"):
                for step in data["routePlan"]:
                    swap_info = step.get("swapInfo", {})
                    swap_fee_bps += swap_info.get("feeBps", 0)
                    # Platform fee might be at top level
                route_label = data["routePlan"][0].get("swapInfo", {}).get("label", "Jupiter")

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

    async def get_pumpportal_price(self, mint: str) -> Optional[PriceQuote]:
        """Get price from PumpPortal for bonding curve tokens."""
        try:
            params = {"mint": mint}
            data = await self._fetch_with_retry(self.pumpportal_api_url, params)

            price = float(data.get("price", 0))
            if price <= 0:
                return None

            decimals = self.get_token_decimals_or_none(mint) or 9
            return PriceQuote(
                price_sol_per_token=price,
                price_impact_pct=0.01,  # Estimate 1% impact for bonding curve
                in_amount=0,
                out_amount=0,
                other_amount_threshold=0,
                in_mint=mint,
                out_mint=SOL_MINT,
                in_decimals=decimals,
                out_decimals=9,
                route="PumpPortal",
                swap_fee_bps=0,
                platform_fee_bps=0,
            )
        except Exception as e:
            logger.warning(f"PumpPortal price failed for {mint}: {e}")
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
        amount = int(sol_amount * LAMPORTS_PER_SOL)
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
        decimals = self.get_token_decimals(mint)  # Fails closed if unknown
        amount = int(token_amount_human * (10 ** decimals))
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
        if is_graduated or mint == SOL_MINT:
            decimals = self.get_token_decimals_or_none(mint)
            if decimals is None:
                logger.warning(f"Unknown decimals for {mint}, cannot quote")
                return None

            if side == "sell":
                # SELL: token -> SOL
                # Need a price estimate first - this is a fallback only
                # In production, caller should use get_sell_quote with actual token amount
                # Estimate token amount from size_sol at rough current price
                # This is APPROXIMATE - prefer get_sell_quote with real inventory
                price_estimate = await self.get_multiple_prices([mint])
                est_price = price_estimate.get(mint, 0.0005)
                if est_price <= 0:
                    est_price = 0.0005
                token_amount_human = size_sol / est_price
                return await self.get_sell_quote(mint, token_amount_human)
            else:
                # BUY: SOL -> token
                return await self.get_buy_quote(mint, size_sol)
        else:
            return await self.get_pumpportal_price(mint)

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

    def get_pumpportal_price(self, mint: str) -> PriceQuote:
        return self.get_price(mint)
