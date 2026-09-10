"""
Price Source - Jupiter + PumpPortal price feeds for executable price realism.

Provides realistic price quotes using Jupiter's swap API for actual swap routes,
including price impact, fees, and slippage estimation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
import logging

logger = logging.getLogger(__name__)


@dataclass
class PriceQuote:
    """Realistic executable price quote."""
    price: float                    # Price per token in SOL
    price_impact_pct: float         # Estimated price impact
    out_amount: float               # Raw output amount (lamports for SOL, base units for tokens)
    in_amount: float                # Input amount
    route: str                      # Route description (Jupiter, PumpPortal, Raydium, etc.)
    timestamp: float = field(default_factory=time.time)

    @property
    def executable_price(self) -> float:
        """Price after accounting for price impact."""
        return self.price * (1 - self.price_impact_pct)

    @property
    def net_price_after_fees(self) -> float:
        """Price after DEX fees (0.25% typical Raydium) and priority fee."""
        dex_fee_pct = 0.0025  # 0.25% Raydium fee
        return self.executable_price * (1 - dex_fee_pct)


# Jupiter API endpoints
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_PRICE_API = "https://price.jup.ag/v4/price"
JUPITER_TOKENS_API = "https://token.jup.ag/all"

# PumpPortal API
PUMPPORTAL_TRADE_API = "https://pumpportal.fun/api/trade-local"
PUMPPORTAL_PRICE_API = "https://pumpportal.fun/api/price"

# Solana constants
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class PriceSource:
    """
    Unified price source using Jupiter for graduated tokens and PumpPortal for bonding curve tokens.
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
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_decimals_cache: dict[str, int] = {}

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

    async def _fetch_with_retry(self, url: str, params: dict) -> dict:
        """Fetch with exponential backoff retry."""
        session = await self._get_session()
        for attempt in range(self.max_retries):
            try:
                async with session.get(url, params=params) as resp:
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
        amount: input amount in base units (lamports for SOL)
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }

        try:
            data = await self._fetch_with_retry(self.jupiter_api_url, params)

            # Parse Jupiter quote response
            out_amount = int(data["outAmount"])
            in_amount = int(data["inAmount"])
            price_impact = float(data.get("priceImpactPct", 0))
            route = data.get("routePlan", [{}])[0].get("swapInfo", {}).get("label", "Jupiter")

            # Calculate price (SOL per token)
            if output_mint == SOL_MINT:
                # Buying SOL with tokens: price = SOL out / tokens in
                price = out_amount / in_amount
            else:
                # Buying tokens with SOL: price = tokens out / SOL in
                price = in_amount / out_amount

            return PriceQuote(
                price=price,
                price_impact_pct=price_impact,
                out_amount=out_amount,
                in_amount=in_amount,
                route=route,
            )
        except Exception as e:
            logger.warning(f"Jupiter quote failed for {input_mint}->{output_mint}: {e}")
            return None

    async def get_pumpportal_price(self, mint: str) -> Optional[PriceQuote]:
        """Get price from PumpPortal for bonding curve tokens."""
        try:
            params = {"mint": mint}
            data = await self._fetch_with_retry(self.pumpportal_api_url, params)

            # PumpPortal returns price in SOL
            price = float(data.get("price", 0))
            if price <= 0:
                return None

            return PriceQuote(
                price=price,
                price_impact_pct=0.01,  # Estimate 1% impact for bonding curve
                out_amount=0,
                in_amount=0,
                route="PumpPortal",
            )
        except Exception as e:
            logger.warning(f"PumpPortal price failed for {mint}: {e}")
            return None

    async def get_price(
        self,
        mint: str,
        side: str = "sell",  # "buy" or "sell"
        size_sol: float = 0.1,
        is_graduated: bool = False,
    ) -> Optional[PriceQuote]:
        """
        Get executable price for a token.
        side: "sell" = token -> SOL, "buy" = SOL -> token
        """
        if is_graduated or mint == SOL_MINT:
            # Graduated token - use Jupiter
            if side == "sell":
                input_mint, output_mint = mint, SOL_MINT
            else:
                input_mint, output_mint = SOL_MINT, mint

            amount = int(size_sol * LAMPORTS_PER_SOL) if side == "buy" else int(size_sol * LAMPORTS_PER_SOL)

            return await self.get_jupiter_quote(input_mint, output_mint, amount)
        else:
            # Pre-graduation bonding curve token - use PumpPortal
            return await self.get_pumpportal_price(mint)

    async def get_multiple_prices(self, mints: list[str], vs_mint: str = SOL_MINT) -> dict[str, float]:
        """Get current prices for multiple tokens (simple price, not executable quote)."""
        if not mints:
            return {}

        params = {"ids": ",".join(mints), "vsToken": vs_mint}
        try:
            session = await self._get_session()
            async with session.get(JUPITER_PRICE_API, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {mint: float(info["price"]) for mint, info in data.get("data", {}).items()}
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

        # Random walk with trend
        change = random.gauss(self.trend, self.volatility)
        self.current_price *= (1 + change)
        self.current_price = max(self.current_price, 0.000001)  # Floor
        self.step += 1

        # Simulate price impact based on size
        price_impact = min(0.05, size_sol * 0.001)  # 0.1% per 0.1 SOL

        return PriceQuote(
            price=self.current_price,
            price_impact_pct=price_impact,
            out_amount=0,
            in_amount=0,
            route="Mock",
        )

    def get_jupiter_quote(self, *args, **kwargs) -> PriceQuote:
        return self.get_price("", *args, **kwargs)

    def get_pumpportal_price(self, mint: str) -> PriceQuote:
        return self.get_price(mint)