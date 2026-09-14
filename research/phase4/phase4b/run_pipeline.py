#!/usr/bin/env python3
"""
Phase 4B Pipeline Orchestrator - Main pipeline runner for Phase 4B soak test.

Integrates all Phase 4B components into a complete experimental pipeline:
Discovery -> Pre-score -> Enrichment -> Risk -> Narrative -> Meta -> Paper Execution
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from research.phase4.phase4b.discovery.models import (
    DiscoveryEvent, Candidate, DiscoverySource, DiscoveryQueue
)
from research.phase4.phase4b.discovery.pumpportal import (
    PumpPortalDiscovery, SolanaPublicRPCDiscovery, DiscoveryManager
)
from research.phase4.phase4b.scoring.pre_score import PreScorer
from research.phase4.phase4b.scoring.meta_engine import MetaEngine
from research.phase4.phase4b.risk.rug_detector import RugDetector
from research.phase4.phase4b.narrative.narrative_engine import NarrativeEngine
from research.phase4.phase4b.enrichment.helius import (
    HeliusClient, CreditGovernor, TTLCache, EnrichmentPipeline
)
from research.phase4.phase4b.execution.execution_accounting import (
    ExecutionQuote, ExitSettlement, PositionLedger, Side
)
from research.phase4.phase4b.execution.paper_adapter import (
    PaperExecutionAdapter, EntryIntent, ExitIntent, FillResult
)
from research.phase4.phase4b.storage.database import SCHEMA_SQL

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """Configuration for the Phase 4B pipeline."""
    # Discovery
    pumpportal_api_key: Optional[str] = None
    enable_fallback: bool = True

    # Pre-scoring
    pre_score_threshold: float = 0.0
    target_enrichment_pct: float = 0.03
    max_candidates_per_day: int = 1500

    # Enrichment
    helius_api_key: Optional[str] = None
    max_parallel_enrichment: int = 3
    helius_monthly_limit: int = 1_000_000
    safety_ceiling_pct: float = 70.0

    # Execution
    position_size_sol: float = 0.02
    max_open_positions: int = 10
    max_new_positions_per_hour: int = 20
    max_slippage_bps: int = 50
    max_priority_fee_sol: float = 0.0005

    # Database
    db_path: str = "research/phase4/phase4b/results/phase4b_experiment.db"

    # Runtime
    max_runtime_seconds: int = 7200  # 2 hours default
    log_level: str = "INFO"


@dataclass
class PipelineStats:
    """Runtime statistics for the pipeline."""
    start_time: float = 0.0

    # Discovery
    raw_events: int = 0
    unique_mints: int = 0
    duplicate_count: int = 0
    discovery_events_persisted: int = 0
    
    # Pre-scoring
    candidates_pre_scored: int = 0
    candidates_passed: int = 0
    candidates_failed: int = 0
    enrichment_quota: int = 0

    # Enrichment
    candidates_enriched: int = 0
    enrichment_failures: int = 0
    enrichment_attempts: int = 0
    helius_credits_used: int = 0

    # Risk
    risk_rejected: int = 0
    risk_low: int = 0
    risk_moderate: int = 0
    risk_high: int = 0

    # Meta
    meta_approved: int = 0
    meta_rejected: int = 0

    # Execution
    paper_entries: int = 0
    paper_exits: int = 0
    partial_exits: int = 0
    final_exits: int = 0

    # Outcome sampling
    outcomes_scheduled: int = 0
    outcomes_completed: int = 0
    snapshots_due: int = 0
    snapshots_inserted: int = 0
    duplicate_snapshots_skipped: int = 0
    missing_price_observations: int = 0
    observation_errors: int = 0

    # Errors
    provider_failures: int = 0
    rpc_failures: int = 0
    database_errors: int = 0
    unhandled_exceptions: int = 0

    # Provider health
    pumpportal_reconnects: int = 0
    pumpportal_disconnects: int = 0
    helius_governor_state: str = "UNKNOWN"
    helius_requests: int = 0
    helius_successful_requests: int = 0
    helius_http_429: int = 0
    helius_timeouts: int = 0
    helius_retries: int = 0
    helius_governor_skips: int = 0
    helius_average_latency_ms: Optional[float] = None
    helius_p95_latency_ms: Optional[float] = None
    helius_average_requests_per_second: float = 0.0
    helius_peak_requests_per_second: int = 0

    def to_dict(self) -> Dict[str, Any]:
        elapsed = time.time() - self.start_time
        return {
            "runtime_seconds": round(elapsed, 1),
            "discovery": {
                "raw_events": self.raw_events,
                "unique_mints": self.unique_mints,
                "duplicate_rate": round(self.duplicate_count / max(self.raw_events, 1), 4),
                "events_persisted": self.discovery_events_persisted,
            },
            "pre_scoring": {
                "candidates_scored": self.candidates_pre_scored,
                "passed": self.candidates_passed,
                "failed": self.candidates_failed,
                "enrichment_quota": self.enrichment_quota,
                "pass_rate": round(self.candidates_passed / max(self.candidates_pre_scored, 1), 4),
            },
            "enrichment": {
                "enriched": self.candidates_enriched,
                "failures": self.enrichment_failures,
                "attempts": self.enrichment_attempts,
                "helius_credits_used": self.helius_credits_used,
            },
            "risk": {
                "rejected": self.risk_rejected,
                "low": self.risk_low,
                "moderate": self.risk_moderate,
                "high": self.risk_high,
            },
            "meta": {
                "approved": self.meta_approved,
                "rejected": self.meta_rejected,
            },
            "execution": {
                "entries": self.paper_entries,
                "exits": self.paper_exits,
                "partial_exits": self.partial_exits,
                "final_exits": self.final_exits,
            },
            "outcomes": {
                "scheduled": self.outcomes_scheduled,
                "completed": self.outcomes_completed,
                "snapshots_due": self.snapshots_due,
                "snapshots_inserted": self.snapshots_inserted,
                "duplicate_snapshots_skipped": self.duplicate_snapshots_skipped,
                "missing_price_observations": self.missing_price_observations,
                "observation_errors": self.observation_errors,
            },
            "errors": {
                "provider_failures": self.provider_failures,
                "rpc_failures": self.rpc_failures,
                "database_errors": self.database_errors,
                "unhandled_exceptions": self.unhandled_exceptions,
            },
            "provider_health": {
                "pumpportal_reconnects": self.pumpportal_reconnects,
                "pumpportal_disconnects": self.pumpportal_disconnects,
                "helius_governor_state": self.helius_governor_state,
                "helius_requests": self.helius_requests,
                "helius_successful_requests": self.helius_successful_requests,
                "helius_http_429": self.helius_http_429,
                "helius_timeouts": self.helius_timeouts,
                "helius_retries": self.helius_retries,
                "helius_governor_skips": self.helius_governor_skips,
                "helius_average_latency_ms": self.helius_average_latency_ms,
                "helius_p95_latency_ms": self.helius_p95_latency_ms,
                "helius_average_requests_per_second": self.helius_average_requests_per_second,
                "helius_peak_requests_per_second": self.helius_peak_requests_per_second,
            }
        }


class Phase4BPipeline:
    """
    Main Phase 4B pipeline orchestrator.

    Coordinates all components: discovery, pre-scoring, enrichment,
    risk analysis, narrative analysis, meta scoring, and paper execution.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.stats = PipelineStats()
        self._running = False
        self._start_time = 0.0

        # Setup logging
        logging.basicConfig(
            level=getattr(logging, config.log_level),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Core components (initialized in start())
        self.discovery_queue: Optional[DiscoveryQueue] = None
        self.discovery_manager: Optional[DiscoveryManager] = None
        self.pre_scorer: Optional[PreScorer] = None
        self.helius_client: Optional[HeliusClient] = None
        self.governor: Optional[CreditGovernor] = None
        self.cache: Optional[TTLCache] = None
        self.enrichment_pipeline: Optional[EnrichmentPipeline] = None
        self.paper_adapter: Optional[PaperExecutionAdapter] = None
        self.db_path: Path = Path(config.db_path)

        # State
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self._running = False

    async def initialize(self) -> None:
        """Initialize all pipeline components."""
        logger.info("Initializing Phase 4B pipeline...")

        # Set start time for both pipeline and stats
        self._start_time = time.time()
        self.stats.start_time = time.time()

        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(SCHEMA_SQL)
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")

        # Discovery queue
        self.discovery_queue = DiscoveryQueue(max_size=50000)

        # Pre-scorer
        self.pre_scorer = PreScorer(
            target_enrichment_pct=self.config.target_enrichment_pct,
            min_score_threshold=self.config.pre_score_threshold,
            max_candidates_per_day=self.config.max_candidates_per_day,
        )

        # Helius enrichment
        self.cache = TTLCache(max_size=20000)
        self.governor = CreditGovernor(
            monthly_credit_limit=self.config.helius_monthly_limit,
            safety_ceiling_pct=self.config.safety_ceiling_pct,
        )
        self.helius_client = HeliusClient(
            api_key=self.config.helius_api_key,
            cache=self.cache,
            governor=self.governor,
        )
        await self.helius_client.__aenter__()

        self.enrichment_pipeline = EnrichmentPipeline(
            helius_client=self.helius_client,
            governor=self.governor,
            cache=self.cache,
            max_parallel=self.config.max_parallel_enrichment,
        )

        # Paper execution adapter
        from research.phase4.prototype.paper_position_manager import ExitConfig
        from research.phase4.prototype.price_source import PriceSource
        exit_config = ExitConfig()
        # Create a simple adapter to use HeliusClient as PriceSource
        class HeliusPriceSource(PriceSource):
            def __init__(self, helius_client):
                self.helius = helius_client

            async def get_buy_quote(self, mint: str, sol_amount: float, slippage_bps: int = 50):
                return await self.helius.get_buy_quote(mint, sol_amount, slippage_bps)

            async def get_sell_quote(self, mint: str, token_amount_human: float, slippage_bps: int = 50):
                return await self.helius.get_sell_quote(mint, token_amount_human, slippage_bps)

            async def get_price(self, mint: str, side: str = "sell", size_sol: float = 0.1, is_graduated: bool = False):
                return await self.helius.get_price(mint, side, size_sol, is_graduated)

            async def get_multiple_prices(self, mints: list[str], vs_mint: str = "So11111111111111111111111111111111111111112"):
                return await self.helius.get_multiple_prices(mints, vs_mint)

        helius_price_source = HeliusPriceSource(self.helius_client)
        exit_config = ExitConfig()
        self.paper_adapter = PaperExecutionAdapter(
            price_source=helius_price_source,
            default_exit_config=exit_config,
            max_open_positions=self.config.max_open_positions,
            max_new_positions_per_hour=self.config.max_new_positions_per_hour,
            position_size_sol=self.config.position_size_sol,
        )

        # Risk detector
        from research.phase4.phase4b.risk.rug_detector import RugDetector
        self.rug_detector = RugDetector()

        # Meta engine
        from research.phase4.phase4b.scoring.meta_engine import MetaEngine
        self.meta_engine = MetaEngine()

        # Narrative engine
        from research.phase4.phase4b.narrative.narrative_engine import NarrativeEngine
        self.narrative_engine = NarrativeEngine()

        # Discovery manager
        self.discovery_manager = DiscoveryManager(
            queue=self.discovery_queue,
            pumpportal_api_key=self.config.pumpportal_api_key,
            enable_fallback=self.config.enable_fallback,
            db_path=self.config.db_path,
        )

        logger.info("Pipeline initialization complete")

    async def start(self) -> None:
        """Start the pipeline."""
        self._running = True
        
        # Start discovery
        self._tasks.append(asyncio.create_task(self.discovery_manager.start()))

        # Start main processing loop
        self._tasks.append(asyncio.create_task(self._processing_loop()))

        # Start metrics reporting
        self._tasks.append(asyncio.create_task(self._metrics_loop()))

        # Start outcome sampling loop
        self._tasks.append(asyncio.create_task(self._outcome_sampling_loop()))

        logger.info("Pipeline started")

    async def shutdown(self) -> None:
        """Gracefully shutdown the pipeline."""
        logger.info("Shutting down pipeline...")
        self._running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Shutdown components
        if self.discovery_manager:
            await self.discovery_manager.shutdown()

        if self.helius_client:
            await self.helius_client.__aexit__(None, None, None)

        # Print final stats
        self._print_final_stats()

        logger.info("Pipeline shutdown complete")

    async def _processing_loop(self) -> None:
        """Main processing loop - handles candidate pipeline."""
        # Use instance narrative engine
        narrative_engine = self.narrative_engine
        
        while self._running:
            try:
                # Process discovery queue
                await self._process_discovery_queue()

                # Process pre-scoring
                await self._process_pre_scoring()

                # Process enrichment
                await self._process_enrichment()

                # Process risk analysis
                await self._process_risk_analysis()

                # Process narrative analysis
                await self._process_narrative_analysis(narrative_engine)

                # Process meta decisions
                await self._process_meta_decisions()

                # Process paper entries
                await self._process_paper_entries()

                # Update paper positions
                await self._update_paper_positions()

                # Small delay to prevent busy loop
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.unhandled_exceptions += 1
                logger.error(f"Processing loop error: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _process_discovery_queue(self) -> None:
        """Process new discovery events from queue."""
        self._sync_runtime_stats()
        if not self.discovery_queue:
            return

        # Get all candidates that need pre-scoring (newly added, not yet scored)
        all_candidates = self.discovery_queue.get_all()
        newly_scored = []
        for candidate in all_candidates:
            # Skip if already scored
            if candidate.pre_score_timestamp > 0:
                continue
            
            # Run pre-scorer on the discovery event
            if candidate.discovery_event:
                event = DiscoveryEvent(
                    mint=candidate.mint,
                    source=candidate.discovery_source,
                    event_timestamp=candidate.first_discovered,
                    receive_timestamp=candidate.last_seen,
                    symbol=candidate.symbol,
                    name=candidate.name,
                    creator=candidate.discovery_event.get("creator"),
                    uri=candidate.discovery_event.get("uri"),
                    bonding_curve_key=candidate.discovery_event.get("bonding_curve_key"),
                    initial_buy=candidate.discovery_event.get("initial_buy"),
                    sol_amount=candidate.discovery_event.get("sol_amount"),
                    v_tokens_in_bonding_curve=candidate.discovery_event.get("v_tokens_in_bonding_curve"),
                    v_sol_in_bonding_curve=candidate.discovery_event.get("v_sol_in_bonding_curve"),
                    market_cap_sol=candidate.discovery_event.get("market_cap_sol"),
                    is_mayhem_mode=candidate.discovery_event.get("is_mayhem_mode"),
                    pool=candidate.discovery_event.get("pool"),
                    signature=candidate.discovery_event.get("signature"),
                    raw_payload=candidate.discovery_event.get("raw_payload"),
                )
                
                scored_candidate = self.pre_scorer.evaluate(event)
                
                # Update the candidate with scored values
                candidate.pre_score = scored_candidate.pre_score
                candidate.pre_score_components = scored_candidate.pre_score_components
                candidate.pre_score_reason = scored_candidate.pre_score_reason
                candidate.pre_score_passed = False
                candidate.pre_score_timestamp = scored_candidate.pre_score_timestamp
                newly_scored.append(candidate)

        selected_mints = self.pre_scorer.select_for_enrichment(
            [c for c in all_candidates if c.pre_score_timestamp > 0]
        )
        changed_candidates = {candidate.mint: candidate for candidate in newly_scored}
        for candidate in all_candidates:
            if candidate.enrichment_status == "pending":
                selected = candidate.mint in selected_mints
                if candidate.pre_score_passed != selected:
                    candidate.pre_score_passed = selected
                    changed_candidates[candidate.mint] = candidate

        scored_count = sum(1 for c in all_candidates if c.pre_score_timestamp > 0)
        passed_count = sum(1 for c in all_candidates if c.pre_score_passed)
        self.stats.candidates_pre_scored = scored_count
        self.stats.candidates_passed = passed_count
        self.stats.candidates_failed = scored_count - passed_count
        self.stats.enrichment_quota = self.pre_scorer.enrichment_quota(all_candidates)

        for candidate in changed_candidates.values():
            await self._persist_candidate(candidate)

        self._sync_runtime_stats()

    def _sync_runtime_stats(self) -> None:
        """Copy authoritative component counters into the public pipeline report."""
        if self.discovery_manager:
            discovery = self.discovery_manager.get_stats()
            totals = discovery.get("totals", {})
            self.stats.raw_events = totals.get("valid_discovery_events", 0)
            self.stats.unique_mints = totals.get("unique_mints", 0)
            self.stats.duplicate_count = totals.get("duplicate_count", 0)
            self.stats.discovery_events_persisted = totals.get("discovery_events_persisted", 0)
            pumpportal = discovery.get("pumpportal", {})
            self.stats.pumpportal_reconnects = pumpportal.get("reconnect_count", 0)
            self.stats.pumpportal_disconnects = pumpportal.get("disconnect_count", 0)

        if self.helius_client and hasattr(self.helius_client, "get_metrics"):
            helius = self.helius_client.get_metrics()
            self.stats.helius_requests = helius["requests"]
            self.stats.helius_successful_requests = helius["successful_requests"]
            self.stats.helius_http_429 = helius["http_429"]
            self.stats.helius_timeouts = helius["timeouts"]
            self.stats.helius_retries = helius["retries"]
            self.stats.helius_governor_skips = helius["governor_skips"]
            self.stats.helius_average_latency_ms = helius["average_latency_ms"]
            self.stats.helius_p95_latency_ms = helius["p95_latency_ms"]
            self.stats.helius_average_requests_per_second = helius["average_requests_per_second"]
            self.stats.helius_peak_requests_per_second = helius["peak_requests_per_second"]
            self.stats.helius_governor_state = helius["governor_state"]
            self.stats.helius_credits_used = helius["credits_used"]
            self.stats.rpc_failures = helius["failed_calls"]

    async def _process_pre_scoring(self) -> None:
        """Process pre-scoring of new candidates."""
        # This is handled inline in discovery event handling
        pass

    async def _process_enrichment(self) -> None:
        """Process enrichment for candidates that passed pre-scoring."""
        if not self.enrichment_pipeline or not self.discovery_queue:
            return

        # Get candidates ready for enrichment
        pending = self.discovery_queue.get_pending_enrichment(limit=50)
        if not pending:
            return

        # Convert candidates to enrichment format
        candidates_to_enrich = []
        for candidate in pending:
            if candidate.enrichment_status == "pending":
                candidate.enrichment_status = "in_progress"
                candidate.enrichment_attempts += 1
                candidates_to_enrich.append(candidate)

        if not candidates_to_enrich:
            return

        # Enrich batch
        try:
            enrichment_results = await self.enrichment_pipeline.enrich_batch(candidates_to_enrich)

            for candidate in candidates_to_enrich:
                if candidate.mint in enrichment_results:
                    enrichment = enrichment_results[candidate.mint]
                    candidate.enrichment_status = "completed"
                    candidate.last_enrichment = time.time()
                    # Attach enrichment data to candidate for downstream consumers
                    candidate.enrichment_data = enrichment
                    # Store enrichment data
                    await self._persist_enrichment(candidate, enrichment)
                    self.stats.candidates_enriched += 1
                    self.stats.helius_credits_used += enrichment.get("enrichment_credits", 0)
                else:
                    candidate.enrichment_status = "failed"
                    self.stats.enrichment_failures += 1

            self.stats.enrichment_attempts = sum(
                1 for candidate in self.discovery_queue.get_all()
                if candidate.enrichment_attempts > 0
            )

        except Exception as e:
            self.stats.enrichment_failures += len(candidates_to_enrich)
            logger.error(f"Enrichment batch failed: {e}")

    async def _process_risk_analysis(self) -> None:
        """Analyze risk for enriched candidates using real rug detector."""
        if not self.discovery_queue or not self.rug_detector:
            return

        enriched = [c for c in self.discovery_queue.get_all()
                   if c.enrichment_status == "completed" and not c.risk_evaluated]

        for candidate in enriched:
            # Use real rug detector
            enrichment_data = getattr(candidate, 'enrichment_data', {})
            discovery_event = candidate.discovery_event
            
            assessment = self.rug_detector.assess(enrichment_data, discovery_event)
            
            candidate.risk_score = assessment.risk_score
            candidate.risk_class = assessment.risk_class
            candidate.risk_reasons = assessment.risk_reasons
            candidate.risk_component_scores = assessment.component_scores
            candidate.risk_confidence = assessment.confidence
            candidate.missing_data_indicators = assessment.missing_data_indicators
            candidate.risk_evaluated = True

            if assessment.risk_class == "REJECT":
                self.stats.risk_rejected += 1
            elif assessment.risk_class == "HIGH":
                self.stats.risk_high += 1
            elif assessment.risk_class == "MODERATE":
                self.stats.risk_moderate += 1
            else:
                self.stats.risk_low += 1

            # Persist risk score
            await self._persist_risk_score(candidate)
            await self._persist_candidate(candidate)

    async def _process_narrative_analysis(self, narrative_engine: NarrativeEngine) -> None:
        """Analyze narrative for enriched candidates."""
        if not self.discovery_queue or not narrative_engine:
            return

        enriched = [c for c in self.discovery_queue.get_all()
                   if c.enrichment_status == "completed" and not c.narrative_evaluated]

        for candidate in enriched:
            enrichment_data = getattr(candidate, 'enrichment_data', {})
            discovery_event = candidate.discovery_event
            
            narrative_signal = narrative_engine.analyze(
                candidate_mint=candidate.mint,
                discovery_data=discovery_event,
                enrichment_data=enrichment_data,
            )
            
            narrative_missing = bool(narrative_signal.get("missing_data", True))
            candidate.narrative_category = narrative_signal.get("category", "unknown")
            candidate.narrative_score = None if narrative_missing else narrative_signal.get("score")
            candidate.narrative_confidence = None if narrative_missing else narrative_signal.get("confidence")
            candidate.narrative_reasons = narrative_signal.get("reasons", [])
            candidate.narrative_sources_observed = narrative_signal.get("sources_observed", 0)
            candidate.narrative_unique_sources = narrative_signal.get("unique_sources", 0)
            candidate.narrative_mentions_total = narrative_signal.get("mentions_total", 0)
            candidate.narrative_mentions_delta = narrative_signal.get("mentions_delta", 0.0)
            candidate.narrative_unique_sources_delta = narrative_signal.get("unique_sources_delta", 0.0)
            candidate.narrative_engagement_delta = narrative_signal.get("engagement_delta", 0.0)
            candidate.narrative_missing_data = narrative_signal.get("missing_data", True)
            candidate.narrative_unknown_state = narrative_signal.get("unknown_state", True)
            candidate.narrative_evaluated = True
            
            # Persist narrative
            await self._persist_narrative(candidate, narrative_signal)
            await self._persist_candidate(candidate)

    async def _process_meta_decisions(self) -> None:
        """Make meta-scoring decisions for risk-passed candidates."""
        if not self.discovery_queue or not self.meta_engine:
            return

        # Get candidates that passed risk and are ready for meta decision
        ready = [c for c in self.discovery_queue.get_all()
                if c.enrichment_status == "completed"
                and c.risk_evaluated
                and c.risk_class != "REJECT"
                and c.narrative_evaluated
                and not c.meta_evaluated]

        for candidate in ready:
            # Use real meta engine with actual data from candidate
            discovery_quality = candidate.pre_score * 100
            
            # On-Chain Quality - derived from enrichment
            onchain_quality = 0.0
            completeness = 0.0
            if hasattr(candidate, 'enrichment_data') and candidate.enrichment_data:
                enrichment = candidate.enrichment_data
                # Check enrichment completeness
                if enrichment.get("token_info") is not None:
                    completeness += 0.3
                if enrichment.get("largest_accounts") is not None:
                    completeness += 0.2
                if enrichment.get("holder_analysis") is not None:
                    completeness += 0.2
                if enrichment.get("creator_signatures") is not None:
                    completeness += 0.15
                if enrichment.get("creator_accounts") is not None:
                    completeness += 0.15
                onchain_quality = completeness * 100
            
            # Liquidity / Execution - derived from the actual sell quote. Holder
            # concentration belongs to Risk and is not a liquidity proxy.
            liquidity_execution = None

            # Narrative / Momentum - from narrative engine
            narrative_momentum = None
            narrative_confidence = 0.0
            if (
                not getattr(candidate, "narrative_missing_data", True)
                and hasattr(candidate, 'narrative_score')
                and candidate.narrative_score is not None
            ):
                narrative_momentum = candidate.narrative_score
            if narrative_momentum is not None and candidate.narrative_confidence is not None:
                narrative_confidence = candidate.narrative_confidence
            
            # Creator Quality remains missing unless creator evidence was actually
            # collected. It must not inherit the unrelated discovery score.
            creator_quality = None
            creator_confidence = 0.0
            
            risk_score = int(candidate.risk_score)
            risk_class = candidate.risk_class
            
            sell_quote = candidate.enrichment_data.get("sell_quote", {})
            price_impact_pct = abs(float(sell_quote["price_impact_pct"]))
            quote_age = max(0.0, time.time() - float(sell_quote["timestamp"]))
            quote_quality = (
                max(0.0, 1.0 - min(price_impact_pct / 100.0, 1.0))
                if quote_age <= 30 else 0.0
            )
            liquidity_execution = quote_quality * 100.0
            slippage_estimate = price_impact_pct / 100.0

            meta_result = self.meta_engine.evaluate(
                discovery_quality=discovery_quality,
                onchain_quality=onchain_quality,
                liquidity_execution=liquidity_execution,
                narrative_momentum=narrative_momentum,
                creator_quality=creator_quality,
                risk_score=risk_score,
                risk_class=risk_class,
                narrative_confidence=narrative_confidence,
                enrichment_completeness=completeness,
                quote_quality=quote_quality,
                slippage_estimate=slippage_estimate,
                creator_confidence=creator_confidence,
                risk_confidence=getattr(candidate, "risk_confidence", 0.0),
            )

            candidate.meta_score = meta_result["meta_score"]
            candidate.meta_evaluated = True
            candidate.meta_confidence = meta_result["confidence"]
            candidate.meta_decision = meta_result["decision"]
            candidate.meta_rejection_reason = meta_result.get("rejection_reason", "")
            candidate.meta_component_breakdown = meta_result.get("component_breakdown", {})
            candidate.recommended_size_sol = meta_result.get("recommended_size_sol", 0.02)
            candidate.calibration = meta_result.get("calibration", "EMPIRICAL_UNCALIBRATED")
            candidate.expected_return_estimate = meta_result.get("expected_return")
            candidate.expected_downside = meta_result.get("expected_downside")
            candidate.estimated_execution_cost = meta_result.get("estimated_execution_cost")
            candidate.estimated_slippage = meta_result.get("estimated_slippage")
            candidate.risk_adjusted_ev = meta_result.get("risk_adjusted_ev")

            if meta_result["decision"] == "approve":
                candidate.meta_approved = True
                self.stats.meta_approved += 1
            else:
                candidate.meta_approved = False
                self.stats.meta_rejected += 1

            await self._persist_meta_decision(candidate)
            await self._persist_candidate(candidate)

    async def _process_paper_entries(self) -> None:
        """Enter paper positions for meta-approved candidates."""
        if not self.paper_adapter or not self.discovery_queue:
            return

        # Get candidates ready for entry
        ready = [c for c in self.discovery_queue.get_all()
                if c.meta_approved and not c.paper_entered]

        for candidate in ready:
            # Check position limits
            if not self.paper_adapter.can_open_position():
                logger.warning("Position limit reached, skipping entry")
                break

            # Create entry intent - use recommended size from meta engine
            position_size = getattr(candidate, 'recommended_size_sol', self.config.position_size_sol)
            
            intent = EntryIntent(
                mint=candidate.mint,
                symbol=candidate.symbol,
                size_sol=position_size,
                entry_price_hint=0.0,  # Will be filled by actual execution quote
                max_slippage_bps=50,
                max_priority_fee_sol=0.0005,
            )

            # Execute entry
            result = await self.paper_adapter.execute_entry(intent)

            if result.success:
                candidate.paper_entered = True
                candidate.paper_entry_time = time.time()
                # Store actual execution data for persistence
                candidate.actual_entry_price = result.fill_price_sol
                candidate.actual_tokens_received = result.tokens_received
                candidate.actual_sol_spent = result.sol_spent
                candidate.actual_priority_fee = result.priority_fee_sol
                candidate.actual_route_fees = result.route_fees_sol
                candidate.actual_total_cost_basis = result.total_cost_basis_sol
                candidate.actual_slippage = result.slippage_bps
                candidate.actual_route_provider = result.route_provider
                candidate.quote_timestamp = result.quote_timestamp
                self.stats.paper_entries += 1
                logger.info(f"Paper entry: {candidate.symbol} ({candidate.mint[:8]}...) @ {result.fill_price_sol:.8f} SOL/token")

                # Persist entry with actual execution values
                await self._persist_paper_entry(candidate, intent, True, result)
                await self._persist_candidate(candidate)
            else:
                self.stats.provider_failures += 1
                logger.warning(f"Entry failed for {candidate.mint}: {result.error}")
                await self._persist_paper_entry(candidate, intent, False, result)

    async def _update_paper_positions(self) -> None:
        """Update all open paper positions with market data."""
        if not self.paper_adapter:
            return

        # Get market data for all open positions
        market_data = await self._fetch_market_data()

        if market_data:
            triggers = self.paper_adapter.update_positions(market_data)

            # Check for exits
            for mint, mint_triggers in triggers.items():
                final_triggers = [t for t in mint_triggers if t in (
                    "INITIAL_STOP", "TRAILING_STOP", "BREAKEVEN_STOP",
                    "TIME_EXIT", "LIQUIDITY_EXIT", "SELL_PRESSURE_EXIT",
                )]

                if final_triggers:
                    # Position will be closed, stats updated in paper_adapter
                    self.stats.paper_exits += 1
                    if mint in self.paper_adapter.positions:
                        pos = self.paper_adapter.positions[mint]
                        if pos.tokens_remaining == 0:
                            self.stats.final_exits += 1
                        else:
                            self.stats.partial_exits += 1

    async def _fetch_market_data(self) -> Dict[str, Dict]:
        """Fetch current market data for open positions."""
        if not self.paper_adapter or not self.helius_client:
            return {}

        market_data = {}
        for mint, position in self.paper_adapter.positions.items():
            try:
                # Get price quote
                quote = await self.helius_client.get_price(mint, side="sell", size_sol=0.02)
                if quote:
                    market_data[mint] = {
                        "price": quote.executable_price,
                        "liquidity_usd": None,
                        "market_cap_usd": None,
                        "volume_24h_usd": None,
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch market data for {mint}: {e}")

        return market_data

    async def _outcome_sampling_loop(self) -> None:
        """Schedule and collect outcome samples for all candidates."""
        while self._running:
            try:
                await self._schedule_outcome_samples()
                await asyncio.sleep(60.0)  # Check every minute
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Outcome sampling error: {e}")
                await asyncio.sleep(30.0)

    async def _schedule_outcome_samples(self) -> None:
        """Schedule and collect outcome observations for candidates."""
        if not self.discovery_queue or not self.helius_client:
            return

        all_candidates = self.discovery_queue.get_all()
        now = time.time()

        for candidate in all_candidates:
            if not getattr(candidate, "_outcome_sampling_scheduled", False):
                candidate._outcome_sampling_scheduled = True
                self.stats.outcomes_scheduled += 1

            if not hasattr(candidate, "_outcomes_recorded"):
                candidate._outcomes_recorded = set()

            # Schedule outcome observations at various horizons.
            horizons = [60, 300, 900, 1800, 3600, 14400, 86400]  # seconds

            for horizon in horizons:
                if candidate.first_discovered + horizon > now:
                    continue
                if horizon in candidate._outcomes_recorded:
                    continue

                self.stats.snapshots_due += 1
                status, price_missing = await self._record_outcome_snapshot(candidate, horizon, now)
                if status == "inserted":
                    candidate._outcomes_recorded.add(horizon)
                    self.stats.snapshots_inserted += 1
                    self.stats.outcomes_completed += 1
                    if price_missing:
                        self.stats.missing_price_observations += 1
                elif status == "already_exists":
                    candidate._outcomes_recorded.add(horizon)
                    self.stats.duplicate_snapshots_skipped += 1
                else:
                    self.stats.observation_errors += 1

    async def _record_outcome_snapshot(
        self,
        candidate: Candidate,
        horizon: int,
        now: float,
    ) -> tuple[str, bool]:
        """Record a snapshot and return (status, price_missing)."""
        import sqlite3

        conn = None
        try:
            # Check idempotency before making provider calls.
            conn = sqlite3.connect(str(self.db_path))
            existing = conn.execute(
                "SELECT 1 FROM outcome_snapshots WHERE candidate_mint = ? AND horizon_seconds = ? LIMIT 1",
                (candidate.mint, horizon),
            ).fetchone()
            if existing:
                return "already_exists", False
            conn.close()
            conn = None

            # Get current price quote
            quote = await self.helius_client.get_price(candidate.mint, side="sell", size_sol=0.02)
            
            price_sol = quote.executable_price if quote else None
            executable_buy_price = None
            executable_sell_price = price_sol
            liquidity_usd = None
            price_change_from_decision = None
            
            # Calculate price change from decision time if we have entry data
            if hasattr(candidate, 'actual_entry_price') and candidate.actual_entry_price and price_sol:
                price_change_from_decision = (price_sol - candidate.actual_entry_price) / candidate.actual_entry_price
            
            # Get sell quote status
            sell_quote_available = None
            try:
                sell_quote = await self.helius_client.get_sell_quote(candidate.mint, 1000.0, slippage_bps=50)
                sell_quote_available = bool(sell_quote)
            except Exception as exc:
                logger.debug("Sellability observation unavailable for %s: %s", candidate.mint, exc)
            
            # Persist to database
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.execute("""
                INSERT OR IGNORE INTO outcome_snapshots
                (candidate_mint, horizon_seconds, observation_time, price_sol,
                 executable_buy_price, executable_sell_price, liquidity_usd,
                 price_change_from_decision, max_favorable_excursion, max_adverse_excursion,
                 peak_price, peak_return_pct, trough_price, trough_return_pct,
                 time_to_peak, time_to_trough, liquidity_change_pct,
                 tradable, sellable, rug_detected, liquidity_disappeared,
                 freeze_authority_activated, missing_data_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint,
                horizon,
                now,
                price_sol,
                executable_buy_price,
                executable_sell_price,
                liquidity_usd,
                price_change_from_decision,
                None,  # max_favorable_excursion
                None,  # max_adverse_excursion
                None,  # peak_price
                None,  # peak_return_pct
                None,  # trough_price
                None,  # trough_return_pct
                None,  # time_to_peak
                None,  # time_to_trough
                None,  # liquidity_change_pct
                1 if price_sol is not None else None,  # tradable unknown if no observation
                1 if sell_quote_available else (0 if sell_quote_available is False else None),
                None,  # rug_detected was not measured
                None,  # liquidity_disappeared was not measured
                None,  # freeze_authority_activated was not measured
                None if price_sol is not None else "price_observation_unavailable",
                now,  # created_at
            ))
            conn.commit()
            if cursor.rowcount == 0:
                return "already_exists", False
            return "inserted", price_sol is None
            
        except Exception as e:
            logger.error(f"Failed to record outcome snapshot for {candidate.mint} at {horizon}s: {e}")
            self.stats.database_errors += 1
            return "failed", True
        finally:
            if conn is not None:
                conn.close()

    async def _metrics_loop(self) -> None:
        """Periodic metrics logging."""
        while self._running:
            try:
                await asyncio.sleep(60.0)  # Log every minute
                self._log_metrics()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Metrics loop error: {e}")

    def _log_metrics(self) -> None:
        """Log current pipeline metrics."""
        self._sync_runtime_stats()
        stats = self.stats.to_dict()
        logger.info(f"Pipeline Metrics: {json.dumps(stats, indent=2)}")

    def _print_final_stats(self) -> None:
        """Print final statistics at shutdown."""
        self._sync_runtime_stats()
        stats = self.stats.to_dict()
        stats["runtime_seconds"] = time.time() - self._start_time
        print("\n" + "="*60)
        print("PHASE 4B SOAK TEST - FINAL STATISTICS")
        print("="*60)
        print(json.dumps(stats, indent=2))
        print("="*60)

        # Save to file
        results_dir = Path(self.config.db_path).parent
        results_file = results_dir / f"soak_test_results_{int(time.time())}.json"
        with open(results_file, "w") as f:
            json.dump(stats, f, indent=2)
        logger.info(f"Results saved to {results_file}")

    # Persistence methods
    async def _persist_candidate(self, candidate: Candidate) -> None:
        """Persist candidate to database."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            risk_score = candidate.risk_score if candidate.risk_evaluated else None
            risk_class = candidate.risk_class if candidate.risk_evaluated else None
            risk_reasons = json.dumps(candidate.risk_reasons) if candidate.risk_evaluated else None
            narrative_score = candidate.narrative_score if candidate.narrative_evaluated else None
            narrative_category = candidate.narrative_category if candidate.narrative_evaluated else None
            narrative_confidence = candidate.narrative_confidence if candidate.narrative_evaluated else None
            meta_score = candidate.meta_score if candidate.meta_evaluated else None
            meta_confidence = candidate.meta_confidence if candidate.meta_evaluated else None
            meta_approved = int(candidate.meta_approved) if candidate.meta_evaluated else None
            rejection_reason = (
                getattr(candidate, "meta_rejection_reason", None)
                if candidate.meta_evaluated else None
            )
            conn.execute("""
                INSERT OR REPLACE INTO candidates
                (mint, symbol, name, first_discovered, discovery_source,
                 duplicate_count, last_seen, pre_score, pre_score_components,
                 pre_score_reason, pre_score_passed, pre_score_timestamp,
                 enrichment_status, enrichment_attempts, last_enrichment,
                 risk_score, risk_class, risk_reasons,
                 narrative_score, narrative_category, narrative_confidence,
                 meta_score, meta_confidence, meta_approved, rejection_reason,
                 paper_entered, paper_entry_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, candidate.symbol, candidate.name,
                candidate.first_discovered, candidate.discovery_source.value if candidate.discovery_source else None,
                candidate.duplicate_count, candidate.last_seen,
                candidate.pre_score, json.dumps(candidate.pre_score_components),
                candidate.pre_score_reason, int(candidate.pre_score_passed),
                candidate.pre_score_timestamp,
                candidate.enrichment_status, candidate.enrichment_attempts,
                candidate.last_enrichment,
                risk_score, risk_class, risk_reasons,
                narrative_score, narrative_category, narrative_confidence,
                meta_score, meta_confidence, meta_approved, rejection_reason,
                int(candidate.paper_entered), candidate.paper_entry_time,
                time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist candidate: {e}")

    async def _persist_enrichment(self, candidate: Candidate, enrichment: Dict) -> None:
        """Persist enrichment data."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO enrichments
                (mint, candidate_id, enriched_at, enrichment_credits,
                 token_info, largest_accounts, holder_analysis,
                 creator_signatures, creator_accounts, raw_enrichment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint,
                None,  # candidate_id would need lookup
                enrichment.get("enriched_at", time.time()),
                enrichment.get("enrichment_credits", 0),
                json.dumps(enrichment.get("token_info", {})),
                json.dumps(enrichment.get("largest_accounts", [])),
                json.dumps(enrichment.get("holder_analysis", {})),
                json.dumps(enrichment.get("creator_signatures", [])),
                json.dumps(enrichment.get("creator_accounts", [])),
                json.dumps(enrichment)
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist enrichment: {e}")

    async def _persist_narrative(self, candidate: Candidate, narrative_signal: Dict) -> None:
        """Persist narrative analysis."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            missing = bool(narrative_signal.get("missing_data", True))
            conn.execute("""
                INSERT INTO narratives
                (mint, candidate_id, snapshot_at, narrative_category, narrative_strength,
                 narrative_velocity, social_presence_score, social_activity_score,
                 duplicate_narrative_count, trend_alignment, pop_culture_relevance,
                 metadata_quality, source_count, confidence, raw_sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, None, time.time(),
                narrative_signal.get("category", "unknown"),
                None if missing else narrative_signal.get("score"),
                None if missing else narrative_signal.get("mentions_delta"),
                None if missing else narrative_signal.get("sources_observed"),
                None if missing else narrative_signal.get("unique_sources"),
                None,  # duplicate_narrative_count was not measured
                None,  # trend_alignment
                None,  # pop_culture_relevance was not measured
                None,  # metadata_quality was not measured
                None if missing else narrative_signal.get("unique_sources"),
                None if missing else narrative_signal.get("confidence"),
                json.dumps({
                    "reasons": narrative_signal.get("reasons", []),
                    "mentions_total": narrative_signal.get("mentions_total", 0),
                    "engagement_delta": narrative_signal.get("engagement_delta", 0.0),
                    "missing_data": narrative_signal.get("missing_data", True),
                    "unknown_state": narrative_signal.get("unknown_state", True),
                })
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist narrative: {e}")

    async def _persist_risk_score(self, candidate: Candidate) -> None:
        """Persist risk score with actual values."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            
            # Extract actual values from risk assessment
            mint_authority_active = None
            freeze_authority_active = None
            creator_holdings_pct = None
            top_1_holder_pct = None
            top_10_holders_pct = None
            initial_liquidity_sol = None
            current_liquidity_sol = None
            liquidity_change_pct = None
            creator_rapid_launches = None
            metadata_abnormalities = None
            
            if hasattr(candidate, 'risk_component_scores') and candidate.risk_component_scores:
                # Extract from component scores
                pass  # Component scores already in candidate.risk_reasons
            
            # Try to get from enrichment data
            if hasattr(candidate, 'enrichment_data') and candidate.enrichment_data:
                enrichment = candidate.enrichment_data
                if enrichment.get("token_info"):
                    token_info = enrichment["token_info"]
                    mint_authority_active = 1 if token_info.get("mint_authority") else 0
                    freeze_authority_active = 1 if token_info.get("freeze_authority") else 0
                if enrichment.get("holder_analysis"):
                    ha = enrichment["holder_analysis"]
                    if ha.get("holder_classification_complete") is True:
                        top_1_holder_pct = ha.get("top_1_non_protocol_pct_total_supply")
                        top_10_holders_pct = ha.get("top_10_non_protocol_pct_total_supply")
                    else:
                        top_1_holder_pct = ha.get("top_1_all_accounts_pct")
                        top_10_holders_pct = ha.get("top_10_all_accounts_pct")
            
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO risk_scores
                (mint, candidate_id, risk_score, risk_class, risk_reasons,
                 mint_authority_active, freeze_authority_active,
                 creator_holdings_pct, top_1_holder_pct, top_10_holders_pct,
                 initial_liquidity_sol, current_liquidity_sol, liquidity_change_pct,
                 creator_rapid_launches, metadata_abnormalities)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, None,
                candidate.risk_score, candidate.risk_class,
                json.dumps(candidate.risk_reasons),
                mint_authority_active, freeze_authority_active,
                creator_holdings_pct, top_1_holder_pct, top_10_holders_pct,
                initial_liquidity_sol, current_liquidity_sol, liquidity_change_pct,
                creator_rapid_launches, metadata_abnormalities
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist risk score: {e}")

    async def _persist_meta_decision(self, candidate: Candidate) -> None:
        """Persist meta decision with actual component scores."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            
            # Use actual component breakdown from meta engine
            component_breakdown = candidate.meta_component_breakdown if hasattr(candidate, 'meta_component_breakdown') else {}
            discovery_score = component_breakdown.get("discovery_quality")
            onchain_score = component_breakdown.get("onchain_quality")
            liquidity_score = component_breakdown.get("liquidity_execution")
            narrative_score = component_breakdown.get("narrative_momentum")
            creator_score = component_breakdown.get("creator_quality")
            risk_penalty = component_breakdown.get("risk_penalty")
            
            # Use actual values from meta engine, not synthetic fallbacks
            expected_return_estimate = candidate.expected_return_estimate if hasattr(candidate, 'expected_return_estimate') else None
            expected_downside = candidate.expected_downside if hasattr(candidate, 'expected_downside') else None
            estimated_execution_cost = candidate.estimated_execution_cost if hasattr(candidate, 'estimated_execution_cost') else None
            estimated_slippage = candidate.estimated_slippage if hasattr(candidate, 'estimated_slippage') else None
            risk_adjusted_ev = candidate.risk_adjusted_ev if hasattr(candidate, 'risk_adjusted_ev') else None
            
            conn.execute("""
                INSERT INTO meta_decisions
                (mint, candidate_id, decided_at, discovery_score, onchain_score,
                 liquidity_score, narrative_score, creator_score, risk_penalty,
                 meta_score, confidence, expected_return_estimate, expected_downside,
                 estimated_execution_cost, estimated_slippage, risk_adjusted_ev,
                 recommended_size_sol, entry_reason, rejection_reason, decision,
                 component_scores, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, None, time.time(),
                discovery_score, onchain_score,
                liquidity_score, narrative_score, creator_score, risk_penalty,
                candidate.meta_score, candidate.meta_confidence,
                expected_return_estimate, expected_downside,
                estimated_execution_cost, estimated_slippage, risk_adjusted_ev,
                candidate.recommended_size_sol if hasattr(candidate, 'recommended_size_sol') else None,
                "approve" if candidate.meta_approved else "reject",
                candidate.meta_rejection_reason if hasattr(candidate, 'meta_rejection_reason') else None,
                candidate.meta_decision if hasattr(candidate, 'meta_decision') else None,
                json.dumps(candidate.meta_component_breakdown if hasattr(candidate, 'meta_component_breakdown') else {}),
                time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist meta decision: {e}")

    async def _persist_paper_entry(self, candidate: Candidate, intent: EntryIntent, success: bool, result: Optional[FillResult] = None) -> None:
        """Persist paper entry attempt with actual execution values."""
        if not success or result is None or not result.success:
            # The schema models positions and fills, not failed attempts. Writing
            # an all-zero REJECTED position fabricates a fill that never happened.
            return
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            
            entry_price = result.fill_price_sol
            initial_tokens = float(result.tokens_received)
            entry_cost_basis = result.total_cost_basis_sol
            entry_fees = result.priority_fee_sol
            size_sol = result.sol_spent
            entry_time = candidate.paper_entry_time or time.time()
            
            cursor = conn.execute("""
                INSERT INTO paper_positions
                (mint, symbol, candidate_id, entry_price, entry_time, size_sol,
                 initial_tokens, tokens_remaining, entry_cost_basis_sol, entry_fees_sol, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, candidate.symbol, None,
                entry_price, entry_time, size_sol,
                initial_tokens, initial_tokens, entry_cost_basis, entry_fees,
                "OPEN"
            ))
            conn.execute("""
                    INSERT INTO paper_fills
                    (position_id, mint, side, fill_time, tokens_filled, avg_price,
                     gross_proceeds_sol, network_costs_sol, net_proceeds_sol,
                     realized_pnl_sol, quote_timestamp, quote_age_seconds,
                     quote_price_impact_pct, quote_slippage_bps, quote_route,
                     quote_swap_fee_bps, quote_platform_fee_bps, priority_fee_sol,
                     tokens_remaining, realized_pnl_cumulative)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cursor.lastrowid, candidate.mint, result.side, entry_time,
                float(result.tokens_filled), result.avg_price,
                float(result.gross_proceeds), float(result.network_costs),
                float(result.net_proceeds), float(result.realized_pnl),
                result.quote_timestamp,
                max(0.0, entry_time - result.quote_timestamp),
                result.price_impact_pct, result.slippage_bps,
                result.route_provider, result.swap_fee_bps,
                result.platform_fee_bps, result.priority_fee_sol,
                float(result.tokens_filled), float(result.realized_pnl),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist paper entry: {e}")


async def run_soak_test(config: Optional[PipelineConfig] = None, max_runtime: int = 7200) -> Dict[str, Any]:
    """
    Run the Phase 4B soak test.

    Args:
        config: Pipeline configuration
        max_runtime: Maximum runtime in seconds (default 2 hours)

    Returns:
        Final pipeline statistics
    """
    if config is None:
        config = PipelineConfig(max_runtime_seconds=max_runtime)

    pipeline = Phase4BPipeline(config)

    try:
        await pipeline.initialize()
        await pipeline.start()

        # Run for specified duration
        await asyncio.sleep(config.max_runtime_seconds)

    except asyncio.CancelledError:
        logger.info("Soak test cancelled")
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Soak test error: {e}", exc_info=True)
    finally:
        await pipeline.shutdown()

    return pipeline.stats.to_dict()


async def run_24h_collection(config: Optional[PipelineConfig] = None) -> Dict[str, Any]:
    """Run 24-hour paper collection."""
    if config is None:
        config = PipelineConfig(max_runtime_seconds=86400)  # 24 hours
    return await run_soak_test(config, 86400)


if __name__ == "__main__":
    # Run 2-hour soak test by default
    import sys

    # Parse command line args
    max_runtime = 7200  # 2 hours default
    if len(sys.argv) > 1:
        if sys.argv[1] == "24h":
            max_runtime = 86400
        else:
            try:
                max_runtime = int(sys.argv[1])
            except ValueError:
                pass

    config = PipelineConfig(max_runtime_seconds=max_runtime)

    results = asyncio.run(run_soak_test(config, max_runtime))
    print("\nSoak test completed!")
    print(json.dumps(results, indent=2))
