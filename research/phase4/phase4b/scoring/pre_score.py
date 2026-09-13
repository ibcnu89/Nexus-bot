"""
Pre-Scoring - Cheap local filtering to reduce candidates before enrichment.

Implements deterministic, explainable pre-scoring to reduce ~36K candidates/day
to ~1K for Helius enrichment.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set

from ..discovery.models import Candidate, DiscoveryEvent

logger = logging.getLogger(__name__)


class PreScorer:
    """
    Cheap local pre-scoring to filter candidates before expensive enrichment.
    
    Goals:
    - Deterministic and explainable
    - Reduce ~36K candidates/day to ~1K (top ~3%)
    - Fast (<1ms per candidate)
    - Uses only data available from discovery payload
    """
    
    def __init__(
        self,
        target_enrichment_pct: float = 0.03,  # 3%
        min_score_threshold: float = 0.0,
        max_candidates_per_day: int = 1500,
    ):
        self.target_enrichment_pct = target_enrichment_pct
        self.min_score_threshold = min_score_threshold
        self.max_candidates_per_day = max_candidates_per_day
        
        # Feature weights (must sum to 1.0 for interpretability)
        self.weights = {
            "metadata_completeness": 0.25,
            "creator_novelty": 0.20,
            "symbol_quality": 0.15,
            "uri_quality": 0.15,
            "creator_history": 0.15,
            "burst_penalty": -0.10,  # Penalty
        }
        
        # Track creator history
        self._creator_launches: Dict[str, List[float]] = defaultdict(list)
        self._symbol_counts: Dict[str, int] = defaultdict(int)
        self._uri_domains: Dict[str, int] = defaultdict(int)
        
        # Known scam patterns
        self._scam_symbols: Set[str] = {
            "rug", "scam", "honeypot", "fake", "test", "ponzi",
            "bitcoin", "ethereum", "solana", "usdc", "usdt",
        }
        self._scam_words: Set[str] = {
            "rug", "scam", "honeypot", "fake", "ponzi", "exit",
            "drain", "steal", "phish", "wallet", "seed", "phrase",
        }
    
    def score(self, event: "DiscoveryEvent") -> Dict[str, float]:
        """
        Score a discovery event and return component scores.
        
        Returns dict of component_name -> score (0-1 or negative for penalties).
        """
        components = {}
        
        # 1. Metadata completeness (0-1)
        components["metadata_completeness"] = self._score_metadata_completeness(event)
        
        # 2. Creator novelty (0-1) - new creators score higher
        components["creator_novelty"] = self._score_creator_novelty(event)
        
        # 3. Symbol quality (0-1)
        components["symbol_quality"] = self._score_symbol_quality(event)
        
        # 4. URI quality (0-1)
        components["uri_quality"] = self._score_uri_quality(event)
        
        # 5. Creator history (0-1) - repeated launches penalized
        components["creator_history"] = self._score_creator_history(event)
        
        # 6. Burst penalty (negative)
        components["burst_penalty"] = self._score_burst_penalty(event)
        
        return components
    
    def _score_metadata_completeness(self, event) -> float:
        """Score based on how complete the discovery metadata is."""
        fields = [
            event.symbol,
            event.name,
            event.creator,
            event.uri,
            event.bonding_curve_key,
        ]
        filled = sum(1 for f in fields if f)
        return filled / len(fields)
    
    def _score_creator_novelty(self, event) -> float:
        """Score based on whether creator is new or known."""
        if not event.creator:
            return 0.5  # Unknown creator
        
        launches = self._creator_launches.get(event.creator, [])
        if not launches:
            return 1.0  # First time seeing this creator
        
        # Decay over time - older launches matter less
        now = time.time()
        recent_launches = sum(1 for t in launches if now - t < 86400)  # Last 24h
        
        if recent_launches == 0:
            return 0.8
        elif recent_launches <= 2:
            return 0.5
        else:
            return 0.2  # Serial launcher
    
    def _score_symbol_quality(self, event) -> float:
        """Score based on symbol quality and uniqueness."""
        if not event.symbol:
            return 0.3
        
        symbol = event.symbol.upper()
        
        # Check for scam indicators
        for word in self._scam_words:
            if word in symbol.lower():
                return 0.0
        
        # Check for copycat symbols
        if symbol in self._scam_symbols:
            return 0.1
        
        # Check symbol frequency
        count = self._symbol_counts.get(symbol, 0)
        if count > 10:
            return 0.2  # Very common symbol
        elif count > 3:
            return 0.5
        else:
            return 0.8  # Unique symbol
    
    def _score_uri_quality(self, event) -> float:
        """Score based on URI quality and domain."""
        if not event.uri:
            return 0.3
        
        uri = event.uri.lower()
        
        # Check for suspicious patterns
        if any(word in uri for word in ["rug", "scam", "honeypot", "fake", "phish"]):
            return 0.0
        
        # Check domain reputation
        try:
            from urllib.parse import urlparse
            domain = urlparse(uri).netloc
            if domain:
                domain_count = self._uri_domains.get(domain, 0)
                if domain_count > 5:
                    return 0.3  # Mass-produced domains
                elif domain_count > 1:
                    return 0.6
                else:
                    return 0.8
        except Exception:
            pass
        
        # Basic URI validation
        if uri.startswith(("http://", "https://")):
            return 0.7
        elif uri.startswith("ipfs://"):
            return 0.6
        else:
            return 0.4
    
    def _score_creator_history(self, event) -> float:
        """Score based on creator's historical behavior."""
        if not event.creator:
            return 0.5
        
        launches = self._creator_launches.get(event.creator, [])
        total_launches = len(launches)
        
        if total_launches == 0:
            return 1.0
        elif total_launches <= 3:
            return 0.7
        elif total_launches <= 10:
            return 0.4
        else:
            return 0.1  # Prolific creator = higher risk
    
    def _score_burst_penalty(self, event) -> float:
        """Penalty for burst creation behavior."""
        if not event.creator:
            return 0.0
        
        launches = self._creator_launches.get(event.creator, [])
        now = time.time()
        recent = sum(1 for t in launches if now - t < 3600)  # Last hour
        
        if recent >= 5:
            return -1.0  # Heavy burst
        elif recent >= 3:
            return -0.5
        elif recent >= 2:
            return -0.2
        return 0.0
    
    def compute_score(self, components: Dict[str, float]) -> float:
        """Compute weighted total score from components."""
        score = 0.0
        for component, value in components.items():
            weight = self.weights.get(component, 0)
            score += weight * value
        return max(0.0, score)  # Floor at 0
    
    def evaluate(self, event: "DiscoveryEvent") -> Candidate:
        """
        Evaluate a discovery event and return a scored Candidate.
        
        Updates internal state and returns Candidate with pre-score.
        """
        # Score components
        components = self.score(event)
        total_score = self.compute_score(components)
        
        # Create candidate
        candidate = Candidate(
            mint=event.mint,
            symbol=event.symbol or "UNKNOWN",
            name=event.name or "Unknown Token",
            first_discovered=event.receive_timestamp,
            discovery_source=event.source,
            discovery_event=event.to_dict(),
            pre_score=total_score,
            pre_score_components=components,
            pre_score_reason=self._explain_score(components),
            # Admission is assigned by select_for_enrichment(), not by a zero threshold.
            pre_score_passed=False,
            pre_score_timestamp=time.time(),
        )
        
        # Update internal state
        self._update_state(event)
        
        return candidate

    def enrichment_quota(self, candidates: List[Candidate]) -> int:
        """Return the hard cumulative admission quota for the scored cohort."""
        scored_count = sum(1 for candidate in candidates if candidate.pre_score_timestamp > 0)
        return min(
            self.max_candidates_per_day,
            int(scored_count * self.target_enrichment_pct),
        )

    def select_for_enrichment(self, candidates: List[Candidate]) -> Set[str]:
        """Reserve only the remaining cumulative top-fraction enrichment slots."""
        scored = [c for c in candidates if c.pre_score_timestamp > 0]
        quota = self.enrichment_quota(scored)
        if quota <= 0:
            return set()

        eligible = [c for c in scored if c.pre_score >= self.min_score_threshold]
        admitted = {
            c.mint for c in eligible
            if c.pre_score_passed or c.enrichment_attempts > 0 or c.enrichment_status != "pending"
        }
        remaining_slots = max(0, quota - len(admitted))
        if remaining_slots == 0:
            return admitted

        pending = [c for c in eligible if c.mint not in admitted]
        ranked = sorted(pending, key=lambda c: (-c.pre_score, c.first_discovered, c.mint))
        admitted.update(candidate.mint for candidate in ranked[:remaining_slots])
        return admitted
    
    def _explain_score(self, components: Dict[str, float]) -> str:
        """Generate human-readable explanation of score."""
        parts = []
        for name, value in components.items():
            weight = self.weights.get(name, 0)
            if weight != 0:
                contribution = weight * value
                parts.append(f"{name}={value:.2f}×{weight:.2f}={contribution:.3f}")
        return "; ".join(parts)
    
    def _update_state(self, event) -> None:
        """Update internal tracking state."""
        now = time.time()
        
        if event.creator:
            self._creator_launches[event.creator].append(now)
            # Keep only last 7 days
            cutoff = time.time() - 7 * 86400
            self._creator_launches[event.creator] = [
                t for t in self._creator_launches[event.creator] if t > cutoff
            ]
        
        if event.symbol:
            symbol = event.symbol.upper()
            self._symbol_counts[symbol] += 1
        
        if event.uri:
            try:
                from urllib.parse import urlparse
                domain = urlparse(event.uri).netloc
                if domain:
                    self._uri_domains[domain] += 1
            except Exception:
                pass
    
    def get_stats(self) -> dict:
        return {
            "tracked_creators": len(self._creator_launches),
            "unique_symbols": len(self._symbol_counts),
            "unique_domains": len(self._uri_domains),
            "weights": self.weights,
            "threshold": self.min_score_threshold,
        }
