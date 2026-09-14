import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
import research.phase4.phase4b.enrichment.helius as helius_module

from research.phase4.phase4b.discovery.models import (
    Candidate,
    DiscoveryEvent,
    DiscoveryQueue,
    DiscoverySource,
)
from research.phase4.phase4b.discovery.pumpportal import PumpPortalDiscovery
from research.phase4.phase4b.enrichment.helius import HeliusClient
from research.phase4.phase4b.execution.paper_adapter import (
    EntryIntent,
    FillResult,
    PaperExecutionAdapter,
)
from research.phase4.phase4b.narrative.narrative_engine import NarrativeEngine
from research.phase4.phase4b.risk.rug_detector import RugDetector
from research.phase4.phase4b.run_pipeline import Phase4BPipeline, PipelineStats
from research.phase4.phase4b.scoring.meta_engine import MetaEngine
from research.phase4.phase4b.scoring.pre_score import PreScorer
from research.phase4.phase4b.storage.database import SCHEMA_SQL
from research.phase4.prototype.price_source import BondingCurveState, PriceSource


class OwnerFixtureHelius(HeliusClient):
    def __init__(self, owners):
        super().__init__(api_key="fixture")
        self.owners = owners
        self.requested_addresses = None

    async def get_multiple_accounts(self, addresses):
        self.requested_addresses = list(addresses)
        return [
            None if owner is None else {
                "data": {"parsed": {"info": {"owner": owner}}}
            }
            for owner in self.owners
        ]


def test_holder_analysis_parses_inner_account_values_and_excludes_curve_owner():
    client = OwnerFixtureHelius(["curve-pda", "alice", "bob"])
    largest = [
        {"address": "curve-vault", "amount": "700"},
        {"address": "alice-ata", "amount": "200"},
        {"address": "bob-ata", "amount": "100"},
    ]

    result = asyncio.run(
        client._analyze_holders(largest, {"supply": 1_000}, "curve-pda")
    )

    assert client.requested_addresses == ["curve-vault", "alice-ata", "bob-ata"]
    assert result["protocol_controlled_balance"] == 700
    assert result["protocol_accounts"] == ["curve-vault"]
    assert result["unclassified_account_count"] == 0
    assert result["holder_classification_complete"] is True
    assert result["top_1_all_accounts_pct"] == 70.0
    assert result["top_1_non_protocol_pct_total_supply"] == 20.0
    assert result["top_5_non_protocol_pct_total_supply"] == 30.0
    assert result["circulating_supply"] == 300
    assert result["top_1_non_protocol_pct_circulating_supply"] == pytest.approx(66.67)
    assert result["account_classifications"][0] == {
        "address": "curve-vault",
        "amount": 700,
        "owner": "curve-pda",
        "classification": "protocol",
        "reason": "owner_matches_bonding_curve",
    }


def test_holder_analysis_uses_mint_supply_with_fewer_than_ten_accounts():
    client = OwnerFixtureHelius([])
    largest = [
        {"address": "one", "amount": "400"},
        {"address": "two", "amount": "100"},
        {"address": "three", "amount": "50"},
    ]

    result = asyncio.run(client._analyze_holders(largest, {"supply": 1_000}))

    assert result["sampled_balance_total"] == 550
    assert result["top_1_all_accounts_pct"] == 40.0
    assert result["top_5_all_accounts_pct"] == 55.0
    assert result["top_10_all_accounts_pct"] == 55.0


def test_holder_analysis_rejects_malformed_amount_instead_of_coercing_zero():
    client = OwnerFixtureHelius([])
    result = asyncio.run(
        client._analyze_holders(
            [{"address": "broken", "amount": "not-an-integer"}],
            {"supply": 1_000},
        )
    )

    assert result["missing_data_reason"] == "invalid_account_amount"
    assert result["invalid_account_count"] == 1
    assert result["top_1_all_accounts_pct"] is None
    assert result["top_1_non_protocol_pct_total_supply"] is None


def test_risk_uses_verified_non_protocol_total_supply_measurement():
    detector = RugDetector()
    assessment = detector.assess(
        {
            "token_info": {
                "decimals": 6,
                "mint_authority": None,
                "freeze_authority": None,
            },
            "holder_analysis": {
                "holder_classification_complete": True,
                "top_1_all_accounts_pct": 90.0,
                "top_5_all_accounts_pct": 95.0,
                "top_10_all_accounts_pct": 99.0,
                "top_1_non_protocol_pct_total_supply": 10.0,
                "top_5_non_protocol_pct_total_supply": 20.0,
                "top_10_non_protocol_pct_total_supply": 30.0,
            },
            "sell_quote": {"out_amount": 1_000, "price_impact_pct": 0.1},
        },
        {},
    )

    assert assessment.risk_class == "LOW"
    assert not any("holder" in reason and "extreme" in reason for reason in assessment.risk_reasons)


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.close()


def test_discovery_event_counters_match_persisted_rows(tmp_path):
    db_path = tmp_path / "discovery.db"
    _create_db(db_path)
    queue = DiscoveryQueue()
    discovery = PumpPortalDiscovery(queue=queue, db_path=str(db_path))
    event = DiscoveryEvent(
        mint="mint",
        source=DiscoverySource.PUMPPORTAL,
        event_timestamp=100.0,
        receive_timestamp=101.0,
        symbol="TOK",
        name="Token",
        raw_payload={"mint": "mint"},
    )

    async def ingest_twice():
        await discovery._handle_event(event)
        await discovery._handle_event(event)

    asyncio.run(ingest_twice())

    conn = sqlite3.connect(db_path)
    persisted = conn.execute("SELECT COUNT(*) FROM discovery_events").fetchone()[0]
    conn.close()
    stats = discovery.get_stats()
    assert persisted == 2
    assert stats["valid_discovery_events"] == 2
    assert stats["discovery_events_persisted"] == 2
    assert stats["unique_mints"] == 1
    assert stats["duplicate_count"] == 1
    assert stats["valid_discovery_events"] == stats["unique_mints"] + stats["duplicate_count"]


class MissingPriceHelius:
    async def get_price(self, *args, **kwargs):
        return None

    async def get_sell_quote(self, *args, **kwargs):
        return None


def _outcome_pipeline(db_path: Path, first_discovered: float) -> Phase4BPipeline:
    pipeline = object.__new__(Phase4BPipeline)
    pipeline.db_path = db_path
    pipeline.stats = PipelineStats(start_time=time.time())
    pipeline.discovery_queue = DiscoveryQueue()
    pipeline.helius_client = MissingPriceHelius()
    candidate = Candidate(
        mint="outcome-mint",
        symbol="OUT",
        name="Outcome",
        first_discovered=first_discovered,
        discovery_source=DiscoverySource.PUMPPORTAL,
    )
    pipeline.discovery_queue._candidates[candidate.mint] = candidate
    return pipeline


def test_outcome_counters_distinguish_inserted_missing_and_duplicates(tmp_path):
    db_path = tmp_path / "outcomes.db"
    _create_db(db_path)
    first_discovered = time.time() - 301

    first = _outcome_pipeline(db_path, first_discovered)
    asyncio.run(first._schedule_outcome_samples())
    assert first.stats.outcomes_scheduled == 1
    assert first.stats.snapshots_due == 2
    assert first.stats.snapshots_inserted == 2
    assert first.stats.outcomes_completed == 2
    assert first.stats.missing_price_observations == 2
    assert first.stats.duplicate_snapshots_skipped == 0

    second = _outcome_pipeline(db_path, first_discovered)
    asyncio.run(second._schedule_outcome_samples())
    assert second.stats.outcomes_scheduled == 1
    assert second.stats.snapshots_due == 2
    assert second.stats.snapshots_inserted == 0
    assert second.stats.outcomes_completed == 0
    assert second.stats.duplicate_snapshots_skipped == 2

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT horizon_seconds, missing_data_reason FROM outcome_snapshots ORDER BY horizon_seconds"
    ).fetchall()
    conn.close()
    assert rows == [
        (60, "price_observation_unavailable"),
        (300, "price_observation_unavailable"),
    ]


def test_runtime_stats_report_measured_discovery_and_helius_values():
    class Discovery:
        def get_stats(self):
            return {
                "pumpportal": {"reconnect_count": 2, "disconnect_count": 1},
                "totals": {
                    "valid_discovery_events": 12,
                    "unique_mints": 10,
                    "duplicate_count": 2,
                    "discovery_events_persisted": 12,
                },
            }

    class Helius:
        def get_metrics(self):
            return {
                "requests": 8,
                "successful_requests": 6,
                "http_429": 1,
                "timeouts": 1,
                "retries": 2,
                "governor_skips": 3,
                "failed_calls": 1,
                "average_latency_ms": 12.5,
                "p95_latency_ms": 25.0,
                "average_requests_per_second": 0.4,
                "peak_requests_per_second": 2,
                "credits_used": 6,
                "governor_state": "NORMAL",
            }

    pipeline = object.__new__(Phase4BPipeline)
    pipeline.stats = PipelineStats(start_time=time.time())
    pipeline.discovery_manager = Discovery()
    pipeline.helius_client = Helius()
    pipeline._sync_runtime_stats()
    report = pipeline.stats.to_dict()

    assert report["discovery"] == {
        "raw_events": 12,
        "unique_mints": 10,
        "duplicate_rate": pytest.approx(2 / 12, abs=0.0001),
        "events_persisted": 12,
    }
    assert report["provider_health"]["helius_governor_state"] == "NORMAL"
    assert report["provider_health"]["helius_http_429"] == 1
    assert report["provider_health"]["helius_retries"] == 2
    assert report["provider_health"]["helius_governor_skips"] == 3
    assert report["provider_health"]["helius_average_requests_per_second"] == 0.4
    assert report["provider_health"]["helius_peak_requests_per_second"] == 2
    assert report["enrichment"]["helius_credits_used"] == 6


def test_helius_rpc_metrics_measure_retry_rate_limit_and_success(monkeypatch):
    class Response:
        def __init__(self, status, body=None):
            self.status = status
            self.body = body or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return self.body

    class Session:
        closed = False

        def __init__(self):
            self.responses = [
                Response(429),
                Response(200, {"result": {"value": "ok"}}),
            ]

        def post(self, *args, **kwargs):
            return self.responses.pop(0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(helius_module.asyncio, "sleep", no_sleep)
    client = HeliusClient(api_key="fixture", max_retries=2)
    client._session = Session()
    result = asyncio.run(client._rpc_call("getAccountInfo", ["mint"]))
    metrics = client.get_metrics()

    assert result == {"value": "ok"}
    assert metrics["requests"] == 2
    assert metrics["successful_requests"] == 1
    assert metrics["http_429"] == 1
    assert metrics["retries"] == 1
    assert metrics["timeouts"] == 0
    assert metrics["failed_calls"] == 0
    assert metrics["credits_used"] == 1
    assert metrics["governor_state"] == "NORMAL"
    assert metrics["peak_requests_per_second"] == 2


def test_missing_narrative_and_risk_evidence_persist_as_null(tmp_path):
    db_path = tmp_path / "truth.db"
    _create_db(db_path)
    pipeline = object.__new__(Phase4BPipeline)
    pipeline.db_path = db_path
    pipeline.stats = PipelineStats(start_time=time.time())
    candidate = Candidate(
        mint="missing-evidence",
        symbol="MISS",
        name="Missing",
        first_discovered=time.time(),
        discovery_source=DiscoverySource.PUMPPORTAL,
    )
    candidate.risk_score = 10
    candidate.risk_class = "LOW"
    candidate.risk_evaluated = True
    candidate.narrative_score = None
    candidate.narrative_confidence = None
    candidate.narrative_evaluated = True
    candidate.enrichment_data = {}

    signal = {
        "category": "unknown",
        "score": 0.0,
        "confidence": 0.0,
        "mentions_delta": 0.0,
        "sources_observed": 0,
        "unique_sources": 0,
        "missing_data": True,
        "unknown_state": True,
    }
    asyncio.run(pipeline._persist_narrative(candidate, signal))
    asyncio.run(pipeline._persist_risk_score(candidate))
    asyncio.run(pipeline._persist_candidate(candidate))

    conn = sqlite3.connect(db_path)
    narrative = conn.execute(
        "SELECT narrative_strength, narrative_velocity, social_presence_score, "
        "social_activity_score, duplicate_narrative_count, pop_culture_relevance, "
        "metadata_quality, source_count, confidence FROM narratives"
    ).fetchone()
    risk = conn.execute(
        "SELECT mint_authority_active, freeze_authority_active, top_1_holder_pct, "
        "top_10_holders_pct, metadata_abnormalities FROM risk_scores"
    ).fetchone()
    candidate_row = conn.execute(
        "SELECT risk_score, narrative_score, narrative_confidence, meta_score, meta_approved "
        "FROM candidates WHERE mint = ?",
        (candidate.mint,),
    ).fetchone()
    conn.close()

    assert narrative == (None,) * 9
    assert risk == (None,) * 5
    assert candidate_row == (10.0, None, None, None, None)


def test_failed_paper_entry_does_not_create_zero_position(tmp_path):
    from decimal import Decimal

    db_path = tmp_path / "failed-entry.db"
    _create_db(db_path)
    pipeline = object.__new__(Phase4BPipeline)
    pipeline.db_path = db_path
    pipeline.stats = PipelineStats(start_time=time.time())
    candidate = Candidate(
        mint="no-fill",
        symbol="NO",
        name="No Fill",
        first_discovered=time.time(),
        discovery_source=DiscoverySource.PUMPPORTAL,
    )
    intent = EntryIntent("no-fill", "NO", 0.02, 0.0)
    failed = FillResult(
        success=False,
        mint="no-fill",
        side="buy",
        tokens_filled=Decimal(0),
        avg_price=0.0,
        gross_proceeds=Decimal(0),
        network_costs=Decimal(0),
        net_proceeds=Decimal(0),
        realized_pnl=Decimal(0),
        error="quote unavailable",
    )

    asyncio.run(pipeline._persist_paper_entry(candidate, intent, False, failed))
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0
    conn.close()


def test_meta_marks_unavailable_components_missing_instead_of_zero():
    result = MetaEngine().evaluate(
        discovery_quality=90.0,
        onchain_quality=80.0,
        liquidity_execution=95.0,
        narrative_momentum=None,
        creator_quality=None,
        risk_score=5,
        risk_class="LOW",
        narrative_confidence=0.0,
        enrichment_completeness=0.8,
        quote_quality=0.95,
        creator_confidence=0.0,
        risk_confidence=0.9,
    )

    assert result["component_breakdown"]["narrative_momentum"] is None
    assert result["component_breakdown"]["creator_quality"] is None
    assert result["component_scores"]["narrative_momentum"]["missing"] is True
    assert result["component_scores"]["creator_quality"]["missing"] is True


def test_real_pipeline_methods_reach_one_idempotent_paper_fill_and_outcome(tmp_path):
    now = time.time()
    db_path = tmp_path / "end-to-end.db"
    _create_db(db_path)
    queue = DiscoveryQueue()

    target_event = DiscoveryEvent(
        mint="target-mint",
        source=DiscoverySource.PUMPPORTAL,
        event_timestamp=now - 301,
        receive_timestamp=now - 301,
        symbol="DOG",
        name="Dog Community",
        creator="creator-target",
        uri="https://example.com/dog",
        bonding_curve_key="curve-target",
        pool="pump",
    )
    queue.add_or_update(target_event)
    for index in range(33):
        queue.add_or_update(DiscoveryEvent(
            mint=f"other-{index}",
            source=DiscoverySource.PUMPPORTAL,
            event_timestamp=now,
            receive_timestamp=now,
            symbol=None,
            name=None,
        ))

    price_source = PriceSource()
    state = BondingCurveState(
        mint="target-mint",
        bonding_curve_key="curve-target",
        token_decimals=6,
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=793_000_000_000_000,
        real_sol_reserves=0,
        token_total_supply=1_000_000_000_000_000,
        observed_at=now,
        source="fixture_on_chain",
    )
    price_source.cache_bonding_curve_state(state)
    sell_quote = asyncio.run(price_source.get_sell_quote("target-mint", 1_000.0, 50))

    enrichment = {
        "token_info": {
            "mint": "target-mint",
            "supply": 1_000_000_000_000_000,
            "decimals": 6,
            "mint_authority": None,
            "freeze_authority": None,
        },
        "largest_accounts": [
            {"address": "holder-one", "amount": "50_000_000_000_000"}
        ],
        "holder_analysis": {
            "holder_classification_complete": True,
            "top_1_non_protocol_pct_total_supply": 5.0,
            "top_5_non_protocol_pct_total_supply": 15.0,
            "top_10_non_protocol_pct_total_supply": 25.0,
        },
        "creator_signatures": None,
        "creator_accounts": None,
        "sell_quote": {
            "executable_price": sell_quote.executable_price,
            "price_impact_pct": sell_quote.price_impact_pct,
            "out_amount": sell_quote.out_amount,
            "timestamp": sell_quote.timestamp,
            "route": sell_quote.route,
        },
        "enrichment_credits": 0,
        "enriched_at": now,
    }

    class FixtureEnrichment:
        async def enrich_batch(self, candidates):
            return {candidate.mint: enrichment for candidate in candidates}

    class OutcomePriceSource:
        async def get_price(self, mint, **kwargs):
            return await price_source.get_price(mint, **kwargs)

        async def get_sell_quote(self, mint, token_amount_human, slippage_bps=50):
            return await price_source.get_sell_quote(mint, token_amount_human, slippage_bps)

    pipeline = object.__new__(Phase4BPipeline)
    pipeline.db_path = db_path
    pipeline.config = type("Config", (), {"position_size_sol": 0.02})()
    pipeline.stats = PipelineStats(start_time=now)
    pipeline.discovery_queue = queue
    pipeline.discovery_manager = None
    pipeline.pre_scorer = PreScorer(target_enrichment_pct=0.03)
    pipeline.enrichment_pipeline = FixtureEnrichment()
    pipeline.rug_detector = RugDetector()
    pipeline.narrative_engine = NarrativeEngine()
    pipeline.meta_engine = MetaEngine()
    pipeline.paper_adapter = PaperExecutionAdapter(price_source=price_source)
    pipeline.helius_client = OutcomePriceSource()

    async def traverse():
        await pipeline._process_discovery_queue()
        await pipeline._process_enrichment()
        await pipeline._process_risk_analysis()
        await pipeline._process_narrative_analysis(pipeline.narrative_engine)
        await pipeline._process_meta_decisions()
        await pipeline._process_paper_entries()
        await pipeline._process_paper_entries()
        await pipeline._schedule_outcome_samples()

    asyncio.run(traverse())
    target = queue.get_candidate("target-mint")
    assert pipeline.stats.candidates_passed == 1
    assert target.risk_class == "LOW"
    assert target.narrative_evaluated is True
    assert target.meta_approved is True
    assert target.paper_entered is True
    assert pipeline.stats.paper_entries == 1

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1
    lifecycle = conn.execute(
        "SELECT risk_class, narrative_category, meta_approved, paper_entered "
        "FROM candidates WHERE mint = 'target-mint'"
    ).fetchone()
    assert lifecycle == ("LOW", "animal_meme", 1, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM outcome_snapshots WHERE candidate_mint = 'target-mint'"
    ).fetchone()[0] == 2
    conn.close()
