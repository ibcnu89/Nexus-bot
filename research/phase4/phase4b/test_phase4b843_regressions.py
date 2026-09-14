"""Regression tests for Phase 4B.8.4.3 provider-budget controls."""

import asyncio
import time
from types import SimpleNamespace

from research.phase4.phase4b.enrichment.helius import CreditGovernor, HeliusClient
from research.phase4.phase4b.run_pipeline import (
    Phase4BPipeline,
    PipelineConfig,
    PipelineStats,
)
from research.phase4.prototype.paper_position_manager import ExitTrigger


class _Quote:
    executable_price = 1.0


class _PositionPriceClient:
    def __init__(self):
        self.calls = []

    async def get_price(self, mint, **kwargs):
        self.calls.append((mint, kwargs))
        return _Quote()


class _PositionAdapter:
    def __init__(self, count=1, triggers=None):
        self.positions = {f"mint-{index}": object() for index in range(count)}
        self.triggers = triggers or {}
        self.updates = []

    def update_positions(self, market_data):
        self.updates.append(market_data)
        return self.triggers


def _monitor_pipeline(position_count=1, triggers=None):
    pipeline = object.__new__(Phase4BPipeline)
    pipeline.config = SimpleNamespace(position_update_interval_seconds=60.0)
    pipeline.stats = PipelineStats(start_time=time.time())
    pipeline.paper_adapter = _PositionAdapter(position_count, triggers)
    pipeline.helius_client = _PositionPriceClient()
    pipeline._last_position_update_monotonic = None
    return pipeline


def test_position_monitor_default_matches_shortest_outcome_horizon():
    assert PipelineConfig().position_update_interval_seconds == 60.0


def test_one_second_processing_loop_honors_sixty_second_monitor_cadence():
    pipeline = _monitor_pipeline(position_count=10)

    async def simulate_ten_minutes():
        for second in range(600):
            await pipeline._update_paper_positions(now_monotonic=float(second))

    asyncio.run(simulate_ten_minutes())

    assert len(pipeline.helius_client.calls) == 100
    assert pipeline.stats.position_monitor_cycles == 10
    assert pipeline.stats.position_monitor_quote_attempts == 100
    assert pipeline.stats.position_monitor_quotes_received == 100
    assert pipeline.stats.position_monitor_peak_open_positions == 10
    assert pipeline.stats.position_monitor_cycles_skipped_by_cadence == 590


def test_monitor_deadline_resets_when_portfolio_becomes_empty():
    pipeline = _monitor_pipeline()

    async def exercise_reset():
        await pipeline._update_paper_positions(now_monotonic=0.0)
        pipeline.paper_adapter.positions.clear()
        await pipeline._update_paper_positions(now_monotonic=10.0)
        pipeline.paper_adapter.positions["new-mint"] = object()
        await pipeline._update_paper_positions(now_monotonic=11.0)

    asyncio.run(exercise_reset())

    assert [call[0] for call in pipeline.helius_client.calls] == ["mint-0", "new-mint"]
    assert pipeline.stats.position_monitor_cycles == 2


def test_provider_failure_does_not_bypass_next_monitor_deadline():
    pipeline = _monitor_pipeline()

    async def failing_fetch():
        raise RuntimeError("provider unavailable")

    pipeline._fetch_market_data = failing_fetch

    async def exercise_failure():
        try:
            await pipeline._update_paper_positions(now_monotonic=0.0)
        except RuntimeError:
            pass
        await pipeline._update_paper_positions(now_monotonic=1.0)

    asyncio.run(exercise_failure())

    assert pipeline.stats.position_monitor_cycles == 1
    assert pipeline.stats.position_monitor_cycles_skipped_by_cadence == 1


def test_exit_trigger_enums_increment_truthful_exit_counters():
    final_pipeline = _monitor_pipeline(
        triggers={"mint-0": [ExitTrigger.INITIAL_STOP]},
    )
    partial_pipeline = _monitor_pipeline(
        triggers={"mint-0": [ExitTrigger.TAKE_PROFIT]},
    )

    asyncio.run(final_pipeline._update_paper_positions(now_monotonic=0.0))
    asyncio.run(partial_pipeline._update_paper_positions(now_monotonic=0.0))

    assert final_pipeline.stats.paper_exits == 1
    assert final_pipeline.stats.final_exits == 1
    assert final_pipeline.stats.partial_exits == 0
    assert partial_pipeline.stats.paper_exits == 0
    assert partial_pipeline.stats.final_exits == 0
    assert partial_pipeline.stats.partial_exits == 1


def test_governor_projects_from_measured_session_not_calendar_cycle():
    governor = CreditGovernor(
        monthly_credit_limit=1_000_000,
        safety_ceiling_pct=70.0,
        projection_min_observation_seconds=300.0,
    )
    governor._session_started_at = time.time() - 600.0
    governor.record_usage("getAccountInfo", count=100)

    state = governor.get_state()

    assert state["projection_ready"] is True
    assert state["projection_basis"] == "process_session_run_rate_30_days"
    assert 431_000 <= state["session_projected_monthly_credits"] <= 432_000
    assert state["state"] == "NORMAL"
    assert state["account_cycle_usage_complete"] is False
    assert state["account_cycle_state"] == "UNKNOWN"


def test_governor_stops_an_unsafe_sustained_session_rate():
    governor = CreditGovernor(
        monthly_credit_limit=1_000_000,
        safety_ceiling_pct=70.0,
        projection_min_observation_seconds=300.0,
    )
    governor._session_started_at = time.time() - 600.0
    governor.record_usage("getAccountInfo", count=200)

    state = governor.get_state()
    allowed, reason = governor.can_proceed()

    assert state["session_projected_monthly_credits"] > 700_000
    assert state["state"] == "HARD_STOP"
    assert allowed is False
    assert "HARD_STOP" in reason


def test_helius_metrics_attribute_attempts_and_credits_by_purpose():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"result": {"value": "ok"}}

    class Session:
        closed = False

        def post(self, *args, **kwargs):
            return Response()

    client = HeliusClient(api_key="fixture")
    client._session = Session()

    async def call_with_purpose():
        with client.request_purpose("position_monitor"):
            return await client._rpc_call("getAccountInfo", ["mint"])

    assert asyncio.run(call_with_purpose()) == {"value": "ok"}
    metrics = client.get_metrics()

    assert metrics["requests_by_purpose"] == {"position_monitor": 1}
    assert metrics["successful_requests_by_purpose"] == {"position_monitor": 1}
    assert metrics["credits_by_purpose"] == {"position_monitor": 1}


def test_pipeline_report_exposes_monitor_and_budget_truth():
    stats = PipelineStats(start_time=time.time())
    stats.position_monitor_cycles = 2
    stats.position_monitor_quote_attempts = 4
    stats.position_monitor_quotes_received = 3
    stats.helius_requests_by_purpose = {"position_monitor": 4, "enrichment": 8}
    stats.helius_credits_by_purpose = {"position_monitor": 4, "enrichment": 8}
    stats.helius_session_projected_daily_credits = 1_728
    stats.helius_session_projected_monthly_credits = 51_840
    stats.helius_projection_ready = True

    report = stats.to_dict()

    assert report["execution"]["position_monitor"]["interval_seconds"] == 60.0
    assert report["execution"]["position_monitor"]["quote_attempts"] == 4
    assert report["provider_health"]["helius_requests_by_purpose"] == {
        "position_monitor": 4,
        "enrichment": 8,
    }
    assert report["provider_health"]["helius_session_projected_monthly_credits"] == 51_840
    assert report["provider_health"]["helius_account_cycle_state"] == "UNKNOWN"
