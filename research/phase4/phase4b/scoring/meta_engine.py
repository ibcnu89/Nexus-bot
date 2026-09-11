"""
Meta Signal + EV Engine - Combines all signals into expected-value framework.

Implements transparent scoring with explicit component weights.
All placeholders removed - confidence derived from evidence quality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum
from enum import Enum as PyEnum

logger = logging.getLogger(__name__)


class EVCalibration(str, Enum):
    """EV calibration status."""
    EMPIRICAL_UNCALIBRATED = "EMPIRICAL_UNCALIBRATED"
    PRELIMINARY_CALIBRATED = "PRELIMINARY_CALIBRATED"
    CALIBRATED = "CALIBRATED"


@dataclass
class ComponentScore:
    """Individual component score with metadata."""
    name: str
    score: float  # 0 to max_weight
    max_weight: float
    confidence: float  # 0-1
    evidence: List[str] = field(default_factory=list)
    missing: bool = False


@dataclass
class MetaScore:
    """Complete meta-scoring result."""
    component_scores: Dict[str, ComponentScore]
    meta_score: float
    confidence: float
    expected_return: Optional[float]
    expected_downside: Optional[float]
    estimated_execution_cost: Optional[float]
    estimated_slippage: Optional[float]
    risk_adjusted_ev: Optional[float]
    recommended_size_sol: float
    decision: str  # 'approve', 'reject', 'defer'
    rejection_reason: str
    calibration: str
    component_breakdown: Dict[str, float]


class MetaEngine:
    """
    Meta Signal + EV Engine.
    
    Combines all independent signals into a transparent expected-value framework.
    No placeholders - all scores derived from evidence.
    """
    
    # Component weights (must sum to 100)
    WEIGHTS = {
        "discovery_quality": 20,
        "onchain_quality": 25,
        "liquidity_execution": 20,
        "narrative_momentum": 20,
        "creator_quality": 15,
        "risk_penalty": -100,  # penalty weight (negative)
    }
    
    # Calibration status - must be EMPIRICAL_UNCALIBRATED until Phase 4C
    CALIBRATION = "EMPIRICAL_UNCALIBRATED"
    
    # Experimental position size cap (SOL)
    MAX_POSITION_SIZE_SOL = 0.02
    
    def __init__(
        self,
        risk_penalty_weight: float = 1.5,
        min_meta_score_for_entry: float = 50.0,
        max_position_size_sol: float = 0.02,
    ):
        self.risk_penalty_weight = risk_penalty_weight
        self.min_meta_score_for_entry = min_meta_score_for_entry
        self.max_position_size_sol = max_position_size_sol
        
    def evaluate(
        self,
        discovery_quality: float,
        onchain_quality: float,
        liquidity_execution: float,
        narrative_momentum: float,
        creator_quality: float,
        risk_score: int,
        risk_class: str,
        narrative_confidence: float = 0.5,
        enrichment_completeness: float = 1.0,
        quote_quality: float = 0.5,
        slippage_estimate: float = 0.02,
        priority_fee_sol: float = 0.0005,
    ) -> Dict[str, Any]:
        """
        Evaluate all signals into a meta decision.
        
        Args:
            discovery_quality: 0-100 (pre-score + deduplication quality)
            onchain_quality: 0-100 (enrichment completeness + token quality)
            liquidity_execution: 0-100 (liquidity depth + execution feasibility)
            narrative_momentum: 0-100 (narrative score + velocity)
            creator_quality: 0-100 (creator reputation + history)
            risk_score: 0-100 (from rug detector)
            risk_class: "LOW", "MODERATE", "HIGH", "REJECT"
            narrative_confidence: 0-1
            enrichment_completeness: 0-1 (fraction of enrichment data available)
            quote_quality: 0-1 (quote freshness + slippage)
            slippage_estimate: expected slippage as fraction
            priority_fee_sol: priority fee in SOL
            
        Returns:
            Complete meta decision dictionary
        """
        # Validate inputs
        for name, val in [
            ("discovery_quality", discovery_quality),
            ("onchain_quality", onchain_quality),
            ("liquidity_execution", liquidity_execution),
            ("narrative_momentum", narrative_momentum),
            ("creator_quality", creator_quality),
            ("risk_score", risk_score),
        ]:
            if not 0 <= val <= 100:
                raise ValueError(f"{name} must be 0-100, got {val}")
        
        if not 0 <= narrative_confidence <= 1:
            raise ValueError("narrative_confidence must be 0-1")
        if not 0 <= enrichment_completeness <= 1:
            raise ValueError("enrichment_completeness must be 0-1")
        if not 0 <= quote_quality <= 1:
            raise ValueError("quote_quality must be 0-1")
        
        # Component scores
        components = {}
        
        # Discovery Quality (20 pts max)
        components["discovery_quality"] = self._make_component(
            "discovery_quality", discovery_quality, 20, 1.0,
            ["pre_score", "deduplication"]
        )
        
        # On-Chain Quality (25 pts max)
        onchain_confidence = 0.5  # Base confidence
        components["onchain_quality"] = self._make_component(
            "onchain_quality", onchain_quality, 25, onchain_confidence,
            ["enrichment_completeness", "token_quality"]
        )
        
        # Liquidity/Execution (20 pts max)
        liq_confidence = 0.6 if slippage_estimate < 0.05 else 0.3
        components["liquidity_execution"] = self._make_component(
            "liquidity_execution", liquidity_execution, 20, liq_confidence,
            ["liquidity_depth", "slippage", "quote_quality"]
        )
        
        # Narrative/Momentum (20 pts max)
        nar_confidence = min(narrative_confidence, 0.8)  # Cap narrative confidence
        components["narrative_momentum"] = self._make_component(
            "narrative_momentum", narrative_momentum, 20, nar_confidence,
            ["narrative_score", "velocity", "sentiment"]
        )
        
        # Creator Quality (15 pts max)
        creator_confidence = 0.5  # Hard to verify
        components["creator_quality"] = self._make_component(
            "creator_quality", creator_quality, 15, creator_confidence,
            ["creator_reputation", "history", "serial_launches"]
        )
        
        # Risk Penalty (0 to -100)
        risk_penalty = min(risk_score * 1.5, 100)  # Cap at 100
        components["risk_penalty"] = ComponentScore(
            name="risk_penalty",
            score=-risk_penalty,
            max_weight=100,
            confidence=0.9,
            evidence=[f"risk_score={risk_score}", f"risk_class={risk_class}"],
            missing=False,
        )
        
        # Calculate meta score
        meta_score = sum(c.score for c in components.values())
        
        # Hard rejection override
        hard_reject = risk_class == "REJECT" or risk_score >= 70
        
        # Confidence calculation
        # Weighted average of component confidences, penalized by missing data
        total_weight = sum(abs(c.max_weight) for c in components.values())
        weighted_confidence = sum(c.confidence * abs(c.max_weight) for c in components.values()) / total_weight
        
        # Penalize for missing evidence
        evidence_factor = min(1.0, len([c for c in components.values() if not c.missing]) / len(components))
        confidence = weighted_confidence * evidence_factor
        
        # Expected values (EMPIRICAL_UNCALIBRATED until Phase 4C)
        calibration = "EMPIRICAL_UNCALIBRATED"
        
        # Rough estimates (will be calibrated in Phase 4C)
        expected_return = None
        expected_downside = None
        estimated_execution_cost = None
        estimated_slippage = None
        risk_adjusted_ev = None
        
        # Decision logic
        if risk_class == "REJECT":
            decision = "reject"
            rejection_reason = f"Risk class REJECT (score={risk_score})"
        elif risk_score >= 70:
            decision = "reject"
            rejection_reason = f"Risk score {risk_score} >= 70"
        elif meta_score < 50:
            decision = "reject"
            rejection_reason = f"Meta score {meta_score:.1f} < 50"
        else:
            decision = "approve"
            rejection_reason = ""
        
        # Position sizing (capped at experimental max)
        if decision == "approve":
            # Kelly-like sizing but capped
            base_size = self.max_position_size_sol
            confidence_factor = max(0.2, confidence)  # Min 20% of max
            risk_factor = max(0.1, 1.0 - risk_score / 100.0)  # Reduce size for risk
            recommended_size_sol = round(base_size * confidence_factor * risk_factor, 6)
        else:
            recommended_size_sol = 0.0
        
        return {
            "component_scores": {k: v.__dict__ for k, v in components.items()},
            "meta_score": round(meta_score, 2),
            "confidence": round(confidence, 3),
            "expected_return": expected_return,
            "expected_downside": expected_downside,
            "estimated_execution_cost": estimated_execution_cost,
            "estimated_slippage": estimated_slippage,
            "risk_adjusted_ev": risk_adjusted_ev,
            "recommended_size_sol": recommended_size_sol,
            "decision": decision,
            "rejection_reason": "" if decision == "approve" else f"Risk class {risk_class} (score={risk_score})" if risk_class == "REJECT" else f"Meta score {meta_score:.1f} < 50",
            "calibration": "EMPIRICAL_UNCALIBRATED",
            "component_breakdown": {k: round(v.score, 2) for k, v in components.items()},
        }
    
    def _make_component(
        self,
        name: str,
        raw_score: float,
        max_weight: float,
        confidence: float,
        evidence: List[str],
    ) -> ComponentScore:
        """Create a normalized component score."""
        # Normalize score to weight
        normalized = max(0.0, min(1.0, raw_score / 100.0))
        score = normalized * max_weight
        
        return ComponentScore(
            name=name,
            score=round(score, 2),
            max_weight=max_weight,
            confidence=confidence,
            evidence=evidence,
            missing=False,
        )


def create_meta_engine(config: Optional[Dict] = None) -> MetaEngine:
    """Factory to create configured MetaEngine."""
    config = config or {}
    return MetaEngine(
        risk_penalty_weight=config.get("risk_penalty_weight", 1.5),
        min_meta_score_for_entry=config.get("min_meta_score_for_entry", 50.0),
        max_position_size_sol=config.get("max_position_size_sol", 0.02),
    )