"""
Database Schema - SQLite schema for Phase 4B experimental data capture.

Creates tables for complete experimental history preservation.
"""

SCHEMA_SQL = """
-- Discovery events from all sources
CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    source TEXT NOT NULL,
    event_timestamp REAL NOT NULL,
    receive_timestamp REAL NOT NULL,
    symbol TEXT,
    name TEXT,
    creator TEXT,
    uri TEXT,
    bonding_curve_key TEXT,
    initial_buy REAL,
    sol_amount REAL,
    v_tokens_in_bonding_curve REAL,
    v_sol_in_bonding_curve REAL,
    market_cap_sol REAL,
    is_mayhem_mode INTEGER,
    pool TEXT,
    signature TEXT,
    raw_payload TEXT,
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_discovery_mint ON discovery_events(mint);
CREATE INDEX IF NOT EXISTS idx_discovery_receive_ts ON discovery_events(receive_timestamp);
CREATE INDEX IF NOT EXISTS idx_discovery_source ON discovery_events(source);

-- Candidates (deduplicated, pre-scored)
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL UNIQUE,
    symbol TEXT,
    name TEXT,
    first_discovered REAL NOT NULL,
    discovery_source TEXT,
    duplicate_count INTEGER DEFAULT 1,
    last_seen REAL,
    pre_score REAL,
    pre_score_components TEXT,  -- JSON
    pre_score_reason TEXT,
    pre_score_passed INTEGER,
    pre_score_timestamp REAL,
    enrichment_status TEXT DEFAULT 'pending',
    enrichment_attempts INTEGER DEFAULT 0,
    last_enrichment REAL,
    risk_score REAL,
    risk_class TEXT,
    risk_reasons TEXT,  -- JSON array
    narrative_score REAL,
    narrative_category TEXT,
    narrative_confidence REAL,
    meta_score REAL,
    meta_confidence REAL,
    meta_approved INTEGER DEFAULT 0,
    rejection_reason TEXT,
    paper_entered INTEGER DEFAULT 0,
    paper_entry_time REAL,
    created_at REAL DEFAULT (strftime('%s', 'now')),
    updated_at REAL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_candidates_pre_score ON candidates(pre_score);
CREATE INDEX IF NOT EXISTS idx_candidates_enrichment ON candidates(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_candidates_risk ON candidates(risk_class);
CREATE INDEX IF NOT EXISTS idx_candidates_meta ON candidates(meta_approved);

-- Enrichment snapshots
CREATE TABLE IF NOT EXISTS enrichments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    candidate_id INTEGER,
    enriched_at REAL NOT NULL,
    enrichment_credits INTEGER,
    token_info TEXT,  -- JSON
    largest_accounts TEXT,  -- JSON
    holder_analysis TEXT,  -- JSON
    creator_signatures TEXT,  -- JSON
    creator_accounts TEXT,  -- JSON
    raw_enrichment TEXT,  -- JSON
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_enrichments_mint ON enrichments(mint);

-- Risk scores
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    candidate_id INTEGER,
    risk_score REAL,
    risk_class TEXT,
    risk_reasons TEXT,  -- JSON array
    mint_authority_active INTEGER,
    freeze_authority_active INTEGER,
    creator_holdings_pct REAL,
    top_1_holder_pct REAL,
    top_10_holders_pct REAL,
    initial_liquidity_sol REAL,
    current_liquidity_sol REAL,
    liquidity_change_pct REAL,
    creator_rapid_launches INTEGER,
    metadata_abnormalities TEXT,  -- JSON
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_risk_mint ON risk_scores(mint);
CREATE INDEX IF NOT EXISTS idx_risk_class ON risk_scores(risk_class);

-- Narrative snapshots
CREATE TABLE IF NOT EXISTS narratives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    candidate_id INTEGER,
    snapshot_at REAL NOT NULL,
    narrative_category TEXT,
    narrative_strength REAL,
    narrative_velocity REAL,
    social_presence_score REAL,
    social_activity_score REAL,
    duplicate_narrative_count INTEGER,
    trend_alignment TEXT,
    pop_culture_relevance REAL,
    metadata_quality REAL,
    source_count INTEGER,
    confidence REAL,
    raw_sources TEXT,  -- JSON
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_narratives_mint ON narratives(mint);
CREATE INDEX IF NOT EXISTS idx_narratives_category ON narratives(narrative_category);

-- Meta decisions
CREATE TABLE IF NOT EXISTS meta_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    candidate_id INTEGER,
    decided_at REAL NOT NULL,
    discovery_score REAL,
    onchain_score REAL,
    liquidity_score REAL,
    narrative_score REAL,
    creator_score REAL,
    risk_penalty REAL,
    meta_score REAL,
    confidence REAL,
    expected_return_estimate REAL,
    expected_downside REAL,
    estimated_execution_cost REAL,
    estimated_slippage REAL,
    risk_adjusted_ev REAL,
    recommended_size_sol REAL,
    entry_reason TEXT,
    rejection_reason TEXT,
    decision TEXT,  -- 'approve', 'reject', 'defer'
    component_scores TEXT,  -- JSON
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_meta_mint ON meta_decisions(mint);
CREATE INDEX IF NOT EXISTS idx_meta_decision ON meta_decisions(decision);

-- Paper positions
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT,
    candidate_id INTEGER,
    entry_price REAL NOT NULL,
    entry_time REAL NOT NULL,
    size_sol REAL NOT NULL,
    initial_tokens REAL NOT NULL,
    tokens_remaining REAL NOT NULL,
    entry_cost_basis_sol REAL NOT NULL,
    entry_fees_sol REAL,
    state TEXT DEFAULT 'OPEN',
    created_at REAL DEFAULT (strftime('%s', 'now')),
    closed_at REAL,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_positions_mint ON paper_positions(mint);
CREATE INDEX IF NOT EXISTS idx_positions_state ON paper_positions(state);

-- Paper fills (entries and exits)
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,  -- 'buy' or 'sell'
    fill_time REAL NOT NULL,
    tokens_filled REAL NOT NULL,
    avg_price REAL NOT NULL,
    gross_proceeds_sol REAL NOT NULL,
    network_costs_sol REAL NOT NULL,
    net_proceeds_sol REAL NOT NULL,
    realized_pnl_sol REAL NOT NULL,
    trigger TEXT,
    quote_timestamp REAL,
    quote_age_seconds REAL,
    quote_price_impact_pct REAL,
    quote_slippage_bps INTEGER,
    quote_route TEXT,
    quote_swap_fee_bps INTEGER,
    quote_platform_fee_bps INTEGER,
    priority_fee_sol REAL,
    tokens_remaining REAL,
    realized_pnl_cumulative REAL,
    FOREIGN KEY (position_id) REFERENCES paper_positions(id)
);

CREATE INDEX IF NOT EXISTS idx_fills_position ON paper_fills(position_id);
CREATE INDEX IF NOT EXISTS idx_fills_mint ON paper_fills(mint);
CREATE INDEX IF NOT EXISTS idx_fills_time ON paper_fills(fill_time);

-- Market snapshots (for outcome sampling)
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    price REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    volume_24h_usd REAL,
    buy_volume REAL,
    sell_volume REAL,
    source TEXT,
    FOREIGN KEY (mint) REFERENCES discovery_events(mint)
);

CREATE INDEX IF NOT EXISTS idx_market_mint_time ON market_snapshots(mint, snapshot_time);

-- Outcome snapshots (for leakage-free outcome labeling)
CREATE TABLE IF NOT EXISTS outcome_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_mint TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    observation_time REAL NOT NULL,
    price_sol REAL,
    executable_buy_price REAL,
    executable_sell_price REAL,
    liquidity_usd REAL,
    price_change_from_decision REAL,
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    peak_price REAL,
    peak_return_pct REAL,
    trough_price REAL,
    trough_return_pct REAL,
    time_to_peak REAL,
    time_to_trough REAL,
    liquidity_change_pct REAL,
    tradable INTEGER,
    sellable INTEGER,
    rug_detected INTEGER,
    liquidity_disappeared INTEGER,
    freeze_authority_activated INTEGER,
    missing_data_reason TEXT,
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (candidate_mint) REFERENCES candidates(mint)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_mint ON outcome_snapshots(candidate_mint);
CREATE INDEX IF NOT EXISTS idx_outcomes_horizon ON outcome_snapshots(horizon_seconds);
CREATE INDEX IF NOT EXISTS idx_outcomes_obs_time ON outcome_snapshots(observation_time);

-- Provider usage
CREATE TABLE IF NOT EXISTS provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    provider TEXT NOT NULL,
    method TEXT,
    credits_used INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1,
    error_message TEXT,
    latency_ms REAL
);

CREATE INDEX IF NOT EXISTS idx_provider_time ON provider_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_provider_name ON provider_usage(provider);

-- System events
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,  -- INFO, WARNING, ERROR, CRITICAL
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_time ON system_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_system_level ON system_events(level);
"""
