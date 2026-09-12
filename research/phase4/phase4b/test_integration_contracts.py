import asyncio
import sqlite3
import time

from research.phase4.phase4b.discovery.models import Candidate, DiscoveryQueue, DiscoverySource
from research.phase4.phase4b.monitoring.outcome_sampler import OutcomePersister, OutcomeSnapshot
from research.phase4.phase4b.run_pipeline import Phase4BPipeline
from research.phase4.phase4b.scoring.pre_score import PreScorer
from research.phase4.phase4b.storage.database import SCHEMA_SQL
from research.phase4.prototype.price_source import PriceSource, TokenDecimalsError


def _candidate(index: int, score: float) -> Candidate:
    return Candidate(
        mint=f"mint-{index}",
        symbol=f"T{index}",
        name=f"Token {index}",
        first_discovered=float(index),
        discovery_source=DiscoverySource.PUMPPORTAL,
        pre_score=score,
        pre_score_timestamp=time.time(),
    )


def test_authoritative_decimals_can_be_injected_without_token_list():
    source = PriceSource()
    source.cache_token_decimals("mint", 6)
    assert source.get_token_decimals("mint") == 6
    try:
        source.cache_token_decimals("bad", -1)
    except TokenDecimalsError:
        pass
    else:
        raise AssertionError("invalid decimals must fail closed")


def test_pre_score_selects_top_three_percent():
    scorer = PreScorer(target_enrichment_pct=0.03)
    candidates = [_candidate(i, i / 100) for i in range(100)]
    selected = scorer.select_for_enrichment(candidates)
    assert selected == {"mint-97", "mint-98", "mint-99"}


def test_outcome_persistence_is_truthful_and_idempotent(tmp_path):
    db_path = tmp_path / "outcomes.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.close()

    snapshot = OutcomeSnapshot(
        candidate_mint="mint",
        horizon_seconds=60,
        observation_time=123.0,
        missing_data_reason="price_observation_unavailable",
    )
    persister = OutcomePersister(str(db_path))
    asyncio.run(persister.persist_snapshot(snapshot))
    asyncio.run(persister.persist_snapshot(snapshot))

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT tradable, sellable, rug_detected, liquidity_disappeared, "
        "freeze_authority_activated, missing_data_reason FROM outcome_snapshots"
    ).fetchall()
    conn.close()
    assert rows == [(None, None, None, None, None, "price_observation_unavailable")]


def test_unknown_narrative_is_evaluated_once():
    candidate = _candidate(1, 0.9)
    candidate.enrichment_status = "completed"
    candidate.enrichment_data = {"token_info": {"decimals": 6}}
    queue = DiscoveryQueue()
    queue._candidates[candidate.mint] = candidate

    class Engine:
        calls = 0

        def analyze(self, **kwargs):
            self.calls += 1
            return {"category": "unknown", "score": 0.0, "confidence": 0.0}

    pipeline = object.__new__(Phase4BPipeline)
    pipeline.discovery_queue = queue

    async def persist(*args):
        return None

    pipeline._persist_narrative = persist
    engine = Engine()

    async def run_twice():
        await pipeline._process_narrative_analysis(engine)
        await pipeline._process_narrative_analysis(engine)

    asyncio.run(run_twice())
    assert engine.calls == 1
    assert candidate.narrative_evaluated is True
