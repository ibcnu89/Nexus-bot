import asyncio
import base64
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from research.phase4.phase4b.discovery.models import Candidate, DiscoveryQueue, DiscoverySource
from research.phase4.phase4b.enrichment.helius import HeliusClient
from research.phase4.phase4b.execution.paper_adapter import EntryIntent, PaperExecutionAdapter
from research.phase4.phase4b.monitoring.outcome_sampler import OutcomePersister, OutcomeSnapshot
from research.phase4.phase4b.run_pipeline import Phase4BPipeline
from research.phase4.phase4b.scoring.pre_score import PreScorer
from research.phase4.phase4b.storage.database import SCHEMA_SQL
from research.phase4.prototype.price_source import (
    BondingCurveState,
    LAMPORTS_PER_SOL,
    PriceSource,
    SOL_MINT,
    TokenDecimalsError,
)


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


def test_jupiter_v2_quote_only_schema_is_mapped_without_a_taker():
    class FixturePriceSource(PriceSource):
        def __init__(self):
            super().__init__()
            self.jupiter_api_key = "fixture"
            self.params = None

        async def _fetch_with_retry(self, url, params):
            self.params = params
            return {
                "inputMint": SOL_MINT,
                "outputMint": "mint",
                "inAmount": "20000000",
                "outAmount": "700000000000",
                "otherAmountThreshold": "696500000000",
                "slippageBps": 50,
                "priceImpact": 0.12,
                "feeBps": 50,
                "platformFee": {"amount": "1000000", "feeBps": 50, "feeMint": "mint"},
                "router": "metis",
                "routePlan": [{"swapInfo": {"label": "Pumpfun AMM"}}],
                "transaction": None,
            }

    source = FixturePriceSource()
    source.cache_token_decimals("mint", 6)
    quote = asyncio.run(source.get_buy_quote("mint", 0.02, slippage_bps=50))

    assert quote is not None
    assert source.params == {
        "inputMint": SOL_MINT,
        "outputMint": "mint",
        "amount": "20000000",
        "slippageBps": "50",
    }
    assert "taker" not in source.params
    assert quote.route == "Jupiter/metis:Pumpfun AMM"
    assert quote.swap_fee_bps == quote.platform_fee_bps == 50
    assert quote.price_impact_pct == 0.12


def test_pre_score_selects_top_three_percent():
    scorer = PreScorer(target_enrichment_pct=0.03)
    candidates = [_candidate(i, i / 100) for i in range(100)]
    selected = scorer.select_for_enrichment(candidates)
    assert selected == {"mint-97", "mint-98", "mint-99"}


def test_pre_score_small_cohort_does_not_round_up():
    scorer = PreScorer(target_enrichment_pct=0.03)
    candidates = [_candidate(i, i / 100) for i in range(33)]
    assert scorer.enrichment_quota(candidates) == 0
    assert scorer.select_for_enrichment(candidates) == set()


def test_pre_score_streaming_admissions_never_exceed_cumulative_quota():
    scorer = PreScorer(target_enrichment_pct=0.03)
    candidates = [_candidate(i, i / 100) for i in range(100)]

    first = scorer.select_for_enrichment(candidates)
    assert len(first) == 3
    for candidate in candidates:
        if candidate.mint in first:
            candidate.pre_score_passed = True
            candidate.enrichment_attempts = 1
            candidate.enrichment_status = "completed"

    candidates.extend(_candidate(i, 10 + i) for i in range(100, 110))
    assert scorer.enrichment_quota(candidates) == 3
    assert scorer.select_for_enrichment(candidates) == first

    candidates.extend(_candidate(i, 10 + i) for i in range(110, 134))
    fourth = scorer.select_for_enrichment(candidates)
    assert scorer.enrichment_quota(candidates) == 4
    assert len(fourth) == 4
    assert first.issubset(fourth)


def _curve_state(*, complete: bool = False) -> BondingCurveState:
    return BondingCurveState(
        mint="curve-mint",
        bonding_curve_key="curve-account",
        token_decimals=6,
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30 * LAMPORTS_PER_SOL,
        real_token_reserves=793_100_000_000_000,
        real_sol_reserves=0,
        token_total_supply=1_000_000_000_000_000,
        complete=complete,
        observed_at=time.time(),
        source="fixture",
    )


def test_bonding_curve_quotes_use_observed_reserves_and_fee_once():
    source = PriceSource()
    state = _curve_state()
    source.cache_bonding_curve_state(state)

    sell = asyncio.run(source.get_sell_quote(state.mint, 1_000.0, slippage_bps=50))
    buy = asyncio.run(source.get_buy_quote(state.mint, 0.02, slippage_bps=50))

    assert sell is not None and buy is not None
    assert sell.route == buy.route == "PumpFunBondingCurve"
    assert sell.in_amount == 1_000_000_000
    gross_sell = (
        sell.in_amount * state.virtual_sol_reserves
        // (state.virtual_token_reserves + sell.in_amount)
    )
    assert sell.out_amount == gross_sell - (gross_sell * 125 // 10_000)
    assert sell.out_amount > sell.other_amount_threshold > 0
    assert sell.swap_fee_bps == buy.swap_fee_bps == 125
    assert sell.venue_state_source == buy.venue_state_source == "fixture"
    assert buy.in_amount == 20_000_000
    effective_buy = buy.in_amount - (buy.in_amount * 125 // 10_000)
    expected_tokens = (
        effective_buy * state.virtual_token_reserves
        // (state.virtual_sol_reserves + effective_buy)
    )
    assert buy.out_amount == expected_tokens
    assert buy.out_amount > buy.other_amount_threshold > 0
    assert buy.in_mint == SOL_MINT and sell.out_mint == SOL_MINT
    assert buy.executable_price > buy.price_sol_per_token
    assert sell.executable_price < sell.price_sol_per_token


def test_completed_curve_routes_to_jupiter():
    class RecordingPriceSource(PriceSource):
        def __init__(self):
            super().__init__()
            self.jupiter_calls = []

        async def get_jupiter_quote(self, input_mint, output_mint, amount, slippage_bps=50):
            self.jupiter_calls.append((input_mint, output_mint, amount, slippage_bps))
            return "jupiter-quote"

    source = RecordingPriceSource()
    source.cache_bonding_curve_state(_curve_state(complete=True))
    quote = asyncio.run(source.get_sell_quote("curve-mint", 2.5))
    assert quote == "jupiter-quote"
    assert source.jupiter_calls == [("curve-mint", SOL_MINT, 2_500_000, 50)]


def test_stale_active_curve_fails_closed_instead_of_using_jupiter():
    class RecordingPriceSource(PriceSource):
        def __init__(self):
            super().__init__()
            self.jupiter_calls = 0

        async def get_jupiter_quote(self, *args, **kwargs):
            self.jupiter_calls += 1
            return "must-not-route"

    fresh = _curve_state()
    state = BondingCurveState(**{**fresh.__dict__, "observed_at": time.time() - 60})
    source = RecordingPriceSource()
    source.cache_bonding_curve_state(state)
    assert asyncio.run(source.get_sell_quote(state.mint, 1_000.0)) is None
    assert source.jupiter_calls == 0


def test_migrated_price_estimate_uses_sol_quote_not_usd_price():
    class QuotingPriceSource(PriceSource):
        def __init__(self):
            super().__init__()
            self.amounts = []

        async def get_jupiter_quote(self, input_mint, output_mint, amount, slippage_bps=50):
            self.amounts.append(amount)
            token_amount = amount / 1_000_000
            sol_out = token_amount * 0.0001
            return self._quote(amount, int(sol_out * LAMPORTS_PER_SOL), input_mint)

        @staticmethod
        def _quote(in_amount, out_amount, mint):
            from research.phase4.prototype.price_source import PriceQuote

            return PriceQuote(
                price_sol_per_token=0.0001,
                in_amount=in_amount,
                out_amount=out_amount,
                other_amount_threshold=out_amount,
                in_mint=mint,
                out_mint=SOL_MINT,
                in_decimals=6,
                out_decimals=9,
                route="JupiterFixture",
                price_impact_pct=0.0,
            )

    source = QuotingPriceSource()
    source.cache_token_decimals("migrated", 6)
    quote = asyncio.run(source.get_price("migrated", side="sell", size_sol=0.02))
    assert quote is not None
    assert source.amounts == [1_000_000_000, 200_000_000]


def test_helius_parses_bonding_curve_account_prefix():
    fields = [
        1_073_000_000_000_000,
        30 * LAMPORTS_PER_SOL,
        793_100_000_000_000,
        123_456_789,
        1_000_000_000_000_000,
    ]
    raw = b"ANCHOR00" + b"".join(value.to_bytes(8, "little") for value in fields) + b"\x00"
    account = {"data": [base64.b64encode(raw).decode(), "base64"]}

    state = HeliusClient.parse_bonding_curve_account(
        "curve-mint", "curve-account", 6, account, observed_at=321.0
    )
    assert state.virtual_token_reserves == fields[0]
    assert state.virtual_sol_reserves == fields[1]
    assert state.complete is False
    assert state.observed_at == 321.0
    assert state.source == "helius_getAccountInfo"


def test_enrichment_uses_fresh_curve_state_for_risk_sell_quote():
    fields = [
        1_073_000_000_000_000,
        30 * LAMPORTS_PER_SOL,
        793_100_000_000_000,
        123_456_789,
        1_000_000_000_000_000,
    ]
    raw = b"ANCHOR00" + b"".join(value.to_bytes(8, "little") for value in fields) + b"\x00"
    account_result = {
        "value": {"data": [base64.b64encode(raw).decode(), "base64"]}
    }

    class FixtureHelius(HeliusClient):
        async def get_token_info(self, mint):
            return {
                "mint": mint,
                "supply": fields[4],
                "decimals": 6,
                "mint_authority": None,
                "freeze_authority": None,
                "is_initialized": True,
            }

        async def get_token_largest_accounts(self, mint, limit=20):
            return [{"amount": "100"}, {"amount": "50"}]

        async def _rpc_call(self, method, params):
            assert method == "getAccountInfo"
            assert params[0] == "curve-account"
            self.governor.record_usage(method)
            return account_result

    candidate = _candidate(1, 0.9)
    candidate.discovery_event = {
        "pool": "pump",
        "bonding_curve_key": "curve-account",
        "v_tokens_in_bonding_curve": 1_073_000_000.0,
        "v_sol_in_bonding_curve": 30.0,
        "receive_timestamp": time.time(),
    }
    client = FixtureHelius(api_key="fixture")
    enrichment = asyncio.run(client.enrich_candidate(candidate))

    assert enrichment is not None
    assert enrichment["sell_quote"]["route"] == "PumpFunBondingCurve"
    assert enrichment["sell_quote"]["venue_state_source"] == "helius_getAccountInfo"
    assert enrichment["bonding_curve_state"]["complete"] is False
    assert enrichment["enrichment_credits"] == 1


def test_curve_quote_reaches_paper_entry_without_transaction_broadcast():
    source = PriceSource()
    state = _curve_state()
    source.cache_bonding_curve_state(state)
    adapter = PaperExecutionAdapter(price_source=source)
    intent = EntryIntent(
        mint=state.mint,
        symbol="CURVE",
        size_sol=0.02,
        entry_price_hint=0.0,
    )

    fill = asyncio.run(adapter.execute_entry(intent))

    assert fill.success is True
    assert fill.route_provider == "PumpFunBondingCurve"
    assert fill.tokens_received > 0
    assert fill.sol_spent == 0.02
    assert fill.total_cost_basis_sol == 0.0205
    assert state.mint in adapter.positions


def test_fill_result_persistence_keeps_runtime_quote_values(tmp_path):
    db_path = tmp_path / "fills.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.close()

    source = PriceSource()
    state = _curve_state()
    source.cache_bonding_curve_state(state)
    adapter = PaperExecutionAdapter(price_source=source)
    intent = EntryIntent(state.mint, "CURVE", 0.02, 0.0)
    fill = asyncio.run(adapter.execute_entry(intent))
    candidate = _candidate(1, 0.9)
    candidate.mint = state.mint
    candidate.symbol = "CURVE"
    candidate.paper_entry_time = fill.quote_timestamp + 0.01

    pipeline = object.__new__(Phase4BPipeline)
    pipeline.db_path = Path(db_path)
    pipeline.stats = SimpleNamespace(database_errors=0)
    asyncio.run(pipeline._persist_paper_entry(candidate, intent, True, fill))

    conn = sqlite3.connect(db_path)
    position = conn.execute(
        "SELECT entry_price, size_sol, initial_tokens, entry_cost_basis_sol, "
        "entry_fees_sol FROM paper_positions"
    ).fetchone()
    persisted_fill = conn.execute(
        "SELECT avg_price, gross_proceeds_sol, network_costs_sol, "
        "quote_slippage_bps, quote_route, quote_swap_fee_bps, priority_fee_sol "
        "FROM paper_fills"
    ).fetchone()
    conn.close()

    assert position == (
        fill.fill_price_sol,
        fill.sol_spent,
        float(fill.tokens_received),
        fill.total_cost_basis_sol,
        fill.priority_fee_sol,
    )
    assert persisted_fill == (
        fill.avg_price,
        float(fill.gross_proceeds),
        float(fill.network_costs),
        fill.slippage_bps,
        fill.route_provider,
        fill.swap_fee_bps,
        fill.priority_fee_sol,
    )
    assert pipeline.stats.database_errors == 0


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
