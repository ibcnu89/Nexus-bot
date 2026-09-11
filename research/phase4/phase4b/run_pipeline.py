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
    start_time: float = field(default_factory=time.time)

    # Discovery
    raw_events: int = 0
    unique_mints: int = 0
    duplicate_count: int = 0

    # Pre-scoring
    candidates_pre_scored: int = 0
    candidates_passed: int = 0
    candidates_failed: int = 0

    # Enrichment
    candidates_enriched: int = 0
    enrichment_failures: int = 0
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

    # Errors
    provider_failures: int = 0
    rpc_failures: int = 0
    database_errors: int = 0
    unhandled_exceptions: int = 0

    # Provider health
    pumpportal_reconnects: int = 0
    pumpportal_disconnects: int = 0
    helius_governor_state: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        elapsed = time.time() - self.start_time
        return {
            "runtime_seconds": round(elapsed, 1),
            "discovery": {
                "raw_events": self.raw_events,
                "unique_mints": self.unique_mints,
                "duplicate_rate": round(self.duplicate_count / max(self.raw_events, 1), 4),
            },
            "pre_scoring": {
                "candidates_scored": self.candidates_pre_scored,
                "passed": self.candidates_passed,
                "failed": self.candidates_failed,
                "pass_rate": round(self.candidates_passed / max(self.candidates_pre_scored, 1), 4),
            },
            "enrichment": {
                "enriched": self.candidates_enriched,
                "failures": self.enrichment_failures,
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
                "helius_governor_state": "UNKNOWN",
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

        # Discovery manager
        self.discovery_manager = DiscoveryManager(
            queue=self.discovery_queue,
            pumpportal_api_key=self.config.pumpportal_api_key,
            enable_fallback=self.config.enable_fallback,
        )

        logger.info("Pipeline initialization complete")

    async def start(self) -> None:
        """Start the pipeline."""
        self._running = True
        self._start_time = time.time()

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

                # Process meta decisions
                await self._process_meta_decisions()

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
        if not self.discovery_queue:
            return

        # Get all candidates that need pre-scoring (newly added, not yet scored)
        all_candidates = self.discovery_queue.get_all()
        for candidate in all_candidates:
            # Skip if already scored
            if candidate.pre_score_timestamp > 0:
                continue
            
            self.stats.candidates_pre_scored += 1
            
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
                candidate.pre_score_passed = scored_candidate.pre_score_passed
                candidate.pre_score_timestamp = scored_candidate.pre_score_timestamp
                
                if candidate.pre_score_passed:
                    self.stats.candidates_passed += 1
                    candidate.enrichment_status = "pending"
                    # Persist passed candidate
                    await self._persist_candidate(candidate)
                else:
                    self.stats.candidates_failed += 1
                    await self._persist_candidate(candidate)

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
                    # Store enrichment data
                    await self._persist_enrichment(candidate, enrichment)
                    self.stats.candidates_enriched += 1
                    self.stats.helius_credits_used += enrichment.get("enrichment_credits", 0)
                else:
                    candidate.enrichment_status = "failed"
                    candidate.enrichment_attempts += 1
                    self.stats.enrichment_failures += 1

        except Exception as e:
            self.stats.enrichment_failures += len(candidates_to_enrich)
            logger.error(f"Enrichment batch failed: {e}")

    async def _process_risk_analysis(self) -> None:
        """Analyze risk for enriched candidates using real rug detector."""
        if not self.discovery_queue or not self.rug_detector:
            return

        enriched = [c for c in self.discovery_queue.get_all()
                   if c.enrichment_status == "completed" and c.risk_score == 0.0]

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

    async def _process_meta_decisions(self) -> None:
        """Make meta-scoring decisions for risk-passed candidates."""
        if not self.discovery_queue or not self.meta_engine:
            return

        # Get candidates that passed risk and are ready for meta decision
        ready = [c for c in self.discovery_queue.get_all()
                if c.enrichment_status == "completed"
                and c.risk_class != "REJECT"
                and c.meta_score == 0.0
                and not c.meta_approved]

        for candidate in ready:
            # Use real meta engine
            # We need to derive inputs from candidate data
            discovery_quality = candidate.pre_score * 100
            
            onchain_quality = 50.0  # default
            if hasattr(candidate, 'enrichment_data') and candidate.enrichment_data:
                onchain_quality = 75.0
            
            liquidity_execution = 50.0  # placeholder - needs actual liquidity data
            
            narrative_momentum = 50.0  # placeholder - needs narrative engine
            
            creator_quality = candidate.pre_score * 100
            
            risk_score = int(candidate.risk_score)
            risk_class = candidate.risk_class
            
            meta_result = self.meta_engine.evaluate(
                discovery_quality=discovery_quality,
                onchain_quality=onchain_quality,
                liquidity_execution=liquidity_execution,
                narrative_momentum=narrative_momentum,
                creator_quality=creator_quality,
                risk_score=risk_score,
                risk_class=risk_class,
                narrative_confidence=0.5,
                enrichment_completeness=1.0 if candidate.enrichment_status == "completed" else 0.0,
                quote_quality=0.5,
            )

            candidate.meta_score = meta_result["meta_score"]
            candidate.meta_confidence = meta_result["confidence"]
            candidate.meta_decision = meta_result["decision"]
            candidate.meta_rejection_reason = meta_result.get("rejection_reason", "")
            candidate.meta_component_breakdown = meta_result.get("component_breakdown", {})
            candidate.recommended_size_sol = meta_result.get("recommended_size_sol", 0.02)
            candidate.calibration = meta_result.get("calibration", "EMPIRICAL_UNCALIBRATED")

            if meta_result["decision"] == "approve":
                candidate.meta_approved = True
                self.stats.meta_approved += 1
            else:
                candidate.meta_approved = False
                self.stats.meta_rejected += 1

            await self._persist_meta_decision(candidate)

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
                        "liquidity_usd": 0,  # Would come from enrichment
                        "market_cap_usd": 0,
                        "volume_24h_usd": 0,
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
        """Schedule outcome observations for candidates."""
        if not self.discovery_queue:
            return

        all_candidates = self.discovery_queue.get_all()
        now = time.time()

        for candidate in all_candidates:
            # Schedule outcome observations at various horizons
            horizons = [60, 300, 900, 1800, 3600, 14400, 86400]  # seconds

            for horizon in horizons:
                # Check if it's time to record this horizon
                if candidate.first_discovered + horizon <= now:
                    # Would record outcome here
                    # For now, just track that we've scheduled it
                    pass

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
        stats = self.stats.to_dict()
        logger.info(f"Pipeline Metrics: {json.dumps(stats, indent=2)}")

    def _print_final_stats(self) -> None:
        """Print final statistics at shutdown."""
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
                candidate.risk_score, candidate.risk_class,
                json.dumps(candidate.risk_reasons),
                candidate.narrative_score, candidate.narrative_category,
                candidate.narrative_confidence,
                candidate.meta_score, candidate.meta_confidence,
                int(candidate.meta_approved), candidate.rejection_reason,
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

    async def _persist_risk_score(self, candidate: Candidate) -> None:
        """Persist risk score."""
        try:
            import sqlite3
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
                0, 0, 0, 0, 0, 0, 0, 0, 0, "[]"
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist risk score: {e}")

    async def _persist_meta_decision(self, candidate: Candidate) -> None:
        """Persist meta decision."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO meta_decisions
                (mint, candidate_id, decided_at, discovery_score, onchain_score,
                 liquidity_score, narrative_score, creator_score, risk_penalty,
                 meta_score, confidence, expected_return_estimate, expected_downside,
                 estimated_execution_cost, estimated_slippage, risk_adjusted_ev,
                 recommended_size_sol, entry_reason, rejection_reason, decision,
                 component_scores)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, None, time.time(),
                candidate.pre_score * 20, 20, 10, 5, 15,
                candidate.risk_score * 1.5,
                candidate.meta_score, candidate.meta_confidence, 0, 0, 0, 0,
                candidate.meta_score, 0.0, "", candidate.rejection_reason,
                "approve" if candidate.meta_approved else "reject",
                json.dumps({"pre_score": candidate.pre_score, "risk": candidate.risk_score})
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.stats.database_errors += 1
            logger.error(f"Failed to persist meta decision: {e}")

    async def _persist_paper_entry(self, candidate: Candidate, intent: EntryIntent, success: bool, result: Optional[FillResult] = None) -> None:
        """Persist paper entry attempt with actual execution values."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            
            # Use actual execution data if available, otherwise fall back to intent
            if result and result.success:
                entry_price = result.avg_price
                initial_tokens = float(result.tokens_filled)
                entry_cost_basis = float(result.gross_proceeds) if result.gross_proceeds else intent.size_sol
                entry_fees = float(result.network_costs) if result.network_costs else 0
            else:
                entry_price = 0.0
                initial_tokens = 0.0
                entry_cost_basis = 0.0
                entry_fees = 0.0
            
            conn.execute("""
                INSERT INTO paper_positions
                (mint, symbol, candidate_id, entry_price, entry_time, size_sol,
                 initial_tokens, tokens_remaining, entry_cost_basis_sol, entry_fees_sol, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.mint, candidate.symbol, None,
                entry_price, time.time(), intent.size_sol,
                initial_tokens, initial_tokens, entry_cost_basis, entry_fees,
                "OPEN" if success else "REJECTED"
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