#!/usr/bin/env python3
"""
Cost Model - Calculate Helius credit usage for different discovery architectures.

Tests mathematical conversions and models three filtering scenarios.
Uses measured data from 2026-09-10 probes.
"""

from dataclasses import dataclass
from typing import List
import json

@dataclass
class CostConversion:
    """Test case for cost conversion."""
    input_mb: float
    expected_credits: float

@dataclass
class ScenarioResult:
    """Result for a filtering scenario."""
    name: str
    discovered_per_day: int
    enriched_per_day: int
    rpc_calls_per_day: int
    helius_credits_per_day: float
    helius_credits_per_month: float
    pct_free_tier: float
    passes_safety_ceiling: bool

@dataclass
class HybridScenarioResult:
    """Result for hybrid PumpPortal + Helius architecture."""
    name: str
    pump_portal_events_per_min: float
    pump_portal_unique_per_min: float
    candidates_per_day: int
    enrichment_pct: float
    enriched_per_day: int
    rpc_calls_per_day: int
    ws_credits_per_day: float
    rpc_credits_per_day: float
    total_credits_per_day: float
    total_credits_per_month: float
    pct_free_tier: float
    passes_safety_ceiling: bool
    latency_note: str

class CostModel:
    """Cost model for Pump.fun discovery architectures."""
    
    # Helius Free Tier constants (verified 2026-09-10)
    FREE_TIER_CREDITS_PER_MONTH = 1_000_000
    FREE_TIER_CREDITS_PER_DAY = FREE_TIER_CREDITS_PER_MONTH / 30  # ~33,333
    SAFETY_CEILING_PCT = 70  # Target ≤70% of free tier
    
    # Credit costs (verified 2026-09-10)
    CREDITS_PER_01_MB_WS = 2  # Helius WS metering: 2 credits per 0.1 MB
    CREDITS_PER_MB_WS = CREDITS_PER_01_MB_WS * 10  # 20 credits/MB
    CREDITS_PER_RPC_CALL = 1  # Standard RPC
    CREDITS_PER_GET_PROGRAM_ACCOUNTS = 10
    CREDITS_PER_DAS = 10
    CREDITS_PER_ENHANCED = 100
    
    # Measured traffic (from probes - 2026-09-10)
    # PumpPortal subscribeNewToken (measured 10-min sample, no API key)
    PUMP_PORTAL_EVENTS_PER_MINUTE = 28.4
    PUMP_PORTAL_UNIQUE_MINTS_PER_MINUTE = 25.2
    PUMP_PORTAL_DUPLICATE_RATE_PCT = 11.3
    PUMP_PORTAL_AVG_PAYLOAD_BYTES = 560
    
    # Helius WS (NOT MEASURED - no credentials)
    # Using conservative estimate: 2.5 MB/day for Pump.fun logsSubscribe
    HELIUS_WS_MB_PER_DAY = 2.5
    
    # Derived: PumpPortal candidates per day (unique mints)
    PUMP_PORTAL_CANDIDATES_PER_DAY = int(PUMP_PORTAL_UNIQUE_MINTS_PER_MINUTE * 60 * 24)
    
    def __init__(self):
        pass
    
    def mb_to_credits(self, mb: float) -> float:
        """Convert MB to Helius WS credits."""
        # 2 credits / 0.1 MB = 20 credits / MB
        return mb * self.CREDITS_PER_MB_WS
    
    def test_conversions(self) -> List[CostConversion]:
        """Test cost conversion calculations."""
        tests = [
            CostConversion(2.5, 50),      # 2.5 MB → 50 credits
            CostConversion(250, 5000),    # 250 MB → 5,000 credits
            CostConversion(2500, 50000),  # 2,500 MB (2.5 GB) → 50,000 credits
        ]
        results = []
        for test in tests:
            actual = self.mb_to_credits(test.input_mb)
            assert abs(actual - test.expected_credits) < 0.01, f"Conversion failed: {test.input_mb} MB → {actual} credits (expected {test.expected_credits})"
            results.append(test)
        return results
    
    def model_helius_only_scenario(
        self,
        name: str,
        discovered_per_day: int,
        enrichment_pct: float,
        rpc_calls_per_enrichment: int = 5,
        ws_mb_per_day: float | None = None
    ) -> ScenarioResult:
        """Model a Helius-only filtering scenario (for comparison)."""
        ws_mb = ws_mb_per_day or self.HELIUS_WS_MB_PER_DAY
        
        # WebSocket credits
        ws_credits_per_day = self.mb_to_credits(ws_mb)
        
        # Enrichment
        enriched_per_day = int(discovered_per_day * enrichment_pct)
        rpc_calls_per_day = enriched_per_day * rpc_calls_per_enrichment
        rpc_credits_per_day = rpc_calls_per_day * self.CREDITS_PER_RPC_CALL
        
        # Total
        total_credits_per_day = ws_credits_per_day + rpc_credits_per_day
        total_credits_per_month = total_credits_per_day * 30
        pct_free_tier = (total_credits_per_month / self.FREE_TIER_CREDITS_PER_MONTH) * 100
        
        return ScenarioResult(
            name=name,
            discovered_per_day=discovered_per_day,
            enriched_per_day=enriched_per_day,
            rpc_calls_per_day=rpc_calls_per_day,
            helius_credits_per_day=total_credits_per_day,
            helius_credits_per_month=total_credits_per_month,
            pct_free_tier=pct_free_tier,
            passes_safety_ceiling=pct_free_tier <= self.SAFETY_CEILING_PCT
        )
    
    def model_hybrid_scenario(
        self,
        name: str,
        enrichment_pct: float,
        rpc_calls_per_enrichment: int = 5
    ) -> HybridScenarioResult:
        """Model PumpPortal + Helius hybrid scenario."""
        # PumpPortal provides discovery (FREE)
        candidates_per_day = self.PUMP_PORTAL_CANDIDATES_PER_DAY
        
        # Enrichment subset
        enriched_per_day = int(candidates_per_day * enrichment_pct)
        rpc_calls_per_day = enriched_per_day * rpc_calls_per_enrichment
        rpc_credits_per_day = rpc_calls_per_day * self.CREDITS_PER_RPC_CALL
        
        # NO Helius WebSocket needed - PumpPortal handles discovery
        ws_credits_per_day = 0
        
        total_credits_per_day = ws_credits_per_day + rpc_credits_per_day
        total_credits_per_month = total_credits_per_day * 30
        pct_free_tier = (total_credits_per_month / self.FREE_TIER_CREDITS_PER_MONTH) * 100
        
        return HybridScenarioResult(
            name=name,
            pump_portal_events_per_min=self.PUMP_PORTAL_EVENTS_PER_MINUTE,
            pump_portal_unique_per_min=self.PUMP_PORTAL_UNIQUE_MINTS_PER_MINUTE,
            candidates_per_day=candidates_per_day,
            enrichment_pct=enrichment_pct,
            enriched_per_day=enriched_per_day,
            rpc_calls_per_day=rpc_calls_per_day,
            ws_credits_per_day=ws_credits_per_day,
            rpc_credits_per_day=rpc_credits_per_day,
            total_credits_per_day=total_credits_per_day,
            total_credits_per_month=total_credits_per_month,
            pct_free_tier=pct_free_tier,
            passes_safety_ceiling=pct_free_tier <= self.SAFETY_CEILING_PCT,
            latency_note="PumpPortal WS ~sub-second; Helius RPC ~100-500ms per call"
        )
    
    def model_all_helius_only(self, discovered_per_day: int = 2000) -> List[ScenarioResult]:
        """Model all three Helius-only scenarios (for comparison)."""
        scenarios = [
            ("A - Conservative (1-5% enrichment)", 0.03),
            ("B - Moderate (10-20% enrichment)", 0.15),
            ("C - Aggressive (most enriched)", 0.70),
        ]
        
        results = []
        for name, enrichment_pct in scenarios:
            result = self.model_helius_only_scenario(name, discovered_per_day, enrichment_pct)
            results.append(result)
        return results
    
    def model_all_hybrid(self) -> List[HybridScenarioResult]:
        """Model all three hybrid scenarios using measured PumpPortal data."""
        scenarios = [
            ("A - Conservative (3% enrichment)", 0.03),
            ("B - Moderate (15% enrichment)", 0.15),
            ("C - Aggressive (70% enrichment)", 0.70),
        ]
        
        results = []
        for name, enrichment_pct in scenarios:
            result = self.model_hybrid_scenario(name, enrichment_pct)
            results.append(result)
        return results

def run_cost_model_tests():
    """Run cost model tests and return results."""
    model = CostModel()
    
    # Test conversions
    print("=== COST CONVERSION TESTS (mathematical audit) ===")
    print("Helius: 2 credits / 0.1 MB = 20 credits / MB")
    print()
    conversions = model.test_conversions()
    for c in conversions:
        actual = model.mb_to_credits(c.input_mb)
        print(f"  {c.input_mb} MB × 20 credits/MB = {actual} credits (expected {c.expected_credits}) ✅")
    
    print("\n=== HELIUS-ONLY ARCHITECTURE (for comparison) ===")
    helius_results = model.model_all_helius_only(discovered_per_day=2000)
    for r in helius_results:
        print(f"\n  {r.name}:")
        print(f"    Discovered/day: {r.discovered_per_day}")
        print(f"    Enriched/day: {r.enriched_per_day}")
        print(f"    RPC calls/day: {r.rpc_calls_per_day}")
        print(f"    WS credits/day: {model.mb_to_credits(model.HELIUS_WS_MB_PER_DAY):.0f}")
        print(f"    RPC credits/day: {r.rpc_calls_per_day * model.CREDITS_PER_RPC_CALL:.0f}")
        print(f"    Total credits/day: {r.helius_credits_per_day:.0f}")
        print(f"    Total credits/month: {r.helius_credits_per_month:.0f}")
        print(f"    % Free tier: {r.pct_free_tier:.1f}%")
        print(f"    Passes 70% ceiling: {'✅ YES' if r.passes_safety_ceiling else '❌ NO'}")
    
    print("\n=== HYBRID ARCHITECTURE (PumpPortal Discovery + Helius Enrichment) ===")
    print(f"PumpPortal measured: {model.PUMP_PORTAL_UNIQUE_MINTS_PER_MINUTE:.1f} unique mints/min")
    print(f"Candidates/day: {model.PUMP_PORTAL_CANDIDATES_PER_DAY:,}")
    print(f"PumpPortal WS: FREE (no Helius credits)")
    print()
    hybrid_results = model.model_all_hybrid()
    for r in hybrid_results:
        print(f"\n  {r.name}:")
        print(f"    PumpPortal events/min: {r.pump_portal_events_per_min:.1f}")
        print(f"    PumpPortal unique/min: {r.pump_portal_unique_per_min:.1f}")
        print(f"    Candidates/day: {r.candidates_per_day:,}")
        print(f"    Enrichment %: {r.enrichment_pct*100:.0f}%")
        print(f"    Enriched/day: {r.enriched_per_day:,}")
        print(f"    RPC calls/day: {r.rpc_calls_per_day:,}")
        print(f"    WS credits/day: {r.ws_credits_per_day:.0f} (PumpPortal = free)")
        print(f"    RPC credits/day: {r.rpc_credits_per_day:.0f}")
        print(f"    Total credits/day: {r.total_credits_per_day:.0f}")
        print(f"    Total credits/month: {r.total_credits_per_month:.0f}")
        print(f"    % Free tier: {r.pct_free_tier:.1f}%")
        print(f"    Passes 70% ceiling: {'✅ YES' if r.passes_safety_ceiling else '❌ NO'}")
        print(f"    Latency: {r.latency_note}")
    
    # Return all results for reporting
    return {
        "conversions": conversions,
        "helius_only": helius_results,
        "hybrid": hybrid_results
    }

if __name__ == "__main__":
    run_cost_model_tests()