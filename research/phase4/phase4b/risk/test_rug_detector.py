"""
Tests for Phase 4B Risk Engine (RugDetector).
"""

import pytest
from research.phase4.phase4b.risk.rug_detector import (
    RugDetector, RiskAssessment, RiskThresholds, RiskClass,
    HARD_REJECT_REASONS, REJECT_THRESHOLD, HIGH_THRESHOLD, MODERATE_THRESHOLD
)


class TestRiskThresholds:
    """Test risk threshold configuration."""

    def test_default_thresholds(self):
        thresholds = RiskThresholds()
        assert thresholds.freeze_authority_active == 50
        assert thresholds.creator_holdings_pct_high == 50.0
        assert thresholds.creator_holdings_pct_extreme == 75.0
        assert thresholds.top_1_holder_pct_high == 30.0
        assert thresholds.top_1_holder_pct_extreme == 50.0
        assert thresholds.top_10_holders_pct_high == 65.0
        assert thresholds.top_10_holders_pct_extreme == 80.0
        assert thresholds.min_liquidity_sol == 1000.0
        assert thresholds.decimals_unresolved == 30
        assert thresholds.no_valid_sell_quote == 50
        assert thresholds.reject_threshold == 70
        assert thresholds.high_threshold == 40
        assert thresholds.moderate_threshold == 20

    def test_custom_thresholds(self):
        thresholds = RiskThresholds(
            reject_threshold=80,
            high_threshold=50,
            moderate_threshold=30,
        )
        assert thresholds.reject_threshold == 80
        assert thresholds.high_threshold == 50
        assert thresholds.moderate_threshold == 30


class TestHealthyToken:
    """Test healthy token with no risk factors."""

    def test_healthy_token_passes(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class == "LOW"
        assert assessment.risk_score < 20
        assert "freeze_authority_active" not in assessment.risk_reasons
        assert "decimals_unresolved" not in assessment.risk_reasons
        assert "no_valid_sell_quote" not in assessment.risk_reasons


class TestHardRejects:
    """Test hard reject conditions."""

    def test_freeze_authority_active_rejects(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": "SomeAuthority123",
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class == "REJECT"
        assert assessment.risk_score >= 70
        assert "freeze_authority_active" in assessment.risk_reasons

    def test_decimals_unresolved_rejects(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "freeze_authority": None,
                "mint_authority": None,
                # No decimals field
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class == "REJECT"
        assert assessment.risk_score >= 70
        assert "decimals_unresolved" in assessment.risk_reasons

    def test_no_valid_sell_quote_rejects(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            # No sell_quote
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class == "REJECT"
        assert assessment.risk_score >= 70
        assert "no_valid_sell_quote" in assessment.risk_reasons


class TestConcentrationRisks:
    """Test concentration risk detection."""

    def test_top_1_holder_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 60.0,  # > 50% extreme
                "top_5_holders_pct": 70.0,
                "top_10_holders_pct": 85.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "top_1_holder_60.0pct_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("top_1_holder_extreme", 0) == 30

    def test_top_10_holders_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 10.0,
                "top_5_holders_pct": 40.0,
                "top_10_holders_pct": 90.0,  # > 80% extreme
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "top_10_holders_90.0pct_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("top_10_holders_extreme", 0) == 30

    def test_creator_holdings_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 10.0,
                "top_5_holders_pct": 30.0,
                "top_10_holders_pct": 50.0,
            },
            "creator_holdings_pct": 80.0,  # > 75% extreme
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "creator_holds_80.0pct_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("creator_holdings_extreme", 0) == 40


class TestLiquidityRisks:
    """Test liquidity risk detection."""

    def test_low_liquidity(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 500.0,  # < 1000 min
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "liquidity_below_min_500sol" in assessment.risk_reasons
        assert assessment.component_scores.get("low_liquidity", 0) == 35

    def test_liquidity_drop_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 2000.0,
            "initial_liquidity_sol": 10000.0,  # 80% drop
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "liquidity_drop_80.0pct_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("liquidity_collapse", 0) == 40


class TestCreatorBehavior:
    """Test creator behavior risk detection."""

    def test_rapid_launches_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 6,  # > 5 extreme
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "creator_rapid_launches_6_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("creator_rapid_launches_extreme", 0) == 30


class TestSellQuoteRisks:
    """Test sell quote risk detection."""

    def test_sell_price_impact_extreme(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 30.0,  # > 25% extreme
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "sell_price_impact_30.0pct_extreme" in assessment.risk_reasons
        assert assessment.component_scores.get("sell_price_impact_extreme", 0) == 30

    def test_no_valid_sell_quote_zero_out_amount(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 0,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "no_valid_sell_quote" in assessment.risk_reasons
        assert assessment.risk_class == "REJECT"


class TestMissingData:
    """Test missing data handling."""

    def test_missing_data_reduces_confidence(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {
                "decimals": 9,
                "freeze_authority": None,
                "mint_authority": None,
            },
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
                "top_5_holders_pct": 20.0,
                "top_10_holders_pct": 35.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        # With no missing data, confidence should be high
        assert assessment.confidence >= 0.9

    def test_multiple_missing_data(self):
        detector = RugDetector()
        enrichment = {
            # Missing token_info entirely
            "holder_analysis": {
                "top_1_holder_pct": 5.0,
            },
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {
                "out_amount": 1000000,
                "price_impact_pct": 1.0,
            },
        }
        assessment = detector.assess(enrichment, {})
        assert "token_info_missing" in assessment.missing_data_indicators
        assert assessment.confidence < 1.0


class TestClassBoundaries:
    """Test risk class boundary transitions."""

    def test_low_boundary(self):
        detector = RugDetector()
        # Score 0-19 -> LOW
        enrichment = {
            "token_info": {"decimals": 9, "freeze_authority": None},
            "holder_analysis": {"top_1_holder_pct": 5.0, "top_5_holders_pct": 20.0, "top_10_holders_pct": 35.0},
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {"out_amount": 1000000, "price_impact_pct": 1.0},
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class == "LOW"
        assert assessment.risk_score < 20

    def test_moderate_boundary(self):
        detector = RugDetector()
        # Need to trigger some risk to get MODERATE (20-39)
        enrichment = {
            "token_info": {"decimals": 9, "freeze_authority": None},
            "holder_analysis": {"top_1_holder_pct": 40.0, "top_5_holders_pct": 55.0, "top_10_holders_pct": 70.0},
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 0,
            "sell_quote": {"out_amount": 1000000, "price_impact_pct": 1.0},
        }
        assessment = detector.assess(enrichment, {})
        assert assessment.risk_class in ["MODERATE", "HIGH", "REJECT"]
        assert assessment.risk_score >= 20


class TestDeterministicOutput:
    """Test that assessment is deterministic for same input."""

    def test_deterministic(self):
        detector = RugDetector()
        enrichment = {
            "token_info": {"decimals": 9, "freeze_authority": None},
            "holder_analysis": {"top_1_holder_pct": 10.0, "top_5_holders_pct": 30.0, "top_10_holders_pct": 50.0},
            "current_liquidity_sol": 5000.0,
            "initial_liquidity_sol": 5000.0,
            "creator_rapid_launches": 2,
            "sell_quote": {"out_amount": 1000000, "price_impact_pct": 2.0},
        }
        a1 = detector.assess(enrichment, {})
        a2 = detector.assess(enrichment, {})
        assert a1.risk_score == a2.risk_score
        assert a1.risk_class == a2.risk_class
        assert a1.risk_reasons == a2.risk_reasons


class TestConstants:
    """Test exported constants."""

    def test_hard_reject_reasons(self):
        assert "freeze_authority_active" in HARD_REJECT_REASONS
        assert "decimals_unresolved" in HARD_REJECT_REASONS
        assert "no_valid_sell_quote" in HARD_REJECT_REASONS

    def test_threshold_constants(self):
        assert REJECT_THRESHOLD == 70
        assert HIGH_THRESHOLD == 40
        assert MODERATE_THRESHOLD == 20


if __name__ == "__main__":
    pytest.main([__file__, "-v"])