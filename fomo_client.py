"""
FOMO API Client for PowerTrader AI
Async client for fomoapi.io - social trading data, leaderboards, trader profiles, trades, holdings
"""

import asyncio
import logging
import json
import random
from typing import Optional, Dict, Any, List, AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
import aiohttp
from aiohttp import ClientTimeout, WSMsgType

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
FOMO_KEY_FILE = Path(__file__).parent / "fomo_key.txt"
FOMO_BASE_URL = "https://api.fomoapi.io"
FOMO_WS_URL = "wss://api.fomoapi.io/ws/alerts"

# Rate limiting
FOMO_RATE_LIMIT = 60  # requests per minute per IP
FOMO_REQUEST_INTERVAL = 60.0 / FOMO_RATE_LIMIT  # ~1 second between requests

# 429 Retry Configuration
FOMO_MAX_RETRIES = 3
FOMO_BASE_RETRY_DELAY = 1.0  # seconds
FOMO_MAX_RETRY_DELAY = 60.0  # seconds

# Timeouts
DEFAULT_TIMEOUT = ClientTimeout(total=30)
WS_TIMEOUT = 30

# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------
class FOMOError(Exception):
    """Base exception for FOMO API errors"""
    pass

class FOMOAuthError(FOMOError):
    """Authentication failed (401)"""
    pass

class FOMORateLimitError(FOMOError):
    """Rate limited (429)"""
    pass

class FOMONotFoundError(FOMOError):
    """Resource not found (404)"""
    pass

class FOMOServerError(FOMOError):
    """Server error (5xx)"""
    pass

class FOMOConnectionError(FOMOError):
    """Connection/timeout error"""
    pass

# -----------------------------------------------------------------------------
# Data Models (Internal Representation)
# -----------------------------------------------------------------------------
@dataclass
class FOMOTrader:
    """Normalized trader representation"""
    rank: int
    handle: str
    display_name: str
    pnl_usd: float
    volume_usd: float
    trades: int
    followers: int
    verified: bool = False
    holdings: int = 0
    wallets: Dict[str, Any] = field(default_factory=dict)
    pnl_by_window: Dict[str, float] = field(default_factory=dict)
    account_age_days: int = 0
    created_at: str = ""
    average_hold_time_seconds: int = 0
    clan: str = ""

@dataclass
class FOMOTrade:
    """Normalized trade representation"""
    trade_id: str
    token_symbol: str
    token_address: str
    status: str  # "open" or "closed"
    avg_entry_price: float
    avg_exit_price: Optional[float]
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    created_at: str
    closed_at: Optional[str]
    chain: str = ""

@dataclass
class FOMOHolding:
    """Normalized holding representation"""
    token_symbol: str
    token_address: str
    network_id: int
    chain: str
    amount: float
    total_value_usd: float

@dataclass
class FOMOLeaderboard:
    """Normalized leaderboard representation"""
    window: str
    source: str
    captured_at: str
    count: int
    traders: List[FOMOTrader]

# -----------------------------------------------------------------------------
# FOMO Client
# -----------------------------------------------------------------------------
class FOMOClient:
    """
    Async client for FOMO API (fomoapi.io)
    
    Features:
    - Bearer token authentication
    - Automatic rate limiting (60 req/min)
    - Exponential backoff on 429
    - TTL caching for leaderboard/trader data
    - WebSocket support for real-time alerts
    - Structured error handling
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = FOMO_BASE_URL,
        ws_url: str = FOMO_WS_URL,
        timeout: ClientTimeout = DEFAULT_TIMEOUT,
        cache_ttl: int = 60  # seconds
    ):
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        
        # Load API key
        if api_key:
            self.api_key = api_key.strip()
        else:
            self.api_key = self._load_key_from_file()
        
        if not self.api_key:
            raise FOMOAuthError("FOMO API key not found. Create fomo_key.txt or pass api_key parameter.")
        
        # Session management
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_session: Optional[aiohttp.ClientWebSocketResponse] = None
        
        # Rate limiting
        self._last_request_time = 0.0
        self._rate_limit_lock = asyncio.Lock()
        
        # Caching
        self._cache: Dict[str, tuple] = {}  # key -> (data, expiry_time)
        self._cache_lock = asyncio.Lock()
        
        # WebSocket state
        self._ws_connected = False
        self._ws_reconnect_task: Optional[asyncio.Task] = None
        self._ws_message_queue: asyncio.Queue = asyncio.Queue()
        self._ws_subscriptions: set = set()
    
    def _load_key_from_file(self) -> Optional[str]:
        """Load API key from fomo_key.txt"""
        try:
            if FOMO_KEY_FILE.exists():
                return FOMO_KEY_FILE.read_text().strip()
        except Exception as e:
            logger.error(f"Failed to read FOMO key file: {e}")
        return None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session
    
    async def close(self):
        """Close all connections"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
            self._ws_session = None
        
        if self._ws_reconnect_task:
            self._ws_reconnect_task.cancel()
            try:
                await self._ws_reconnect_task
            except asyncio.CancelledError:
                pass
    
    def _get_cache_key(self, method: str, path: str, params: dict = None) -> str:
        """Generate cache key"""
        import hashlib
        key_data = f"{method}:{path}:{json.dumps(params or {}, sort_keys=True)}"
        return hashlib.md5(key_data.encode()).hexdigest()
    
    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached value if not expired"""
        if key in self._cache:
            data, expiry = self._cache[key]
            if asyncio.get_event_loop().time() < expiry:
                return data
            else:
                del self._cache[key]
        return None
    
    def _set_cache(self, key: str, data: Any):
        """Set cache with TTL"""
        expiry = asyncio.get_event_loop().time() + self.cache_ttl
        self._cache[key] = (data, expiry)
    
    async def _rate_limit(self):
        """Enforce rate limiting"""
        async with self._rate_limit_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < FOMO_REQUEST_INTERVAL:
                await asyncio.sleep(FOMO_REQUEST_INTERVAL - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()
    
    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with auth"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    
    async def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """Make authenticated HTTP request with rate limiting and caching"""
        await self._rate_limit()
        
        # Check cache for GET requests
        cache_key = None
        if method == "GET" and use_cache:
            cache_key = self._get_cache_key(method, path, params)
            cached = self._get_cached(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit: {path}")
                return cached
        
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        
        try:
            # Retry loop for 429 handling
            attempt = 0
            while True:
                async with session.request(
                    method, url, headers=self._build_headers(), params=params
                ) as resp:
                    # Handle rate limiting with bounded exponential backoff
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "60"))
                        logger.warning(f"FOMO rate limited, retry after {retry_after}s (attempt {attempt + 1}/{FOMO_MAX_RETRIES})")
                        
                        if attempt >= FOMO_MAX_RETRIES:
                            raise FOMORateLimitError(f"Rate limited after {FOMO_MAX_RETRIES} retries, retry after {retry_after}s")
                        
                        # Calculate delay: exponential backoff with cap, respect Retry-After
                        delay = min(
                            FOMO_BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 0.5),
                            FOMO_MAX_RETRY_DELAY
                        )
                        delay = max(delay, retry_after)  # Respect server's Retry-After if longer
                        logger.info(f"Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue  # Retry the request
                    
                    # Handle auth errors
                    if resp.status == 401:
                        raise FOMOAuthError("Invalid or expired API key")
                    
                    if resp.status == 403:
                        raise FOMOAuthError("Access forbidden - check API key permissions")
                    
                    if resp.status == 404:
                        raise FOMONotFoundError(f"Resource not found: {path}")
                    
                    if resp.status >= 500:
                        raise FOMOServerError(f"FOMO server error: {resp.status}")
                    
                    if resp.status != 200:
                        text = await resp.text()
                        raise FOMOError(f"API error {resp.status}: {text[:200]}")
                    
                    data = await resp.json()
                    
                    # Cache successful GET responses
                    if method == "GET" and use_cache and cache_key:
                        self._set_cache(cache_key, data)
                    
                    return data
                
        except asyncio.TimeoutError:
            raise FOMOConnectionError(f"Request timeout: {path}")
        except aiohttp.ClientError as e:
            raise FOMOConnectionError(f"Connection error: {e}")
    
    # -------------------------------------------------------------------------
    # Public API Methods
    # -------------------------------------------------------------------------
    
    async def get_leaderboard(
        self,
        window: str = "24h",
        limit: int = 50,
        use_cache: bool = True
    ) -> FOMOLeaderboard:
        """
        Get leaderboard for a time window
        
        Args:
            window: "24h", "7d", "30d", or "all"
            limit: Max rows (1-150, max 100 for "all")
            use_cache: Whether to use cached data
        
        Returns:
            FOMOLeaderboard with normalized traders
        """
        valid_windows = ["24h", "7d", "30d", "all"]
        if window not in valid_windows:
            raise ValueError(f"Invalid window: {window}. Must be one of {valid_windows}")
        
        if window == "all":
            limit = min(limit, 100)
        else:
            limit = min(limit, 150)
        
        params = {"limit": limit}
        data = await self._request("GET", f"/v2/leaderboard/{window}", params=params, use_cache=use_cache)
        
        traders = []
        for t in data.get("traders", []):
            traders.append(FOMOTrader(
                rank=t.get("rank", 0),
                handle=t.get("handle", ""),
                display_name=t.get("displayName", ""),
                pnl_usd=float(t.get("pnlUsd", 0)),
                volume_usd=float(t.get("volumeUsd", 0)),
                trades=int(t.get("trades", 0)),
                followers=int(t.get("followers", 0)),
                verified=t.get("verified", False),
                holdings=int(t.get("holdings", 0)),
            ))
        
        return FOMOLeaderboard(
            window=data.get("window", window),
            source=data.get("source", "fomo"),
            captured_at=data.get("capturedAt", ""),
            count=data.get("count", len(traders)),
            traders=traders
        )
    
    async def get_trader(self, handle: str, use_cache: bool = True) -> FOMOTrader:
        """
        Get detailed trader profile
        
        Args:
            handle: Trader handle (e.g., "CryptoKaleo")
            use_cache: Whether to use cached data
        
        Returns:
            FOMOTrader with full profile
        """
        data = await self._request("GET", f"/v2/users/{handle}", use_cache=use_cache)
        
        pnl_by_window = {}
        pnl_data = data.get("pnl", {})
        for window, value in pnl_data.items():
            pnl_by_window[window] = float(value) if value else 0.0
        
        wallets = data.get("wallets", {})
        
        return FOMOTrader(
            rank=0,  # Not in leaderboard context
            handle=data.get("handle", handle),
            display_name=data.get("displayName", ""),
            pnl_usd=float(data.get("pnlUsd") or 0),
            volume_usd=float(data.get("volumeUsd") or 0),
            trades=int(data.get("trades") or 0),
            followers=int(data.get("followers") or 0),
            verified=data.get("verified", False),
            holdings=int(data.get("holdings") or 0),
            wallets=wallets,
            pnl_by_window=pnl_by_window,
            account_age_days=int(data.get("accountAgeDays") or 0),
            created_at=data.get("createdAt", ""),
            average_hold_time_seconds=int(data.get("averageHoldTimeSeconds") or 0),
            clan=data.get("clan", ""),
        )
    
    async def get_trades(
        self,
        handle: str,
        limit: int = 25,
        deep: bool = False,
        use_cache: bool = True
    ) -> List[FOMOTrade]:
        """
        Get trader's trade history
        
        Args:
            handle: Trader handle
            limit: Max trades (1-25, FOMO caps at 25 for closed trades)
            deep: If True, use ?deep=1 for more closed trades (costs more credits)
            use_cache: Whether to use cached data
        
        Returns:
            List of FOMOTrade objects
        """
        params = {"limit": min(limit, 25)}
        if deep:
            params["deep"] = 1
        
        data = await self._request("GET", f"/v2/users/{handle}/trades", params=params, use_cache=use_cache)
        
        trades = []
        for t in data.get("trades", []):
            token = t.get("token", {})
            avg_entry = t.get("avgEntryPrice")
            avg_exit = t.get("avgExitPrice")
            trades.append(FOMOTrade(
                trade_id=t.get("tradeId", ""),
                token_symbol=token.get("symbol", ""),
                token_address=token.get("address", ""),
                status=t.get("status", "unknown"),
                avg_entry_price=float(avg_entry) if avg_entry is not None else 0.0,
                avg_exit_price=float(avg_exit) if avg_exit is not None else None,
                realized_pnl_usd=float(t.get("realizedPnlUsd", 0)),
                unrealized_pnl_usd=float(t.get("unrealizedPnlUsd", 0)),
                created_at=t.get("createdAt", ""),
                closed_at=t.get("closedAt"),
                chain=t.get("chain", ""),
            ))
        
        return trades
    
    async def get_holdings(
        self,
        handle: str,
        chain: Optional[str] = None,
        use_cache: bool = True
    ) -> List[FOMOHolding]:
        """
        Get trader's current holdings
        
        Args:
            handle: Trader handle
            chain: Optional chain filter (robinhood, solana, base, bsc, eth)
            use_cache: Whether to use cached data
        
        Returns:
            List of FOMOHolding objects
        """
        params = {}
        if chain:
            params["chain"] = chain
        
        data = await self._request("GET", f"/v2/users/{handle}/balances", params=params, use_cache=use_cache)
        
        holdings = []
        for h in data.get("holdings", []):
            token = h.get("token", {})
            holdings.append(FOMOHolding(
                token_symbol=token.get("symbol", ""),
                token_address=token.get("address", ""),
                network_id=token.get("networkId", 0),
                chain=h.get("chain", ""),
                amount=float(h.get("amount", 0)),
                total_value_usd=float(h.get("totalValueUsd", 0)),
            ))
        
        return holdings
    
    async def get_wallets(self, handle: str) -> Dict[str, Any]:
        """
        Get trader's wallet addresses (from trader profile)
        
        Args:
            handle: Trader handle
        
        Returns:
            Dict with wallet info per chain
        """
        trader = await self.get_trader(handle)
        return trader.wallets
    
    # -------------------------------------------------------------------------
    # WebSocket Methods
    # -------------------------------------------------------------------------
    
    async def ws_connect(
        self,
        trader: Optional[str] = None,
        chain: Optional[str] = None,
        token: Optional[str] = None,
        source: Optional[str] = None,
        type_filter: Optional[str] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Connect to FOMO WebSocket and yield messages
        
        Args:
            trader: Subscribe to specific trader
            chain: Filter by chain (robinhood, solana, base, bsc, eth, etc.)
            token: Filter by token symbol or address
            source: Filter by source (feed, push)
            type_filter: Filter by type (buy, sell, thesis, whale, price, trade)
        
        Yields:
            Parsed WebSocket messages
        
        Note: Free tier has 15s delay after 7 days, 60s delay without key
        """
        # Build WebSocket URL with query params and auth
        ws_url = self.ws_url
        params = []
        if self.api_key:
            params.append(f"key={self.api_key}")
        if trader:
            params.append(f"trader={trader}")
        if chain:
            params.append(f"chain={chain}")
        if token:
            params.append(f"token={token}")
        if source:
            params.append(f"source={source}")
        if type_filter:
            params.append(f"type={type_filter}")
        
        if params:
            ws_url += "?" + "&".join(params)
        
        logger.info(f"Connecting to FOMO WebSocket: {self.ws_url}")
        
        session = await self._get_session()
        
        # Connect with exponential backoff
        backoff = 1
        max_backoff = 60
        
        while True:
            try:
                self._ws_session = await session.ws_connect(
                    ws_url,
                    heartbeat=WS_TIMEOUT,
                    autoping=True,
                    autoclose=True,
                )
                self._ws_connected = True
                logger.info("FOMO WebSocket connected")
                backoff = 1  # Reset backoff on successful connection
                
                async for msg in self._ws_session:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            # Handle welcome message (string)
                            if isinstance(data, str):
                                logger.debug(f"FOMO WS welcome: {data}")
                                continue
                            yield data
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse WS message: {e}")
                    elif msg.type == WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {self._ws_session.exception()}")
                        break
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE):
                        logger.info("WebSocket closed")
                        break
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
            
            self._ws_connected = False
            
            # Exponential backoff before reconnect
            logger.info(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
    
    async def ws_subscribe_trader(self, handle: str):
        """Send subscribe message for a specific trader"""
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.send_json({
                "action": "subscribe",
                "trader": handle
            })
            self._ws_subscriptions.add(handle)
    
    async def ws_unsubscribe(self):
        """Unsubscribe from trader filter, return to full firehose"""
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.send_json({"action": "unsubscribe"})
            self._ws_subscriptions.clear()
    
    async def ws_close(self):
        """Close WebSocket connection"""
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
        self._ws_connected = False
        self._ws_subscriptions.clear()
    
    @property
    def is_ws_connected(self) -> bool:
        return self._ws_connected and self._ws_session is not None and not self._ws_session.closed


# -----------------------------------------------------------------------------
# Global client instance (singleton pattern for app)
# -----------------------------------------------------------------------------
_fomo_client: Optional[FOMOClient] = None


async def get_fomo_client() -> FOMOClient:
    """Get or create global FOMO client instance"""
    global _fomo_client
    if _fomo_client is None:
        _fomo_client = FOMOClient()
    return _fomo_client


async def close_fomo_client():
    """Close global FOMO client"""
    global _fomo_client
    if _fomo_client:
        await _fomo_client.close()
        _fomo_client = None


# -----------------------------------------------------------------------------
# Demo / Test
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    async def test():
        client = FOMOClient()
        try:
            # Test leaderboard
            lb = await client.get_leaderboard("24h", limit=5)
            print(f"Leaderboard ({lb.window}): {lb.count} traders")
            for t in lb.traders:
                print(f"  #{t.rank} {t.handle} ({t.display_name}) - PnL: ${t.pnl_usd:,.0f}, Followers: {t.followers:,}")
            
            # Test trader profile
            if lb.traders:
                handle = lb.traders[0].handle
                trader = await client.get_trader(handle)
                print(f"\nTrader: {trader.handle} ({trader.display_name})")
                print(f"  PnL: ${trader.pnl_usd:,.0f}")
                print(f"  PnL by window: {trader.pnl_by_window}")
                print(f"  Wallets: {list(trader.wallets.keys())}")
                
                # Test trades
                trades = await client.get_trades(handle, limit=3)
                print(f"\nRecent trades ({len(trades)}):")
                for trade in trades:
                    print(f"  {trade.token_symbol} {trade.status} - Entry: ${trade.avg_entry_price:.4f}, PnL: ${trade.realized_pnl_usd:,.0f}")
                
                # Test holdings
                holdings = await client.get_holdings(handle)
                print(f"\nHoldings ({len(holdings)}):")
                for h in holdings[:5]:
                    print(f"  {h.token_symbol}: {h.amount:,.2f} (${h.total_value_usd:,.0f})")
        
        finally:
            await client.close()
    
    asyncio.run(test())