"""
Helius Enrichment - On-chain data enrichment with caching and credit governance.

Implements selective on-chain enrichment with aggressive caching and credit budget enforcement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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
            try:
                async with session.post(self.rpc_url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            # Record credit usage
                            self.governor.record_usage(method)
                            return data["result"]
                        elif "error" in data:
                            logger.warning(f"RPC error: {data['error']}")
                    elif resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning(f"Rate limited, waiting {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status} from Helius")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout calling {method} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Error calling {method}: {e}")
            
            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        
        logger.error(f"Failed {method} after {self.max_retries} retries")
        return None
    
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
            info = {
                "mint": mint,
                "supply": int(parsed.get("supply", 0)),
                "decimals": parsed.get("decimals", 0),
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
    
    # --- Enrichment Pipeline ---
    
    async def enrich_candidate(self, candidate) -> Optional[Dict]:
        """
        Fully enrich a candidate with on-chain data.
        
        Target: ~5 RPC calls per candidate, ~5,440 credits/day for 1,088 candidates.
        """
        mint = candidate.mint
        enrichment = {}
        credits_used = 0
        
        try:
            # 1. Token info (IMMUTABLE) - 1 call
            token_info = await self.get_token_info(mint)
            if not token_info:
                logger.warning(f"Token info not found for {mint}")
                return None
            enrichment.update(token_info)
            credits_used += 1
            
            # 2. Largest accounts (SEMISTATIC) - 1 call
            largest = await self.get_token_largest_accounts(mint, limit=20)
            enrichment["largest_accounts"] = largest
            credits_used += 1
            
            # 3. Creator analysis - get creator from candidate
            creator = candidate.enrichment_creator if hasattr(candidate, 'enrichment_creator') else None
            if creator:
                # 3a. Creator signatures - 1 call
                sigs = await self.get_signatures_for_address(creator, limit=50)
                enrichment["creator_signatures"] = sigs
                credits_used += 1
                
                # 3b. Creator token accounts - 1 call
                creator_accounts = await self.get_token_accounts_by_owner(creator)
                enrichment["creator_accounts"] = creator_accounts
                credits_used += 1
            
            # 4. Holder distribution from largest accounts
            if "largest_accounts" in enrichment:
                enrichment["holder_analysis"] = self._analyze_holders(enrichment["largest_accounts"])
            
            enrichment["enrichment_credits"] = credits_used
            enrichment["enriched_at"] = time.time()
            
            return enrichment
            
        except Exception as e:
            logger.error(f"Enrichment failed for {mint}: {e}")
            return None
    
    def _analyze_holders(self, largest_accounts: List[Dict]) -> Dict:
        """Analyze holder concentration from largest accounts."""
        if not largest_accounts:
            return {}
        
        total_supply = sum(acc.get("amount", 0) for acc in largest_accounts)
        if total_supply == 0:
            return {}
        
        amounts = [acc.get("amount", 0) for acc in largest_accounts]
        amounts.sort(reverse=True)
        
        top_1 = amounts[0] / total_supply if amounts else 0
        top_5 = sum(amounts[:5]) / total_supply if len(amounts) >= 5 else sum(amounts) / total_supply
        top_10 = sum(amounts[:10]) / total_supply if len(amounts) >= 10 else 1.0
        
        return {
            "total_holders_in_sample": len(largest_accounts),
            "top_1_holder_pct": round(top_1 * 100, 2),
            "top_5_holders_pct": round(top_5 * 100, 2),
            "top_10_holders_pct": round(top_10 * 100, 2),
            "total_supply_sampled": total_supply,
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