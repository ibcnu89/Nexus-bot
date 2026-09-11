"""
Outcome Sampling - Records future outcomes for all candidates (selected and rejected).

Implements leakage-free outcome labeling for experimental calibration.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..discovery.models import Candidate
    from ..discovery.models import DiscoveryQueue

logger = logging.getLogger(__name__)


@dataclass
class OutcomeSnapshot:
    """Single outcome observation at a specific horizon."""
    candidate_mint: str
    horizon_seconds: int
    observation_time: float
    price_sol: Optional[float] = None
    executable_buy_price: Optional[float] = None
    executable_sell_price: Optional[float] = None
    liquidity_usd: Optional[float] = None
    price_change_from_decision: Optional[float] = None
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None
    peak_price: Optional[float] = None
    peak_return_pct: Optional[float] = None
    trough_price: Optional[float] = None
    trough_return_pct: Optional[float] = None
    time_to_peak: Optional[float] = None
    time_to_trough: Optional[float] = None
    liquidity_change_pct: Optional[float] = None
    tradable: bool = False
    sellable: bool = False
    rug_detected: bool = False
    liquidity_disappeared: bool = False
    freeze_authority_activated: bool = False
    missing_data_reason: Optional[str] = None
    horizon_seconds: int = 0


@dataclass
class OutcomeTracker:
    """Tracks outcome observations for a candidate across horizons."""
    candidate_mint: str
    decision_time: float
    decision_price_sol: Optional[float] = None
    horizons: List[int] = field(default_factory=lambda: [60, 300, 900, 1800, 3600, 14400, 86400])
    snapshots: Dict[int, Any] = field(default_factory=dict)  # horizon -> snapshot
    completed_horizons: set = field(default_factory=set)
    final_outcome_recorded: bool = False
    
    # MFE/MAE tracking
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    peak_price: Optional[float] = None
    trough_price: Optional[float] = None
    peak_time: Optional[float] = None
    trough_time: Optional[float] = None


class OutcomeSampler:
    """
    Records future outcomes for all candidates (selected and rejected).
    
    Critical for unbiased signal calibration - must track rejected candidates too.
    Strictly separates decision-time features from future outcome labels.
    """
    
    # Standard observation horizons (seconds)
    DEFAULT_HORIZONS = [60, 300, 900, 1800, 3600, 14400, 86400]  # 1m, 5m, 15m, 30m, 1h, 4h, 24h
    
    def __init__(
        self,
        price_source: Any,
        horizons: Optional[List[int]] = None,
        max_parallel: int = 5,
        db_path: str = "research/phase4/phase4b/results/phase4b_experiment.db",
    ):
        self.price_source = price_source
        self.horizons = horizons or self.DEFAULT_HORIZONS
        self.max_parallel = max_parallel
        self.db_path = db_path
        self._trackers: Dict[str, OutcomeTracker] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
    def register_candidate(self, candidate_mint: str, decision_time: float, decision_price: Optional[float] = None) -> None:
        """Register a candidate for outcome tracking."""
        if candidate_mint not in self._trackers:
            tracker = OutcomeTracker(
                candidate_mint=candidate_mint,
                decision_time=time.time(),
                decision_price_sol=None,
            )
            self._trackers[candidate_mint] = tracker
    
    def register_decision(self, candidate_mint: str, decision_price_sol: Optional[float] = None) -> None:
        """Record the entry decision price for a candidate."""
        if candidate_mint in self._trackers:
            self._trackers[candidate_mint].decision_price_sol = decision_price_sol
    
    async def start(self):
        """Start the outcome sampling loop."""
        self._running = True
        while True:
            await self._collect_outcomes()
            await asyncio.sleep(30)  # Check every 30 seconds
    
    async def stop(self):
        """Stop the outcome sampler."""
        pass  # Handled by main loop
    
    async def _collect_outcomes(self):
        """Collect outcome observations for all tracked candidates."""
        current_time = time.time()
        
        # This would fetch current prices and record observations
        # For now, this is a framework - actual implementation needs price_source integration
        pass
    
    def get_outcomes_for_candidate(self, mint: str) -> Dict:
        """Get all recorded outcomes for a candidate."""
        if mint not in self._trackers:
            return {}
        tracker = self._trackers[mint]
        return {
            "mint": mint,
            "decision_time": tracker.decision_time,
            "decision_price": tracker.decision_price_sol,
            "snapshots": tracker.snapshots,
            "completed_horizons": list(tracker.completed_horizons),
        }


class OutcomePersister:
    """Persists outcome observations to database."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def persist_snapshot(self, snapshot: Any) -> None:
        """Persist a single outcome snapshot."""
        import sqlite3
        import json
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO outcome_snapshots
                (candidate_mint, horizon_seconds, observation_time, price_sol,
                 executable_buy_price, executable_sell_price, liquidity_usd,
                 price_change_from_decision, max_favorable_excursion, max_adverse_excursion,
                 peak_price, peak_return_pct, trough_price, trough_return_pct,
                 time_to_peak, time_to_trough, liquidity_change_pct,
                 tradable, sellable, rug_detected, liquidity_disappeared,
                 freeze_authority_activated, missing_data_reason, horizon_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.candidate_mint,
                snapshot.horizon_seconds,
                snapshot.observation_time,
                snapshot.price_sol,
                snapshot.executable_buy_price,
                snapshot.executable_sell_price,
                snapshot.liquidity_usd,
                snapshot.price_change_from_decision,
                snapshot.max_favorable_excursion,
                snapshot.max_adverse_excursion,
                snapshot.peak_price,
                snapshot.peak_return_pct,
                snapshot.trough_price,
                snapshot.trough_return_pct,
                snapshot.time_to_peak,
                snapshot.time_to_trough,
                snapshot.liquidity_change_pct,
                int(snapshot.tradable),
                int(snapshot.sellable),
                int(snapshot.rug_detected),
                int(snapshot.liquidity_disappeared),
                int(snapshot.freeze_authority_activated),
                snapshot.missing_data_reason,
                snapshot.horizon_seconds,
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to persist outcome snapshot: {e}")
        finally:
            conn.close()
    
    async def persist_tracker(self, tracker: 'OutcomeTracker') -> None:
        """Persist all snapshots for a tracker."""
        for snapshot in tracker.snapshots.values():
            await self.persist_snapshot(snapshot)


def create_outcome_sampler(price_source: Any, config: Optional[Dict] = None):
    """Factory to create configured OutcomeSampler."""
    config = config or {}
    return OutcomeSampler(
        price_source=price_source,
        horizons=config.get("horizons"),
        max_parallel=config.get("max_parallel", 5),
        db_path=config.get("db_path", "research/phase4/phase4b/results/phase4b_experiment.db"),
    )