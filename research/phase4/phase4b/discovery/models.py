"""
Discovery Models - Data structures for token discovery events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, Any, List
from enum import Enum
import time


class DiscoverySource(Enum):
    """Source of token discovery."""
    PUMPPORTAL = "pumpportal"
    SOLANA_PUBLIC_RPC = "solana_public_rpc"
    HELIUS_WS = "helius_ws"
    UNKNOWN = "unknown"


@dataclass
class DiscoveryEvent:
    """
    Raw discovery event from any source.
    
    Immutable after creation.
    """
    # Core identification
    mint: str
    source: DiscoverySource
    
    # Timestamps
    event_timestamp: float          # When the event occurred on-chain (if available)
    receive_timestamp: float        # When we received it
    
    # Token metadata from discovery
    symbol: Optional[str] = None
    name: Optional[str] = None
    creator: Optional[str] = None
    uri: Optional[str] = None
    
    # Pump.fun specific
    bonding_curve_key: Optional[str] = None
    initial_buy: Optional[float] = None
    sol_amount: Optional[float] = None
    v_tokens_in_bonding_curve: Optional[float] = None
    v_sol_in_bonding_curve: Optional[float] = None
    market_cap_sol: Optional[float] = None
    is_mayhem_mode: Optional[bool] = None
    pool: Optional[str] = None
    
    # Signature for deduplication
    signature: Optional[str] = None
    
    # Raw payload for debugging
    raw_payload: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "source": self.source.value,
            "event_timestamp": self.event_timestamp,
            "receive_timestamp": self.receive_timestamp,
            "symbol": self.symbol,
            "name": self.name,
            "creator": self.creator,
            "uri": self.uri,
            "bonding_curve_key": self.bonding_curve_key,
            "initial_buy": self.initial_buy,
            "sol_amount": self.sol_amount,
            "v_tokens_in_bonding_curve": self.v_tokens_in_bonding_curve,
            "v_sol_in_bonding_curve": self.v_sol_in_bonding_curve,
            "market_cap_sol": self.market_cap_sol,
            "is_mayhem_mode": self.is_mayhem_mode,
            "pool": self.pool,
            "signature": self.signature,
        }


@dataclass
class Candidate:
    """
    Deduplicated, pre-scored token candidate ready for enrichment.
    """
    # Core identification
    mint: str
    symbol: str
    name: str
    
    # Discovery info
    first_discovered: float
    discovery_source: DiscoverySource
    discovery_event: Optional[dict] = None
    
    # Deduplication
    duplicate_count: int = 1
    last_seen: float = 0.0
    
    # Pre-scoring
    pre_score: float = 0.0
    pre_score_components: Dict[str, float] = field(default_factory=dict)
    pre_score_reason: str = ""
    pre_score_passed: bool = False
    pre_score_timestamp: float = 0.0
    
    # Enrichment status
    enrichment_status: str = "pending"  # pending, in_progress, completed, failed
    enrichment_attempts: int = 0
    last_enrichment: float = 0.0
    enrichment_data: Optional[Dict] = None
    
    # Risk
    risk_score: float = 0.0
    risk_class: str = "unknown"
    risk_reasons: List[str] = field(default_factory=list)
    
    # Narrative
    narrative_score: float = 0.0
    narrative_category: str = "unknown"
    narrative_confidence: float = 0.0
    
    # Meta scoring
    meta_score: float = 0.0
    meta_confidence: float = 0.0
    meta_approved: bool = False
    rejection_reason: str = ""
    meta_component_breakdown: Dict = field(default_factory=dict)
    recommended_size_sol: float = 0.02
    
    # Paper trading
    paper_entered: bool = False
    paper_entry_time: float = 0.0
    
    def __post_init__(self):
        if self.pre_score_components is None:
            self.pre_score_components = {}
        if self.risk_reasons is None:
            self.risk_reasons = []
    
    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "name": self.name,
            "first_discovered": self.first_discovered,
            "discovery_source": self.discovery_source.value,
            "duplicate_count": self.duplicate_count,
            "last_seen": self.last_seen,
            "pre_score": self.pre_score,
            "pre_score_components": self.pre_score_components,
            "pre_score_reason": self.pre_score_reason,
            "pre_score_passed": self.pre_score_passed,
            "enrichment_status": self.enrichment_status,
            "risk_score": self.risk_score,
            "risk_class": self.risk_class,
            "risk_reasons": self.risk_reasons,
            "narrative_score": self.narrative_score,
            "narrative_category": self.narrative_category,
            "narrative_confidence": self.narrative_confidence,
            "meta_score": self.meta_score,
            "meta_approved": self.meta_approved,
            "rejection_reason": self.rejection_reason,
            "paper_entered": self.paper_entered,
        }


@dataclass
class EnrichedCandidate:
    """
    Candidate after on-chain enrichment.
    Contains all data needed for risk and meta scoring.
    """
    # Base candidate info
    candidate: "Candidate"
    
    # On-chain data
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    supply: Optional[int] = None
    decimals: Optional[int] = None
    
    # Creator analysis
    creator_wallet: Optional[str] = None
    creator_holdings_pct: Optional[float] = None
    creator_tx_count: int = 0
    creator_recent_launches: int = 0
    
    # Holder analysis
    top_1_holder_pct: Optional[float] = None
    top_10_holders_pct: Optional[float] = None
    holder_count: Optional[int] = None
    
    # Liquidity
    initial_liquidity_sol: Optional[float] = None
    current_liquidity_sol: Optional[float] = None
    liquidity_change_pct: Optional[float] = None
    lp_token_count: Optional[int] = None
    
    # Migration
    is_migrated: bool = False
    migration_slot: Optional[int] = None
    migration_market_cap: Optional[float] = None
    
    # Risk indicators
    suspicious_funding: bool = False
    related_wallets: List[str] = field(default_factory=list)
    creator_rapid_launches: int = 0
    supply_mutations: int = 0
    
    # Metadata
    metadata_complete: bool = False
    uri_accessible: bool = False
    
    # Enrichment metadata
    enriched_at: float = 0.0
    enrichment_calls_used: int = 0
    enrichment_cost_credits: int = 0
    
    def __post_init__(self):
        if self.related_wallets is None:
            self.related_wallets = []


class DiscoveryQueue:
    """
    Thread-safe queue for discovered candidates.
    
    Handles:
    - Deduplication
    - Pre-scoring
    - Priority ordering for enrichment
    """
    
    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self._candidates: Dict[str, Candidate] = {}
        self._lock = __import__("threading").RLock()
    
    def add_or_update(self, event: DiscoveryEvent) -> Candidate:
        """Add new candidate or update existing one. Returns the candidate."""
        import threading
        with threading.RLock():
            mint = event.mint
            now = time.time()
            
            if mint in self._candidates:
                candidate = self._candidates[mint]
                candidate.duplicate_count += 1
                candidate.last_seen = now
                return candidate
            
            # New candidate
            if len(self._candidates) >= self.max_size:
                # Evict oldest
                oldest = min(self._candidates.values(), key=lambda c: c.last_seen)
                del self._candidates[oldest.mint]
            
            candidate = Candidate(
                mint=event.mint,
                symbol=event.symbol or "UNKNOWN",
                name=event.name or "Unknown Token",
                first_discovered=event.receive_timestamp,
                discovery_source=event.source,
                discovery_event=event.to_dict(),
                last_seen=now,
            )
            self._candidates[mint] = candidate
            return candidate
    
    def get_candidate(self, mint: str) -> Optional[Candidate]:
        with __import__("threading").RLock():
            return self._candidates.get(mint)
    
    def get_pending_enrichment(self, limit: int = 100) -> list:
        """Get candidates ready for enrichment, ordered by pre-score."""
        import threading
        with threading.RLock():
            pending = [
                c for c in self._candidates.values()
                if c.pre_score_passed and c.enrichment_status == "pending"
            ]
            # Sort by pre-score descending
            pending.sort(key=lambda c: c.pre_score, reverse=True)
            return pending[:limit]
    
    def get_all(self) -> list:
        with __import__("threading").RLock():
            return list(self._candidates.values())
    
    def mark_enrichment_started(self, mint: str) -> bool:
        with __import__("threading").RLock():
            if mint in self._candidates:
                c = self._candidates[mint]
                c.enrichment_status = "in_progress"
                c.enrichment_attempts += 1
                c.last_enrichment = time.time()
                return True
            return False
    
    def mark_enrichment_complete(self, mint: str) -> bool:
        with __import__("threading").RLock():
            if mint in self._candidates:
                c = self._candidates[mint]
                c.enrichment_status = "completed"
                c.last_enrichment = time.time()
                return True
            return False
    
    def mark_enrichment_failed(self, mint: str) -> bool:
        with __import__("threading").RLock():
            if mint in self._candidates:
                c = self._candidates[mint]
                c.enrichment_status = "failed"
                return True
            return False
    
    def stats(self) -> dict:
        with __import__("threading").RLock():
            total = len(self._candidates)
            by_status = {}
            for c in self._candidates.values():
                by_status[c.enrichment_status] = by_status.get(c.enrichment_status, 0) + 1
            return {
                "total": total,
                "by_enrichment_status": by_status,
                "pre_score_passed": sum(1 for c in self._candidates.values() if c.pre_score_passed),
            }