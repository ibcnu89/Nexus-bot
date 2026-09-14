"""
Helius Enrichment - On-chain data enrichment with caching and credit governance.

Implements selective on-chain enrichment with aggressive caching and credit budget enforcement.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Dict, List, Optional, Any, Callable

import aiohttp

from ..discovery.models import Candidate, EnrichedCandidate

logger = logging.getLogger(__name__)


class CacheType(Enum):
    """Cache TTL categories."""
    IMMUTABLE = -1      # Never expires (mint auth, freeze auth, decimals)
    SEMISTATIC = 3600   # 1 hour (creator history, holder distribution)
    REALTIME = 0        # No cache (liquidity, price)


@dataclass
class CacheEntry:
    """Cache entry with TTL tracking."""
    value: Any
    expires_at: float   # 0 = never expires
    created_at: float
    access_count: int = 0


class TTLCache:
    """Thread-safe TTL cache with LRU eviction."""
    
    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = __import__("threading").RLock()
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                return None
            entry = self._cache[key]
            if entry.expires_at > 0 and time.time() > entry.expires_at:
                del self._cache[key]
                return None
            entry.access_count += 1
            # Move to end (LRU)
            self._cache.move_to_end(key)
            return entry.value
    
    def set(self, key: str, value: Any, ttl: float) -> None:
        """Set value with TTL in seconds. ttl=-1 means never expires."""
        with self._lock:
            expires_at = time.time() + ttl if ttl > 0 else 0
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)  # LRU eviction
            self._cache[key] = CacheEntry(
                value=value,
                expires_at=expires_at,
                created_at=time.time(),
            )
    
    def invalidate(self, key: str) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
    
    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            expired = sum(1 for e in self._cache.values() if e.expires_at > 0 and e.expires_at < now)
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "expired": expired,
                "hit_rate": 0.0,  # Would need hit/miss tracking
            }


class CreditGovernor:
    """
    Helius credit budget governor with projected utilization tracking.
    
    Tracks credits used per billing cycle and projects monthly utilization.
    Enforces NORMAL/WARNING/THROTTLE/HARD_STOP states.
    """
    
    def __init__(
        self,
        monthly_credit_limit: int = 1_000_000,
        billing_cycle_start_day: int = 1,  # Day of month cycle starts
        safety_ceiling_pct: float = 70.0,  # 70% ceiling
    ):
        self.monthly_credit_limit = monthly_credit_limit
        self.billing_cycle_start_day = billing_cycle_start_day
        self.safety_ceiling_pct = safety_ceiling_pct
        self.safety_ceiling_credits = int(monthly_credit_limit * safety_ceiling_pct / 100)
        
        self._credits_used = 0
        self._cycle_start = self._calculate_cycle_start()
        self._call_counts: Dict[str, int] = {}
        self._lock = __import__("threading").RLock()
        
        # Credit costs per RPC method (verified 2026-09-10)
        self.credit_costs = {
            "getAccountInfo": 1,
            "getMultipleAccounts": 1,  # per account
            "getTokenAccountsByOwner": 1,
            "getTokenSupply": 1,
            "getSignaturesForAddress": 1,
            "getTransaction": 1,
            "getProgramAccounts": 10,
            "simulateTransaction": 1,
            "getTokenLargestAccounts": 1,
        }
    
    def _calculate_cycle_start(self) -> float:
        """Calculate the start timestamp of the current billing cycle."""
        now = time.time()
        dt = time.gmtime(now)
        year, month, day = dt.tm_year, dt.tm_mon, dt.tm_mday
        
        if day >= self.billing_cycle_start_day:
            cycle_start = time.mktime(time.struct_time((year, month, self.billing_cycle_start_day, 0, 0, 0, 0, 0, 0)))
        else:
            prev_month = month - 1 if month > 1 else 12
            prev_year = year if month > 1 else year - 1
            cycle_start = time.mktime(time.struct_time((prev_year, prev_month, self.billing_cycle_start_day, 0, 0, 0, 0, 0, 0)))
        
        return cycle_start
    
    def record_usage(self, method: str, count: int = 1) -> int:
        """Record credit usage for a method call. Returns credits consumed."""
        cost_per_call = self.credit_costs.get(method, 1)
        credits = cost_per_call * count
        
        with self._lock:
            self._credits_used += credits
            self._call_counts[method] = self._call_counts.get(method, 0) + count
        
        return credits
    
    def get_state(self) -> Dict[str, Any]:
        """Get current governor state."""
        with self._lock:
            now = time.time()
            elapsed = now - self._cycle_start
            cycle_duration = self._get_cycle_duration()
            elapsed_fraction = min(elapsed / cycle_duration, 1.0) if cycle_duration > 0 else 0
            
            if elapsed_fraction > 0:
                projected_monthly = self._credits_used / elapsed_fraction
            else:
                projected_monthly = self._credits_used * (30 * 86400 / max(elapsed, 1))
            
            projected_utilization_pct = (projected_monthly / self.monthly_credit_limit) * 100
            
            if projected_utilization_pct < 50:
                state = "NORMAL"
            elif projected_utilization_pct < 65:
                state = "WARNING"
            elif projected_utilization_pct < 70:
                state = "THROTTLE"
            else:
                state = "HARD_STOP"
            
            return {
                "state": state,
                "credits_used": self._credits_used,
                "projected_monthly_credits": int(projected_monthly),
                "projected_utilization_pct": round(projected_utilization_pct, 1),
                "safety_ceiling_credits": self.safety_ceiling_credits,
                "safety_ceiling_pct": self.safety_ceiling_pct,
                "elapsed_fraction": round(elapsed_fraction, 3),
                "call_counts": dict(self._call_counts),
            }
    
    def _get_cycle_duration(self) -> float:
        """Get duration of current billing cycle in seconds."""
        # Approximate as 30 days
        return 30 * 86400
    
    def can_proceed(self, estimated_credits: int = 1) -> tuple[bool, str]:
        """Check if an operation can proceed. Returns (allowed, reason)."""
        state_info = self.get_state()
        
        if state_info["state"] == "HARD_STOP":
            return False, f"Credit governor HARD_STOP: projected utilization {state_info['projected_utilization_pct']}% >= 70%"
        
        if state_info["state"] == "THROTTLE":
            return False, f"Credit governor THROTTLE: only highest-score candidates allowed"
        
        # Check if this specific operation would push us over
        if state_info["projected_monthly_credits"] + estimated_credits > self.safety_ceiling_credits:
            return False, f"Would exceed safety ceiling ({self.safety_ceiling_pct}%)"
        
        return True, "OK"
    
    def reset_cycle(self) -> None:
        """Manually reset cycle (for testing)."""
        with self._lock:
            self._credits_used = 0
            self._call_counts.clear()
            self._cycle_start = self._calculate_cycle_start()


class HeliusClient:
    """
    Helius RPC client with caching, retries, and credit tracking.
    
    Provides methods for on-chain enrichment with automatic caching
    and credit governance.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        cache: Optional[TTLCache] = None,
        governor: Optional[CreditGovernor] = None,
    ):
        self.api_key = api_key or os.environ.get("HELIUS_API_KEY")
        self.rpc_url = rpc_url or f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.cache = cache or TTLCache(max_size=10000)
        self.governor = governor or CreditGovernor()
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_cache: Dict[str, Dict] = {}  # mint -> {decimals, supply, authorities}
        self._bonding_curve_accounts: Dict[str, tuple[str, int]] = {}
        self._last_bonding_curve_states: Dict[str, Any] = {}
        self._provider_metrics_started_at = time.time()
        self._provider_metrics = {
            "requests": 0,
            "successful_requests": 0,
            "http_429": 0,
            "timeouts": 0,
            "retries": 0,
            "governor_skips": 0,
            "failed_calls": 0,
            "latencies_ms": [],
            "request_times": [],
        }
        # RPC method credit costs
        self.credit_costs = {
            "getAccountInfo": 1,
            "getMultipleAccounts": 1,  # per account
            "getTokenAccountsByOwner": 1,
            "getTokenSupply": 1,
            "getSignaturesForAddress": 1,
            "getTransaction": 1,
            "getProgramAccounts": 10,
            "simulateTransaction": 1,
            "getTokenLargestAccounts": 1,
        }
    
    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()
        if hasattr(self, '_price_source'):
            await self._price_source.__aexit__(exc_type, exc_val, exc_tb)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session
    
    def _make_cache_key(self, method: str, params: list) -> str:
        """Create deterministic cache key."""
        import hashlib
        key_str = f"{method}:{json.dumps(params, sort_keys=True)}"
        return hashlib.sha256(key_str.encode()).hexdigest()[:32]
    
    async def _rpc_call(self, method: str, params: list) -> Optional[dict]:
        """Make RPC call with retry logic and credit tracking."""
        # Check governor
        cost = self.credit_costs.get(method, 1)
        allowed, reason = self.governor.can_proceed(cost)
        if not allowed:
            logger.warning(f"Governor blocked {method}: {reason}")
            self._provider_metrics["governor_skips"] += 1
            return None
        
        # Check cache
        cache_key = self._make_cache_key(method, params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        
        for attempt in range(self.max_retries):
            if attempt > 0:
                self._provider_metrics["retries"] += 1
            self._provider_metrics["requests"] += 1
            self._provider_metrics["request_times"].append(time.time())
            request_started = time.perf_counter()
            try:
                async with session.post(self.rpc_url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            # Record credit usage
                            self.governor.record_usage(method)
                            self._provider_metrics["successful_requests"] += 1
                            return data["result"]
                        elif "error" in data:
                            logger.warning(f"RPC error: {data['error']}")
                    elif resp.status == 429:
                        self._provider_metrics["http_429"] += 1
                        wait = 2 ** attempt
                        logger.warning(f"Rate limited, waiting {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status} from Helius")
            except asyncio.TimeoutError:
                self._provider_metrics["timeouts"] += 1
                logger.warning(f"Timeout calling {method} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Error calling {method}: {e}")
            finally:
                self._provider_metrics["latencies_ms"].append(
                    (time.perf_counter() - request_started) * 1000
                )
            
            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        
        logger.error(f"Failed {method} after {self.max_retries} retries")
        self._provider_metrics["failed_calls"] += 1
        return None

    def get_metrics(self) -> Dict[str, Any]:
        """Return measured provider and governor health for runtime reporting."""
        latencies = sorted(self._provider_metrics["latencies_ms"])
        average_latency_ms = sum(latencies) / len(latencies) if latencies else None
        p95_latency_ms = (
            latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
            if latencies else None
        )
        governor = self.governor.get_state()
        elapsed = max(time.time() - self._provider_metrics_started_at, 0.001)
        request_buckets: Dict[int, int] = {}
        for timestamp in self._provider_metrics["request_times"]:
            bucket = int(timestamp)
            request_buckets[bucket] = request_buckets.get(bucket, 0) + 1
        return {
            "requests": self._provider_metrics["requests"],
            "successful_requests": self._provider_metrics["successful_requests"],
            "http_429": self._provider_metrics["http_429"],
            "timeouts": self._provider_metrics["timeouts"],
            "retries": self._provider_metrics["retries"],
            "governor_skips": self._provider_metrics["governor_skips"],
            "failed_calls": self._provider_metrics["failed_calls"],
            "average_latency_ms": round(average_latency_ms, 2) if average_latency_ms is not None else None,
            "p95_latency_ms": round(p95_latency_ms, 2) if p95_latency_ms is not None else None,
            "average_requests_per_second": round(
                self._provider_metrics["requests"] / elapsed,
                4,
            ),
            "peak_requests_per_second": max(request_buckets.values(), default=0),
            "credits_used": governor["credits_used"],
            "governor_state": governor["state"],
            "governor": governor,
        }
    
    # --- Token Metadata ---
    
    async def get_token_info(self, mint: str) -> Optional[Dict]:
        """Get token mint info (authorities, supply, decimals) - IMMUTABLE cache."""
        cache_key = f"token_info:{mint}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        result = await self._rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if result and "value" in result and result["value"]:
            parsed = result["value"]["data"]["parsed"]["info"]
            try:
                supply = int(parsed["supply"])
                decimals = int(parsed["decimals"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Mint account is missing valid supply/decimals for %s", mint)
                return None
            if supply < 0 or isinstance(parsed.get("decimals"), bool) or not 0 <= decimals <= 18:
                logger.warning("Mint account has invalid supply/decimals for %s", mint)
                return None
            info = {
                "mint": mint,
                "supply": supply,
                "decimals": decimals,
                "mint_authority": parsed.get("mintAuthority"),
                "freeze_authority": parsed.get("freezeAuthority"),
                "is_initialized": parsed.get("isInitialized", True),
            }
            self.cache.set(f"token_info:{mint}", info, ttl=-1)  # Never expires
            return info
        return None
    
    async def get_token_supply(self, mint: str) -> Optional[int]:
        """Get token supply - IMMUTABLE cache."""
        info = await self.get_token_info(mint)
        return info["supply"] if info else None
    
    async def get_mint_authorities(self, mint: str) -> tuple[Optional[str], Optional[str]]:
        """Get mint and freeze authorities - IMMUTABLE cache."""
        info = await self.get_token_info(mint)
        if info:
            return info.get("mint_authority"), info.get("freeze_authority")
        return None, None
    
    async def get_token_decimals(self, mint: str) -> Optional[int]:
        """Get token decimals - IMMUTABLE cache."""
        info = await self.get_token_info(mint)
        return info["decimals"] if info else None
    
    # --- Holder Analysis ---
    
    async def get_token_largest_accounts(self, mint: str, limit: int = 20) -> Optional[List[Dict]]:
        """Get largest token accounts - SEMISTATIC cache (1h)."""
        cache_key = f"largest_accounts:{mint}:{limit}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        result = await self._rpc_call("getTokenLargestAccounts", [mint, {"limit": limit, "commitment": "processed"}])
        if result and "value" in result:
            accounts = result["value"]
            self.cache.set(cache_key, accounts, ttl=3600)
            return accounts
        return None
    
    async def get_token_accounts_by_owner(self, owner: str, mint: Optional[str] = None) -> Optional[List[Dict]]:
        """Get token accounts for an owner - SEMISTATIC cache (1h)."""
        params = [owner, {"commitment": "processed"}]
        if mint:
            params.append({"mint": mint})
        else:
            params.append({"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"})
        
        cache_key = f"token_accounts:{owner}:{mint or 'all'}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        result = await self._rpc_call("getTokenAccountsByOwner", params)
        if result and "value" in result:
            accounts = result["value"]
            self.cache.set(cache_key, accounts, ttl=3600)
            return accounts
        return None
    
    # --- Creator Analysis ---
    
    async def get_signatures_for_address(self, address: str, limit: int = 100) -> Optional[List[Dict]]:
        """Get transaction signatures for address - REALTIME (no cache)."""
        result = await self._rpc_call("getSignaturesForAddress", [address, {"limit": limit}])
        if result:
            return result
        return None
    
    async def get_transaction(self, signature: str) -> Optional[Dict]:
        """Get transaction details - REALTIME (no cache)."""
        result = await self._rpc_call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        return result
    
    async def get_multiple_accounts(self, addresses: List[str]) -> Optional[List[Optional[Dict]]]:
        """Get multiple accounts in batch - credits = 1 per account."""
        if not addresses:
            return []
        
        # Process in chunks of 100 (RPC limit)
        chunk_size = 100
        all_results = []
        
        for i in range(0, len(addresses), chunk_size):
            chunk = addresses[i:i + chunk_size]
            result = await self._rpc_call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
            if result and "value" in result:
                all_results.extend(result["value"])
            else:
                all_results.extend([None] * len(chunk))
        
        return all_results
    
    # --- Price Quote Methods ---

    async def _ensure_price_source(self):
        if not hasattr(self, '_price_source'):
            from research.phase4.prototype.price_source import PriceSource
            self._price_source = PriceSource()
        return self._price_source

    @staticmethod
    def parse_bonding_curve_account(
        mint: str,
        bonding_curve_key: str,
        decimals: int,
        account_value: Dict[str, Any],
        observed_at: Optional[float] = None,
    ):
        """Parse the stable prefix of Pump.fun's Anchor bonding-curve account."""
        from research.phase4.prototype.price_source import BondingCurveState

        encoded = account_value.get("data") if account_value else None
        if isinstance(encoded, (list, tuple)):
            encoded = encoded[0] if encoded else None
        if not isinstance(encoded, str):
            raise ValueError("Bonding-curve account has no base64 data")

        raw = base64.b64decode(encoded, validate=True)
        if len(raw) < 49:
            raise ValueError(f"Bonding-curve account is too short: {len(raw)} bytes")

        def u64(offset: int) -> int:
            return int.from_bytes(raw[offset:offset + 8], "little", signed=False)

        return BondingCurveState(
            mint=mint,
            bonding_curve_key=bonding_curve_key,
            token_decimals=decimals,
            virtual_token_reserves=u64(8),
            virtual_sol_reserves=u64(16),
            real_token_reserves=u64(24),
            real_sol_reserves=u64(32),
            token_total_supply=u64(40),
            complete=bool(raw[48]),
            observed_at=observed_at or time.time(),
            source="helius_getAccountInfo",
        )

    async def get_bonding_curve_state(
        self,
        mint: str,
        bonding_curve_key: str,
        decimals: int,
    ):
        """Read a fresh Pump.fun bonding-curve account and cache its reserve state."""
        result = await self._rpc_call(
            "getAccountInfo",
            [bonding_curve_key, {"encoding": "base64", "commitment": "processed"}],
        )
        if not result or not result.get("value"):
            return None
        try:
            state = self.parse_bonding_curve_account(
                mint,
                bonding_curve_key,
                decimals,
                result["value"],
            )
        except (TypeError, ValueError) as exc:
            logger.warning(f"Invalid bonding-curve account for {mint}: {exc}")
            return None

        price_source = await self._ensure_price_source()
        price_source.cache_bonding_curve_state(state)
        self._last_bonding_curve_states[mint] = state
        return state

    def _register_discovery_curve_state(self, candidate, decimals: int) -> bool:
        """Seed route state from the discovery event until a fresh RPC read succeeds."""
        from research.phase4.prototype.price_source import BondingCurveState, LAMPORTS_PER_SOL

        event = candidate.discovery_event or {}
        if event.get("pool") != "pump" or not event.get("bonding_curve_key"):
            return False

        self._bonding_curve_accounts[candidate.mint] = (event["bonding_curve_key"], decimals)
        token_reserves = event.get("v_tokens_in_bonding_curve")
        sol_reserves = event.get("v_sol_in_bonding_curve")
        if token_reserves is None or sol_reserves is None:
            return True

        try:
            state = BondingCurveState(
                mint=candidate.mint,
                bonding_curve_key=event["bonding_curve_key"],
                token_decimals=decimals,
                virtual_token_reserves=int(Decimal(str(token_reserves)) * (10 ** decimals)),
                virtual_sol_reserves=int(Decimal(str(sol_reserves)) * LAMPORTS_PER_SOL),
                observed_at=float(event.get("receive_timestamp") or candidate.first_discovered),
                source="pumpportal_discovery_event",
            )
        except (TypeError, ValueError) as exc:
            logger.warning(f"Invalid discovery curve reserves for {candidate.mint}: {exc}")
            return True

        self._last_bonding_curve_states[candidate.mint] = state
        if hasattr(self, '_price_source'):
            self._price_source.cache_bonding_curve_state(state)
        return True

    async def _refresh_registered_curve(self, mint: str):
        registration = self._bonding_curve_accounts.get(mint)
        if not registration:
            return None
        bonding_curve_key, decimals = registration
        return await self.get_bonding_curve_state(mint, bonding_curve_key, decimals)
    
    async def get_buy_quote(
        self,
        mint: str,
        sol_amount: float,
        slippage_bps: int = 50,
    ):
        """Get a venue-aware quote for BUY: SOL -> token."""
        price_source = await self._ensure_price_source()
        await self._refresh_registered_curve(mint)
        return await price_source.get_buy_quote(mint, sol_amount, slippage_bps)
    
    async def get_sell_quote(
        self,
        mint: str,
        token_amount_human: float,
        slippage_bps: int = 50,
    ):
        """Get a venue-aware quote for SELL: token -> SOL."""
        price_source = await self._ensure_price_source()
        await self._refresh_registered_curve(mint)
        return await price_source.get_sell_quote(mint, token_amount_human, slippage_bps)

    async def _ensure_price_source_decimals(self, mint: str, decimals: int) -> None:
        """Pass authoritative on-chain decimals into the quote boundary."""
        price_source = await self._ensure_price_source()
        price_source.cache_token_decimals(mint, decimals)
    
    async def get_price(
        self,
        mint: str,
        side: str = "sell",
        size_sol: float = 0.1,
        is_graduated: bool = False,
    ):
        """Get a venue-aware executable paper price."""
        price_source = await self._ensure_price_source()
        state = await self._refresh_registered_curve(mint)
        graduated = is_graduated or bool(state and state.complete)
        return await price_source.get_price(mint, side, size_sol, graduated)
    
    # --- Enrichment Pipeline ---
    
    async def enrich_candidate(self, candidate) -> Optional[Dict]:
        """
        Fully enrich a candidate with on-chain data.
        
        Target: ~5 RPC calls per candidate, ~5,440 credits/day for 1,088 candidates.
        """
        mint = candidate.mint
        enrichment = {}
        credits_before = self.governor.get_state()["credits_used"]
        
        try:
            # 1. Token info (IMMUTABLE) - 1 call
            token_info = await self.get_token_info(mint)
            if not token_info:
                logger.warning(f"Token info not found for {mint}")
                return None
            enrichment["token_info"] = token_info  # Keep nested structure
            await self._ensure_price_source_decimals(mint, token_info["decimals"])

            event_pool = (candidate.discovery_event or {}).get("pool")
            is_bonding_curve = event_pool == "pump"
            curve_registered = self._register_discovery_curve_state(
                candidate,
                token_info["decimals"],
            )
            
            # 2. Largest accounts (SEMISTATIC) - 1 call
            largest = await self.get_token_largest_accounts(mint, limit=20)
            enrichment["largest_accounts"] = largest
            
            # 3. Creator analysis - get creator from candidate
            creator = candidate.enrichment_creator if hasattr(candidate, 'enrichment_creator') else None
            if creator:
                # 3a. Creator signatures - 1 call
                sigs = await self.get_signatures_for_address(creator, limit=50)
                enrichment["creator_signatures"] = sigs
                
                # 3b. Creator token accounts - 1 call
                creator_accounts = await self.get_token_accounts_by_owner(creator)
                enrichment["creator_accounts"] = creator_accounts
            
            # 4. Holder distribution from largest accounts
            if "largest_accounts" in enrichment:
                bonding_curve_key = candidate.discovery_event.get("bonding_curve_key") if candidate.discovery_event else None
                enrichment["holder_analysis"] = await self._analyze_holders(
                    enrichment["largest_accounts"],
                    token_info,
                    bonding_curve_key,
                )
            
            # 5. Add sell quote for risk assessment
            # Use a small test quantity (1000 tokens) for sellability check
            try:
                decimals = token_info.get("decimals", 9)
                test_amount_human = 1000.0  # Small test quantity
                if is_bonding_curve and not curve_registered:
                    sell_quote = None
                else:
                    sell_quote = await self.get_sell_quote(mint, test_amount_human, slippage_bps=50)
                if sell_quote:
                    enrichment["sell_quote"] = {
                        "executable_price": sell_quote.executable_price,
                        "price_impact_pct": sell_quote.price_impact_pct,
                        "out_amount": sell_quote.out_amount,
                        "in_amount": sell_quote.in_amount,
                        "other_amount_threshold": sell_quote.other_amount_threshold,
                        "route": sell_quote.route,
                        "swap_fee_bps": sell_quote.swap_fee_bps,
                        "platform_fee_bps": sell_quote.platform_fee_bps,
                        "venue_state_source": sell_quote.venue_state_source,
                        "venue_state_observed_at": sell_quote.venue_state_observed_at,
                        "test_amount_human": test_amount_human,
                        "test_amount_atomic": int(test_amount_human * (10 ** decimals)),
                        "timestamp": time.time(),
                    }
                state = self._last_bonding_curve_states.get(mint)
                if state:
                    enrichment["bonding_curve_state"] = asdict(state)
            except Exception as e:
                logger.warning(f"Failed to get sell quote for {mint}: {e}")
            
            # 6. Add get_price method delegate for market data fetching
            enrichment["enrichment_credits"] = (
                self.governor.get_state()["credits_used"] - credits_before
            )
            enrichment["enriched_at"] = time.time()
            
            return enrichment
            
        except Exception as e:
            logger.error(f"Enrichment failed for {mint}: {e}")
            return None
    
    async def _analyze_holders(
        self,
        largest_accounts: List[Dict],
        token_info: Optional[Dict],
        bonding_curve_key: Optional[str] = None,
    ) -> Dict:
        """Analyze holder concentration from largest accounts.
        
        Uses authoritative mint supply from token_info, not sum of sampled accounts.
        Classifies protocol-controlled bonding-curve accounts separately.
        """
        mint_supply = None
        if token_info and token_info.get("supply") is not None:
            try:
                mint_supply = int(token_info["supply"])
            except (TypeError, ValueError):
                pass
        if mint_supply is not None and mint_supply <= 0:
            mint_supply = None

        account_data = []
        invalid_account_count = 0
        for account in largest_accounts or []:
            raw_amount = account.get("amount")
            try:
                if isinstance(raw_amount, bool) or raw_amount is None:
                    raise ValueError
                amount = int(raw_amount)
                if amount < 0:
                    raise ValueError
            except (TypeError, ValueError):
                amount = None
                invalid_account_count += 1
            account_data.append({
                "address": account.get("address"),
                "amount": amount,
                "owner": None,
                "classification": "unknown",
                "reason": "owner_not_resolved",
            })

        valid_amounts = [entry["amount"] for entry in account_data if entry["amount"] is not None]
        total_sampled = sum(valid_amounts) if valid_amounts else None
        missing_data_reason = None
        if not account_data:
            missing_data_reason = "largest_accounts_unavailable"
        elif invalid_account_count:
            missing_data_reason = "invalid_account_amount"
        elif mint_supply is None:
            missing_data_reason = "mint_supply_unavailable"

        # get_multiple_accounts() returns result["value"], so each item here is
        # already the account-value object. There is no additional "value" wrapper.
        if bonding_curve_key and account_data:
            addressed = [entry for entry in account_data if entry["address"]]
            account_infos = await self.get_multiple_accounts(
                [entry["address"] for entry in addressed]
            ) or []
            for entry, info in zip(addressed, account_infos):
                data = info.get("data") if isinstance(info, dict) else None
                parsed = data.get("parsed") if isinstance(data, dict) else None
                parsed_info = parsed.get("info") if isinstance(parsed, dict) else None
                owner = parsed_info.get("owner") if isinstance(parsed_info, dict) else None
                if owner:
                    entry["owner"] = owner
                    if owner == bonding_curve_key:
                        entry["classification"] = "protocol"
                        entry["reason"] = "owner_matches_bonding_curve"
                    else:
                        entry["classification"] = "non_protocol"
                        entry["reason"] = "owner_differs_from_bonding_curve"
            for entry in account_data:
                if not entry["address"]:
                    entry["reason"] = "token_account_address_missing"
        elif account_data:
            for entry in account_data:
                entry["reason"] = "bonding_curve_owner_context_unavailable"

        protocol_entries = [
            entry for entry in account_data
            if entry["classification"] == "protocol" and entry["amount"] is not None
        ]
        non_protocol_entries = [
            entry for entry in account_data
            if entry["classification"] == "non_protocol" and entry["amount"] is not None
        ]
        unclassified_count = sum(
            1 for entry in account_data if entry["classification"] == "unknown"
        )
        classification_complete = bool(account_data) and unclassified_count == 0
        protocol_total = sum(entry["amount"] for entry in protocol_entries)
        non_protocol_total = sum(entry["amount"] for entry in non_protocol_entries)
        protocol_addresses = [entry["address"] for entry in protocol_entries]
        circulating_supply = (
            mint_supply - protocol_total
            if mint_supply is not None and protocol_total <= mint_supply
            else None
        )

        def concentration(balances: List[int], count: int, denominator: Optional[int]):
            if not balances or denominator is None or denominator <= 0:
                return None
            return round(sum(sorted(balances, reverse=True)[:count]) / denominator * 100, 2)

        calculations_reliable = not invalid_account_count and mint_supply is not None
        if calculations_reliable:
            top_1_all = concentration(valid_amounts, 1, mint_supply)
            top_5_all = concentration(valid_amounts, 5, mint_supply)
            top_10_all = concentration(valid_amounts, 10, mint_supply)
        else:
            top_1_all = top_5_all = top_10_all = None

        if calculations_reliable and classification_complete:
            non_protocol_balances = [entry["amount"] for entry in non_protocol_entries]
            top_1_non_total = concentration(non_protocol_balances, 1, mint_supply)
            top_5_non_total = concentration(non_protocol_balances, 5, mint_supply)
            top_10_non_total = concentration(non_protocol_balances, 10, mint_supply)
            top_1_non_circulating = concentration(non_protocol_balances, 1, circulating_supply)
            top_5_non_circulating = concentration(non_protocol_balances, 5, circulating_supply)
            top_10_non_circulating = concentration(non_protocol_balances, 10, circulating_supply)
        else:
            top_1_non_total = top_5_non_total = top_10_non_total = None
            top_1_non_circulating = top_5_non_circulating = top_10_non_circulating = None
            if missing_data_reason is None and bonding_curve_key:
                missing_data_reason = "account_owner_unresolved"

        # Legacy fields retain percentage-of-total-supply semantics. When the
        # protocol classification is complete they exclude verified protocol
        # inventory; otherwise they use the conservative all-account measure.
        risk_values = (
            (top_1_non_total, top_5_non_total, top_10_non_total)
            if classification_complete
            else (top_1_all, top_5_all, top_10_all)
        )
        return {
            "total_holders_in_sample": len(account_data),
            "sampled_balance_total": total_sampled,
            "mint_supply": mint_supply,
            "denominator_source": "token_info.supply" if mint_supply is not None else None,
            "calculation_timestamp": time.time(),
            "protocol_controlled_balance": protocol_total,
            "protocol_accounts": protocol_addresses,
            "non_protocol_balance_total": non_protocol_total,
            "circulating_supply": circulating_supply,
            "holder_classification_complete": classification_complete,
            "unclassified_account_count": unclassified_count,
            "invalid_account_count": invalid_account_count,
            "account_classifications": account_data,
            "top_1_all_accounts_pct": top_1_all,
            "top_5_all_accounts_pct": top_5_all,
            "top_10_all_accounts_pct": top_10_all,
            "top_1_non_protocol_pct_total_supply": top_1_non_total,
            "top_5_non_protocol_pct_total_supply": top_5_non_total,
            "top_10_non_protocol_pct_total_supply": top_10_non_total,
            "top_1_non_protocol_pct_circulating_supply": top_1_non_circulating,
            "top_5_non_protocol_pct_circulating_supply": top_5_non_circulating,
            "top_10_non_protocol_pct_circulating_supply": top_10_non_circulating,
            # Transitional aliases used by existing persistence/reporting code.
            "top_1_non_protocol_pct": top_1_non_total,
            "top_5_non_protocol_pct": top_5_non_total,
            "top_10_non_protocol_pct": top_10_non_total,
            "top_1_holder_pct": risk_values[0],
            "top_5_holders_pct": risk_values[1],
            "top_10_holders_pct": risk_values[2],
            "total_supply_sampled": total_sampled,
            "missing_data_reason": missing_data_reason,
        }


class EnrichmentPipeline:
    """
    High-level enrichment pipeline coordinating Helius client,
    cache, and credit governor.
    """
    
    def __init__(
        self,
        helius_client: HeliusClient,
        governor: CreditGovernor,
        cache: TTLCache,
        max_parallel: int = 3,
    ):
        self.helius = helius_client
        self.governor = governor
        self.cache = cache
        self.max_parallel = max_parallel
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._stats = {
            "enriched": 0,
            "failed": 0,
            "skipped_governor": 0,
            "total_credits": 0,
        }
    
    async def enrich_batch(self, candidates: List) -> Dict[str, Dict]:
        """Enrich a batch of candidates in parallel."""
        results = {}
        
        async def enrich_one(candidate):
            async with self._semaphore:
                # Check governor before each
                allowed, reason = self.governor.can_proceed(5)
                if not allowed:
                    self._stats["skipped_governor"] += 1
                    logger.warning(f"Skipping {candidate.mint}: {reason}")
                    return None
                
                try:
                    enrichment = await self.helius.enrich_candidate(candidate)
                    if enrichment:
                        self._stats["enriched"] += 1
                        self._stats["total_credits"] += enrichment.get("enrichment_credits", 0)
                        return (candidate.mint, enrichment)
                    else:
                        self._stats["failed"] += 1
                        return None
                except Exception as e:
                    self._stats["failed"] += 1
                    logger.error(f"Enrichment error for {candidate.mint}: {e}")
                    return None
        
        # Process in parallel
        tasks = [enrich_one(c) for c in candidates]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        results = {}
        for r in results_list:
            if isinstance(r, tuple) and r[0]:
                results[r[0]] = r[1]
            elif isinstance(r, Exception):
                logger.error(f"Enrichment task failed: {r}")
        
        return results
    
    def get_stats(self) -> dict:
        return {
            **self._stats,
            "governor": self.governor.get_state(),
        }


# Standalone functions for direct use
async def create_helius_pipeline(
    api_key: Optional[str] = None,
    max_parallel: int = 3,
    monthly_limit: int = 1_000_000,
) -> EnrichmentPipeline:
    """Factory to create a fully configured enrichment pipeline."""
    
    cache = TTLCache(max_size=10000)
    governor = CreditGovernor(monthly_credit_limit=monthly_limit)
    helius = HeliusClient(
        api_key=api_key,
        cache=cache,
        governor=governor,
    )
    await helius.__aenter__()
    
    pipeline = EnrichmentPipeline(
        helius_client=helius,
        governor=governor,
        cache=cache,
        max_parallel=max_parallel,
    )
    
    return pipeline
