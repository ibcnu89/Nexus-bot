"""
Rug / Risk Detector - Deterministic risk assessment for Solana tokens.

Implements comprehensive risk checks based on on-chain data and discovery metadata.
All thresholds are configurable and documented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum

logger = logging.getLogger(__name__)


class RiskClass(str, Enum):
    """Risk classification levels."""
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    REJECT = "REJECT"


@dataclass
class RiskThresholds:
    """Configurable risk thresholds."""
    # Authority risks
    freeze_authority_active: int = 50
    
    # Concentration risks
    creator_holdings_pct_high: float = 50.0
    creator_holdings_pct_extreme: float = 75.0
    top_1_holder_pct_high: float = 30.0
    top_1_holder_pct_extreme: float = 50.0
    top_5_holders_pct_high: float = 50.0
    top_5_holders_pct_extreme: float = 75.0
    top_10_holders_pct_high: float = 65.0
    top_10_holders_pct_extreme: float = 80.0
    
    # Liquidity risks
    min_liquidity_sol: float = 1000.0
    liquidity_drop_pct_high: float = 30.0
    liquidity_drop_pct_extreme: float = 50.0
    
    # Creator behavior
    creator_rapid_launches_high: int = 3
    creator_rapid_launches_extreme: int = 5
    creator_serial_launches_window_hours: int = 24
    
    # Metadata risks
    decimals_unresolved: int = 30
    metadata_incomplete: int = 15
    metadata_suspicious: int = 25
    
    # Quote risks
    no_valid_sell_quote: int = 50
    sell_quote_price_impact_high: float = 10.0
    sell_quote_price_impact_extreme: float = 25.0
    
    # Class boundaries
    reject_threshold: int = 70
    high_threshold: int = 40
    moderate_threshold: int = 20


@dataclass
class RiskAssessment:
    """Complete risk assessment result."""
    risk_score: int = 0
    risk_class: str = "UNKNOWN"
    risk_reasons: List[str] = field(default_factory=list)
    component_scores: Dict[str, int] = field(default_factory=dict)
    missing_data_indicators: List[str] = field(default_factory=list)
    confidence: float = 1.0


class RugDetector:
    """
    Deterministic rug/risk detector for Solana tokens.
    
    Evaluates on-chain and discovery data to produce a risk score (0-100)
    and classification. All thresholds are explicit and documented.
    """
    
    def __init__(
        self,
        thresholds: Optional["RiskThresholds"] = None,
        require_sell_quote: bool = True,
    ):
        self.thresholds = thresholds or RiskThresholds()
        self.require_sell_quote = require_sell_quote
        
    def assess(self, enrichment_data: Dict[str, Any], discovery_event: Optional[Dict] = None) -> "RiskAssessment":
        """
        Perform comprehensive risk assessment.
        
        Args:
            enrichment_data: On-chain enrichment data from Helius
            discovery_event: Original discovery event metadata
            
        Returns:
            RiskAssessment with score, class, reasons, and component scores
        """
        assessment = RiskAssessment()
        
        # Check data completeness
        self._check_data_completeness(enrichment_data, assessment)
        
        # Authority risks
        self._check_authorities(enrichment_data, assessment)
        
        # Concentration risks
        self._check_concentration(enrichment_data, assessment)
        
        # Liquidity risks
        self._check_liquidity(enrichment_data, assessment)
        
        # Creator behavior risks
        self._check_creator_behavior(enrichment_data, assessment)
        
        # Metadata risks
        self._check_metadata(enrichment_data, assessment)
        
        # Quote/execution risks
        self._check_quotes(enrichment_data, assessment)
        
        # Calculate final score and class
        self._finalize_assessment(assessment)
        
        return assessment
    
    def _check_data_completeness(
        self,
        enrichment: Dict[str, Any],
        assessment: "RiskAssessment"
    ) -> None:
        """Check for missing critical data."""
        
        # Check token info
        if not enrichment.get("token_info"):
            assessment.missing_data_indicators.append("token_info_missing")
        
        # Check decimals
        token_info = enrichment.get("token_info", {})
        decimals = token_info.get("decimals")
        if decimals is None:
            assessment.missing_data_indicators.append("decimals_unresolved")
            assessment.component_scores["decimals_unresolved"] = self.thresholds.decimals_unresolved
            assessment.risk_reasons.append("decimals_unresolved")
        
        # Check sell quote availability
        if self.require_sell_quote:
            sell_quote = enrichment.get("sell_quote")
            if not sell_quote:
                assessment.missing_data_indicators.append("no_sell_quote")
                assessment.component_scores["no_valid_sell_quote"] = self.thresholds.no_valid_sell_quote
                assessment.risk_reasons.append("no_valid_sell_quote")
    
    def _check_authorities(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check mint and freeze authority status."""
        token_info = enrichment.get("token_info", {})
        
        # Freeze authority
        freeze_auth = token_info.get("freeze_authority")
        if freeze_auth and freeze_auth != "None" and freeze_auth != "":
            assessment.component_scores["freeze_authority_active"] = self.thresholds.freeze_authority_active
            assessment.risk_reasons.append("freeze_authority_active")
        
        # Mint authority (less critical but still notable)
        mint_auth = token_info.get("mint_authority")
        if mint_auth and mint_auth != "None" and mint_auth != "":
            assessment.component_scores["mint_authority_active"] = 15
            assessment.risk_reasons.append("mint_authority_active")
    
    def _check_concentration(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check holder and creator concentration."""
        # Holder concentration from holder_analysis
        holder_analysis = enrichment.get("holder_analysis", {})
        
        top_1 = holder_analysis.get("top_1_holder_pct", 0)
        top_5 = holder_analysis.get("top_5_holders_pct", 0)
        top_10 = holder_analysis.get("top_10_holders_pct", 0)
        
        # Top 1 holder
        if top_1 >= self.thresholds.top_1_holder_pct_extreme:
            assessment.component_scores["top_1_holder_extreme"] = 30
            assessment.risk_reasons.append(f"top_1_holder_{top_1:.1f}pct_extreme")
        elif top_1 >= self.thresholds.top_1_holder_pct_high:
            assessment.component_scores["top_1_holder_high"] = 15
            assessment.risk_reasons.append(f"top_1_holder_{top_1:.1f}pct_high")
        
        # Top 5 holders
        if top_5 >= self.thresholds.top_5_holders_pct_extreme:
            assessment.component_scores["top_5_holders_extreme"] = 25
            assessment.risk_reasons.append(f"top_5_holders_{top_5:.1f}pct_extreme")
        elif top_5 >= self.thresholds.top_5_holders_pct_high:
            assessment.component_scores["top_5_holders_high"] = 12
            assessment.risk_reasons.append(f"top_5_holders_{top_5:.1f}pct_high")
        
        # Top 10 holders
        if top_10 >= self.thresholds.top_10_holders_pct_extreme:
            assessment.component_scores["top_10_holders_extreme"] = 30
            assessment.risk_reasons.append(f"top_10_holders_{top_10:.1f}pct_extreme")
        elif top_10 >= self.thresholds.top_10_holders_pct_high:
            assessment.component_scores["top_10_holders_high"] = 15
            assessment.risk_reasons.append(f"top_10_holders_{top_10:.1f}pct_high")
        
        # Creator holdings (if available)
        creator_holdings = enrichment.get("creator_holdings_pct")
        if creator_holdings is not None:
            if creator_holdings >= self.thresholds.creator_holdings_pct_extreme:
                assessment.component_scores["creator_holdings_extreme"] = 40
                assessment.risk_reasons.append(f"creator_holds_{creator_holdings:.1f}pct_extreme")
            elif creator_holdings >= self.thresholds.creator_holdings_pct_high:
                assessment.component_scores["creator_holdings_high"] = 20
                assessment.risk_reasons.append(f"creator_holds_{creator_holdings:.1f}pct_high")
    
    def _check_liquidity(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check liquidity depth and stability."""
        # Initial liquidity
        initial_liq = enrichment.get("initial_liquidity_sol")
        current_liq = enrichment.get("current_liquidity_sol")
        
        if current_liq is not None:
            if current_liq < self.thresholds.min_liquidity_sol:
                assessment.component_scores["low_liquidity"] = 35
                assessment.risk_reasons.append(f"liquidity_below_min_{current_liq:.0f}sol")
            
            # Check liquidity drop
            initial_liq = enrichment.get("initial_liquidity_sol")
            if initial_liq is not None and initial_liq > 0:
                drop_pct = 100 * (1 - current_liq / initial_liq)
                if drop_pct >= self.thresholds.liquidity_drop_pct_extreme:
                    assessment.component_scores["liquidity_collapse"] = 40
                    assessment.risk_reasons.append(f"liquidity_drop_{drop_pct:.1f}pct_extreme")
                elif drop_pct >= self.thresholds.liquidity_drop_pct_high:
                    assessment.component_scores["liquidity_drop"] = 20
                    assessment.risk_reasons.append(f"liquidity_drop_{drop_pct:.1f}pct_high")
    
    def _check_creator_behavior(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check for suspicious creator patterns."""
        rapid_launches = enrichment.get("creator_rapid_launches", 0)
        if rapid_launches >= self.thresholds.creator_rapid_launches_extreme:
            assessment.component_scores["creator_rapid_launches_extreme"] = 30
            assessment.risk_reasons.append(f"creator_rapid_launches_{rapid_launches}_extreme")
        elif rapid_launches >= self.thresholds.creator_rapid_launches_high:
            assessment.component_scores["creator_rapid_launches_high"] = 15
            assessment.risk_reasons.append(f"creator_rapid_launches_{rapid_launches}_high")
        
        # Check for serial launches in discovery event
        # (would need discovery event data)
    
    def _check_metadata(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check for metadata anomalies."""
        # Check if decimals were resolved
        token_info = enrichment.get("token_info", {})
        if enrichment.get("decimals_unresolved") or enrichment.get("token_info", {}).get("decimals") is None:
            assessment.component_scores["decimals_unresolved"] = self.thresholds.decimals_unresolved
            assessment.risk_reasons.append("decimals_unresolved")
        
        # Check metadata completeness from discovery
        # Note: discovery event is not passed to this method, would need to be passed in
        # This is a placeholder - discovery event data would be passed separately
        pass
    
    def _check_quotes(self, enrichment: Dict[str, Any], assessment: "RiskAssessment") -> None:
        """Check quote availability and quality."""
        sell_quote = enrichment.get("sell_quote")
        if not sell_quote:
            # Already handled in data completeness
            return
        
        # Check price impact on sell
        price_impact = sell_quote.get("price_impact_pct", 0)
        if price_impact >= self.thresholds.sell_quote_price_impact_extreme:
            assessment.component_scores["sell_price_impact_extreme"] = 30
            assessment.risk_reasons.append(f"sell_price_impact_{price_impact:.1f}pct_extreme")
        elif price_impact >= self.thresholds.sell_quote_price_impact_high:
            assessment.component_scores["sell_price_impact_high"] = 15
            assessment.risk_reasons.append(f"sell_price_impact_{price_impact:.1f}pct_high")
        
        # Check if quote is executable (has valid out_amount)
        if not sell_quote.get("out_amount") or sell_quote.get("out_amount", 0) <= 0:
            assessment.component_scores["no_valid_sell_quote"] = self.thresholds.no_valid_sell_quote
            assessment.risk_reasons.append("no_valid_sell_quote")
    
    def _finalize_assessment(self, assessment: "RiskAssessment") -> None:
        """Calculate final score and determine class."""
        # Sum component scores
        assessment.risk_score = min(sum(assessment.component_scores.values()), 100)
        
        # Determine class
        if assessment.risk_score >= self.thresholds.reject_threshold:
            assessment.risk_class = "REJECT"
        elif assessment.risk_score >= self.thresholds.high_threshold:
            assessment.risk_class = "HIGH"
        elif assessment.risk_score >= self.thresholds.moderate_threshold:
            assessment.risk_class = "MODERATE"
        else:
            assessment.risk_class = "LOW"
        
        # Adjust confidence based on missing data
        missing_count = len(assessment.missing_data_indicators)
        if missing_count > 0:
            # Reduce confidence based on missing data
            assessment.confidence = max(0.3, 1.0 - (missing_count * 0.15))
        
        # Hard rejects override everything
        hard_reject_reasons = [
            "freeze_authority_active",
            "decimals_unresolved",
            "no_valid_sell_quote",
        ]
        if any(reason in assessment.risk_reasons for reason in ["freeze_authority_active", "decimals_unresolved", "no_valid_sell_quote"]):
            assessment.risk_class = "REJECT"
            assessment.risk_score = max(assessment.risk_score, 70)


def create_rug_detector(config: Optional[Dict] = None):
    """Factory function to create configured RugDetector."""
    from .rug_detector import RugDetector, RiskThresholds
    thresholds = RiskThresholds()
    if config:
        for key, value in config.items():
            if hasattr(RiskThresholds, key):
                setattr(thresholds, key, value)
    return RugDetector(thresholds=thresholds)


# Hard-reject constants for external use
HARD_REJECT_REASONS = frozenset([
    "freeze_authority_active",
    "decimals_unresolved",
    "no_valid_sell_quote",
])

REJECT_THRESHOLD = 70
HIGH_THRESHOLD = 40
MODERATE_THRESHOLD = 20