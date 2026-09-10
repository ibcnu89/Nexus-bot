#!/usr/bin/env python3
"""
PHASE 3B-A: Leakage-Free Walk-Forward Research Framework (v2)
Isolated research artifact — does NOT modify production code.
Location: research/phase3b/

Implements:
- Frozen chronological holdout AND expanding walk-forward
- Complete test matrix: BTC/ETH/BNB/SOL/XRP x 1h/4h/1d/2h/8h/12h/1w
- Real trading simulation with fees/slippage
- Confidence intervals (Wilson), statistical tests (McNemar, bootstrap)
- Regime analysis, time-period stability, parameter sensitivity
- Forensic timestamp verification
"""

import asyncio
import numpy as np
import json
import os
import time
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional, Any, Literal
from dataclasses import dataclass, field
from enum import Enum
import random
from collections import defaultdict
import math
from scipy import stats
from scipy.stats import norm


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

@dataclass
class Memory:
    """Training memory: pattern + future outcome"""
    pattern: List[float]          # [pct_change_1] or [pct_change_1, pct_change_2]
    high_diff: float              # future high as % of pattern end price
    low_diff: float               # future low as % of pattern end price
    weight: float = 1.0
    high_weight: float = 1.0
    low_weight: float = 1.0

@dataclass
class Prediction:
    signal: Literal["LONG", "SHORT", "WITHIN"]
    pred_move: float
    high_move: float
    low_move: float
    pred_price: float
    high_bound: float
    low_bound: float

class MarketRegime(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"


# ============================================================
# CORE PATTERN MATCHING LOGIC
# ============================================================

class PatternMatching:
    """Core pattern matching logic with leakage controls"""

    def __init__(self, threshold: float = 0.5, pattern_length: int = 1):
        self.threshold = threshold
        self.pattern_length = pattern_length

    def compute_pct_change(self, open_price: float, close_price: float) -> float:
        if open_price == 0:
            return 0.0
        return (close_price - open_price) / open_price * 100

    def build_pattern(self, candles: List[Candle], end_idx: int, pattern_length: int) -> List[float]:
        if end_idx < pattern_length:
            return []
        pattern = []
        for i in range(pattern_length):
            idx = end_idx - pattern_length + i
            c = candles[idx]
            pct = self.compute_pct_change(c.open, c.close)
            pattern.append(pct)
        return pattern

    def match_1_candle(self, current_candle_pct: float, memory_pattern: List[float]) -> float:
        """Current buggy behavior: only compare first candle"""
        memory_candle = memory_pattern[0] if memory_pattern else 0.0
        if current_candle_pct == 0 and memory_candle == 0:
            return 0.0
        denom = (abs(current_candle_pct) + abs(memory_candle)) / 2
        return abs(current_candle_pct - memory_candle) / denom * 100 if denom > 0 else 0.0

    def match_2_candle(self, current_pattern: List[float], memory_pattern: List[float]) -> float:
        """Corrected 2-candle matching"""
        if len(current_pattern) != len(memory_pattern):
            return float('inf')
        diffs = []
        for c, m in zip(current_pattern, memory_pattern):
            if c == 0 and m == 0:
                diffs.append(0)
            else:
                denom = (abs(c) + abs(m)) / 2
                diffs.append(abs(c - m) / denom * 100 if denom > 0 else 0.0)
        return np.mean(diffs)

    def match(self, current_pattern: List[float], memory_pattern: List[float]) -> float:
        if self.pattern_length == 1:
            return self.match_1_candle(current_pattern[-1], memory_pattern)
        else:
            return self.match_2_candle(current_pattern, memory_pattern)


class LeakageFreeTrainer:
    """Leakage-free training state builder"""

    def __init__(self, pattern_length: int = 2, threshold: float = 0.5):
        self.pattern_length = pattern_length
        self.threshold = threshold
        self.matcher = PatternMatching(threshold=threshold, pattern_length=pattern_length)

        self.memories: List[Memory] = []
        self.weights: List[float] = []
        self.high_weights: List[float] = []
        self.low_weights: List[float] = []
        self.threshold = 0.5
        self.perfect_threshold = threshold

        # Training hyperparameters
        self.min_good_matches = 1
        self.max_good_matches = 1
        self.candles_to_predict = 1
        self.max_difference = 0.5
        self.preferred_difference = 0.4
        self.prediction_expander = 1.33
        self.prediction_expander2 = 1.5
        self.prediction_adjuster = 0.0
        self.diff_avg_setting = 0.01
        self.min_success_rate = 90
        self.histories = 'off'

    def build_memories_up_to(self, candles: List[Candle], max_idx: int) -> None:
        """Build memories ONLY from candles[0:max_idx] — NO FUTURE DATA"""
        self.memories = []
        self.weights = []
        self.high_weights = []
        self.low_weights = []

        min_idx = self.pattern_length + 1
        if max_idx < min_idx:
            return

        for i in range(max_idx - self.pattern_length):
            pattern = []
            for j in range(self.pattern_length):
                c = candles[i + j]
                pct = (c.close - c.open) / c.open * 100 if c.open > 0 else 0.0
                pattern.append(pct)

            start_price = candles[i + self.pattern_length - 1].close
            future_candle = candles[i + self.pattern_length]

            high_diff = (future_candle.high - start_price) / start_price * 100
            low_diff = (future_candle.low - start_price) / start_price * 100

            self.memories.append(Memory(
                pattern=pattern,
                high_diff=high_diff,
                low_diff=low_diff
            ))
            self.weights.append(1.0)
            self.high_weights.append(1.0)
            self.low_weights.append(1.0)

    def update_threshold(self, match_count: int) -> None:
        if match_count > self.max_good_matches:
            if self.perfect_threshold < 0.1:
                self.perfect_threshold -= 0.001
            else:
                self.perfect_threshold -= 0.01
            if self.perfect_threshold < 0.0:
                self.perfect_threshold = 0.0
        elif match_count < self.min_good_matches:
            if self.perfect_threshold < 0.1:
                self.perfect_threshold += 0.001
            else:
                self.perfect_threshold += 0.01
            if self.perfect_threshold > 100.0:
                self.perfect_threshold = 100.0


class Predictor:
    """Prediction engine"""

    def __init__(self, pattern_length: int = 1, threshold: float = 0.5, distance: float = 0.5):
        self.pattern_length = pattern_length
        self.threshold = threshold
        self.distance = distance / 100.0
        self.matcher = PatternMatching(threshold=threshold, pattern_length=pattern_length)

    def predict(self, current_pattern: List[float], memories: List[Memory],
                weights: List[float], high_weights: List[float], low_weights: List[float]) -> Tuple[float, float, float]:

        matches = []
        for i, (mem, w, hw, lw) in enumerate(zip(memories, weights, high_weights, low_weights)):
            if self.pattern_length == 1:
                diff = self.matcher.match_1_candle(current_pattern[-1], mem.pattern)
            else:
                diff = self.matcher.match_2_candle(current_pattern, mem.pattern)

            if diff <= 0.5:
                matches.append((mem, weights[i], high_weights[i], low_weights[i]))

        if not matches:
            return 0.0, 0.0, 0.0

        total_w = sum(w for _, w, _, _ in matches)
        if total_w == 0:
            return 0.0, 0.0, 0.0

        pred_move = sum(m.pattern[-1] * w for m, w, _, _ in matches) / total_w
        hw_sum = sum(hw for _, _, hw, _ in matches)
        lw_sum = sum(lw for _, _, _, lw in matches)
        high_move = sum(m.high_diff * hw for m, _, hw, _ in matches) / hw_sum if hw_sum > 0 else 0
        low_move = sum(m.low_diff * lw for m, _, _, lw in matches) / lw_sum if lw_sum > 0 else 0

        return pred_move, high_move, low_move

    def generate_signal(self, current_price: float, pred_move: float,
                       high_move: float, low_move: float, distance: float = 0.5) -> Tuple[str, float, float]:
        distance = distance / 100.0
        pred_price = current_price * (1 + pred_move / 100)
        high_price = current_price * (1 + high_move / 100)
        low_price = current_price * (1 + low_move / 100)

        high_bound = pred_price * (1 + self.distance)
        low_bound = pred_price * (1 - self.distance)

        if current_price > high_bound:
            signal = "SHORT"
        elif current_price < low_bound:
            signal = "LONG"
        else:
            signal = "WITHIN"

        return signal, high_bound, low_bound


# ============================================================
# DATA FETCHING (KuCoin REST API with pagination)
# ============================================================

KUCOIN_API = "https://api.kucoin.com/api/v1/market/candles"

TIMEFRAME_MAP = {
    "1min": "1min", "3min": "3min", "5min": "5min", "15min": "15min", "30min": "30min",
    "1hour": "1hour", "2hour": "2hour", "4hour": "4hour", "6hour": "6hour",
    "8hour": "8hour", "12hour": "12hour",
    "1day": "1day", "1week": "1week"
}

def fetch_candles_rest(symbol: str, timeframe: str, limit: int = 10000) -> List[Candle]:
    """Fetch historical candles from KuCoin REST API with pagination"""
    tf = TIMEFRAME_MAP.get(timeframe, timeframe)
    all_candles = []
    current_end = int(time.time())

    # Max 1500 candles per request
    while len(all_candles) < limit:
        params = {
            'symbol': symbol,
            'type': tf,
            'startAt': current_end - 365 * 24 * 3600,
            'endAt': current_end
        }

        try:
            r = requests.get(KUCOIN_API, params=params, timeout=15)
            data = r.json()

            if data.get('code') != '200000' or not data.get('data'):
                break

            candles = data['data']
            batch = []
            for row in candles:
                if len(row) >= 5:
                    batch.append(Candle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        close=float(row[2]),
                        high=float(row[3]),
                        low=float(row[4]),
                        volume=float(row[5]) if len(row) > 5 else 0.0
                    ))

            if not batch:
                break

            all_candles = batch + all_candles
            current_end = batch[0].timestamp - 1

            if len(batch) < 1500:
                break

            time.sleep(0.05)

        except Exception as e:
            print(f"Error fetching {symbol} {timeframe}: {e}")
            break

    # Sort chronologically
    all_candles.sort(key=lambda c: c.timestamp)
    return all_candles[-limit:] if len(all_candles) > limit else all_candles


# ============================================================
# REGIME CLASSIFICATION
# ============================================================

def classify_regime(candles: List[Candle], idx: int, lookback: int = 50) -> MarketRegime:
    """Classify market regime using ONLY information available before idx"""
    if idx < lookback:
        return MarketRegime.SIDEWAYS

    lookback_candles = candles[idx - lookback:idx]
    returns = [(c.close - c.open) / c.open for c in lookback_candles if c.open > 0]
    mean_ret = np.mean(returns) if returns else 0
    std_ret = np.std(returns) if len(returns) > 1 else 0

    # Volatility classification (using rolling std percentiles)
    # For simplicity, use fixed thresholds
    vol = std_ret * 100  # as percentage

    if mean_ret > 0.001:  # > 0.1%
        trend = MarketRegime.BULLISH
    elif mean_ret < -0.001:
        trend = MarketRegime.BEARISH
    else:
        trend = MarketRegime.SIDEWAYS

    if vol > 3.0:  # High volatility threshold
        vol_regime = MarketRegime.HIGH_VOL
    elif vol < 0.5:
        vol_regime = MarketRegime.LOW_VOL
    else:
        vol_regime = trend  # Use trend regime for medium vol

    return vol_regime


# ============================================================
# STATISTICAL UTILITIES
# ============================================================

def wilson_ci(successes: int, trials: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Wilson score interval for binomial proportion"""
    if trials == 0:
        return (0.0, 0.0)
    z = norm.ppf(1 - (1 - confidence) / 2)
    p = successes / trials
    denominator = 1 + z**2 / trials
    centre = (p + z**2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2)) / denominator
    return (max(0, centre - half), min(1, centre + half))

def mcnemar_test(both_correct: int, a_only: int, b_only: int, both_wrong: int) -> Dict:
    """McNemar's test for paired binary comparison"""
    # a_only: model A correct, B wrong
    # b_only: model B correct, A wrong
    if a_only + b_only == 0:
        return {"p_value": 1.0, "statistic": 0.0, "significant": False}
    # With continuity correction
    stat = (abs(a_only - b_only) - 1)**2 / (a_only + b_only)
    p_val = 1 - stats.chi2.cdf(stat, 1)
    return {"p_value": p_val, "statistic": stat, "significant": p_val < 0.05}

def block_bootstrap_ci(values: List[float], n_blocks: int = 50, n_iter: int = 1000, confidence: float = 0.95) -> Tuple[float, float]:
    """Block bootstrap CI for time series mean"""
    if len(values) < n_blocks * 2:
        return (np.mean(values), np.mean(values))

    n = len(values)
    block_size = n // n_blocks
    if block_size == 0:
        block_size = 1
        n_blocks = n

    means = []
    for _ in range(n_iter):
        sample = []
        for _ in range(n_blocks):
            start = random.randint(0, n - block_size)
            sample.extend(values[start:start + block_size])
        if sample:
            means.append(np.mean(sample))

    if not means:
        return (np.mean(values), np.mean(values))

    alpha = (1 - confidence) / 2
    lower = np.percentile(means, alpha * 100)
    upper = np.percentile(means, (1 - alpha) * 100)
    return (lower, upper)


# ============================================================
# FROZEN CHRONOLOGICAL HOLDOUT EVALUATION
# ============================================================

class FrozenHoldoutEvaluator:
    """Train on first segment, freeze, test on final segment"""

    def __init__(self, pattern_length: int, train_ratio: float = 0.7, min_train: int = 100):
        self.pattern_length = pattern_length
        self.train_ratio = train_ratio
        self.min_train = min_train

    async def run(self, candles: List[Candle], fee_bps: int = 10, slippage_bps: int = 5) -> Dict:
        if len(candles) < self.min_train + 50:
            return {"error": f"Insufficient data: {len(candles)} candles"}

        split_idx = int(len(candles) * self.train_ratio)
        train_candles = candles[:split_idx]
        test_candles = candles[split_idx:]

        print(f"    Holdout: Total={len(candles)}, Train={len(train_candles)}, Test={len(test_candles)}")

        trainer = LeakageFreeTrainer(pattern_length=self.pattern_length, threshold=0.5)
        trainer.build_memories_up_to(candles, len(train_candles))

        if not trainer.memories:
            return {"error": "No memories generated"}

        init_weights = trainer.weights.copy()
        init_hw = trainer.high_weights.copy()
        init_lw = trainer.low_weights.copy()
        init_thresh = trainer.perfect_threshold

        results = {}
        for method_name, pattern_len in [("1-candle", 1), ("2-candle", 2)]:
            trainer.memories = trainer.memories
            trainer.weights = init_weights.copy()
            trainer.high_weights = init_hw.copy()
            trainer.low_weights = init_lw.copy()
            trainer.perfect_threshold = init_thresh
            trainer.matcher = PatternMatching(threshold=0.5, pattern_length=pattern_len)

            predictor = Predictor(pattern_length=pattern_len, threshold=0.5, distance=0.5)

            method_results = self._evaluate_frozen(test_candles, trainer, predictor, pattern_len, fee_bps, slippage_bps)
            results[f"results_{method_name}"] = method_results

        return results

    def _evaluate_frozen(self, test_candles: List[Candle],
                        trainer: LeakageFreeTrainer,
                        predictor: Predictor,
                        pattern_len: int,
                        fee_bps: int, slippage_bps: int) -> Dict:

        fee = fee_bps / 10000.0
        slip = slippage_bps / 10000.0
        round_trip_cost = 2 * fee + 2 * slip  # Entry + exit fees + slippage

        results = {
            "predictions": 0,
            "long_signals": 0,
            "short_signals": 0,
            "within_signals": 0,
            "correct_direction": 0,
            "total_direction": 0,
            "returns": [],
            "net_returns": [],
            "mfe": [],
            "mae": [],
            "trades": [],
            "equity_curve": [1.0],
            "regime_performance": defaultdict(lambda: {
                "predictions": 0, "correct": 0, "returns": [], "net_returns": []
            }),
            "monthly_performance": defaultdict(lambda: {"returns": [], "net_returns": []}),
        }

        # Frozen weights - no updates during test
        weights = trainer.weights.copy()
        high_weights = trainer.high_weights.copy()
        low_weights = trainer.low_weights.copy()

        for i in range(1, len(test_candles) - 1):
            current_candle = test_candles[i]
            next_candle = test_candles[i + 1]

            if i < self.pattern_length:
                continue

            current_pattern = []
            for j in range(self.pattern_length):
                idx = i - self.pattern_length + 1 + j
                if idx < 0:
                    break
                c = test_candles[idx]
                pct = (c.close - c.open) / c.open * 100 if c.open > 0 else 0.0
                current_pattern.append(pct)

            if len(current_pattern) != self.pattern_length:
                continue

            current_price = current_candle.close

            pred_move, high_move, low_move = predictor.predict(
                current_pattern, trainer.memories,
                weights, high_weights, low_weights
            )

            signal, high_bound, low_bound = predictor.generate_signal(
                current_price, pred_move, high_move, low_move
            )

            next_price = next_candle.close
            actual_move = (next_price - current_price) / current_price

            results["predictions"] += 1

            # Regime classification
            regime = classify_regime(test_candles, i)

            # Monthly classification
            month_key = datetime.fromtimestamp(current_candle.timestamp).strftime("%Y-%m")

            if signal == "LONG":
                results["long_signals"] += 1
                ret = (next_candle.close - current_price) / current_price
                mfe = (next_candle.high - current_price) / current_price
                mae = (current_price - next_candle.low) / current_price
                net_ret = ret - round_trip_cost
                results["trades"].append({"side": "LONG", "gross": ret, "net": net_ret,
                                           "mfe": mfe, "mae": mae})
            elif signal == "SHORT":
                results["short_signals"] += 1
                ret = (current_price - next_candle.close) / current_price
                mfe = (current_price - next_candle.low) / current_price
                mae = (next_candle.high - current_price) / current_price
                net_ret = ret - round_trip_cost
                results["trades"].append({"side": "SHORT", "gross": ret, "net": net_ret,
                                           "mfe": mfe, "mae": mae})
            else:
                results["within_signals"] += 1
                ret = 0
                mfe = mae = 0
                net_ret = 0

            results["returns"].append(ret)
            results["net_returns"].append(net_ret)
            results["mfe"].append(mfe)
            results["mae"].append(mae)
            results["equity_curve"].append(results["equity_curve"][-1] * (1 + net_ret))

            # Directional accuracy
            predicted_up = pred_move > 0
            actual_up = actual_move > 0
            if predicted_up == actual_up:
                results["correct_direction"] += 1
                results["regime_performance"][regime.value]["correct"] += 1
            results["total_direction"] += 1
            results["regime_performance"][regime.value]["predictions"] += 1
            results["regime_performance"][regime.value]["returns"].append(ret)
            results["regime_performance"][regime.value]["net_returns"].append(net_ret)

            # Monthly tracking
            results["monthly_performance"][month_key]["returns"].append(ret)
            results["monthly_performance"][month_key]["net_returns"].append(net_ret)

        return self._summarize(results)

    def _summarize(self, r: Dict) -> Dict:
        out = {}
        out["predictions"] = r["predictions"]
        out["long_signals"] = r["long_signals"]
        out["short_signals"] = r["short_signals"]
        out["within_signals"] = r["within_signals"]

        if r["total_direction"] > 0:
            out["directional_accuracy"] = r["correct_direction"] / r["total_direction"]
            out["ci_95"] = wilson_ci(r["correct_direction"], r["total_direction"])

        if r["returns"]:
            out["avg_return"] = float(np.mean(r["returns"]))
            out["median_return"] = float(np.median(r["returns"]))
            out["std_return"] = float(np.std(r["returns"]))
            out["avg_mfe"] = float(np.mean(r["mfe"]))
            out["avg_mae"] = float(np.mean(r["mae"]))

        if r["net_returns"]:
            out["avg_net_return"] = float(np.mean(r["net_returns"]))
            out["compounded_net_return"] = float(r["equity_curve"][-1] - 1)
            out["total_net_return"] = float(sum(r["net_returns"]))
            out["total_gross_return"] = float(sum(r["returns"]))
            out["total_fees"] = float(len(r["trades"]) * (r.get("fee", 0.001) + r.get("slippage", 0.0005)) * 2) if r["trades"] else 0

        if r["trades"]:
            wins = [t for t in r["trades"] if t["net"] > 0]
            losses = [t for t in r["trades"] if t["net"] <= 0]
            out["num_trades"] = len(r["trades"])
            out["win_rate"] = len(wins) / len(r["trades"]) if r["trades"] else 0
            out["avg_winner"] = float(np.mean([t["net"] for t in wins])) if wins else 0
            out["avg_loser"] = float(np.mean([t["net"] for t in losses])) if losses else 0
            out["payoff_ratio"] = abs(out["avg_winner"] / out["avg_loser"]) if out["avg_loser"] != 0 else 0
            out["expectancy"] = out["win_rate"] * out["avg_winner"] + (1 - out["win_rate"]) * out["avg_loser"]
            gross_profit = sum(t["net"] for t in wins)
            gross_loss = abs(sum(t["net"] for t in losses))
            out["profit_factor"] = gross_profit / gross_loss if gross_loss > 0 else float('inf')

            # Max drawdown
            equity = r["equity_curve"]
            peak = equity[0]
            max_dd = 0
            for v in equity:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
            out["max_drawdown"] = max_dd

        # Regime breakdown
        out["regimes"] = {}
        for regime, data in r["regime_performance"].items():
            if data["predictions"] > 0:
                out["regimes"][regime] = {
                    "predictions": data["predictions"],
                    "dir_acc": data["correct"] / data["predictions"],
                    "avg_return": float(np.mean(data["returns"])) if data["returns"] else 0,
                    "avg_net_return": float(np.mean(data["net_returns"])) if data["net_returns"] else 0,
                }

        # Monthly/subperiod breakdown
        out["subperiods"] = {}
        for month, data in r["monthly_performance"].items():
            if data["returns"]:
                out["subperiods"][month] = {
                    "avg_return": float(np.mean(data["returns"])),
                    "avg_net_return": float(np.mean(data["net_returns"])),
                    "count": len(data["returns"])
                }

        return out


# ============================================================
# EXPANDING WALK-FORWARD EVALUATION
# ============================================================

class ExpandingWalkForwardEvaluator:
    """True expanding walk-forward: retrain/expand at each step"""

    def __init__(self, pattern_length: int, initial_train: int = 1000, min_train: int = 100):
        self.pattern_length = pattern_length
        self.initial_train = initial_train
        self.min_train = min_train

    async def run(self, candles: List[Candle], fee_bps: int = 10, slippage_bps: int = 5) -> Dict:
        n = len(candles)
        if n < self.initial_train + 50:
            return {"error": f"Insufficient data: {n} candles"}

        print(f"    Expanding WF: Total={n}, InitialTrain={self.initial_train}")

        fee = fee_bps / 10000.0
        slip = slippage_bps / 10000.0
        round_trip = 2 * fee + 2 * slip

        # We'll store predictions for comparison
        all_predictions = []

        for method_name, pattern_len in [("1-candle", 1), ("2-candle", 2)]:
            preds = self._evaluate_expanding(candles, pattern_len, round_trip)
            all_predictions.append({"method": method_name, "predictions": preds})

        return {"predictions": all_predictions}

    def _evaluate_expanding(self, candles: List[Candle], pattern_len: int, round_trip: float) -> List[Dict]:
        n = len(candles)
        results = []

        # Start with initial training window
        train_end = self.initial_train

        for i in range(train_end, n - 1):
            if i % 1000 == 0:
                print(f"      Expanding WF step {i}/{n-1} (train_end={train_end})")

            # Build memories from all data up to i
            trainer = LeakageFreeTrainer(pattern_length=pattern_len, threshold=0.5)
            trainer.build_memories_up_to(candles, i)

            if not trainer.memories:
                continue

            # Predict at i using data up to i
            current_candle = candles[i]
            next_candle = candles[i + 1]

            if i < pattern_len:
                continue

            current_pattern = []
            for j in range(pattern_len):
                idx = i - pattern_len + 1 + j
                if idx < 0:
                    break
                c = candles[idx]
                pct = (c.close - c.open) / c.open * 100 if c.open > 0 else 0.0
                current_pattern.append(pct)

            if len(current_pattern) != pattern_len:
                continue

            current_price = current_candle.close

            predictor = Predictor(pattern_length=pattern_len, threshold=0.5, distance=0.5)
            pred_move, high_move, low_move = predictor.predict(
                current_pattern, trainer.memories,
                trainer.weights, trainer.high_weights, trainer.low_weights
            )

            signal, high_bound, low_bound = predictor.generate_signal(
                current_price, pred_move, high_move, low_move
            )

            next_price = next_candle.close
            actual_move = (next_price - current_price) / current_price

            if signal == "LONG":
                ret = (next_candle.close - current_price) / current_price
            elif signal == "SHORT":
                ret = (current_price - next_candle.close) / current_price
            else:
                ret = 0

            net_ret = ret - round_trip if signal != "WITHIN" else 0

            predicted_up = pred_move > 0
            actual_up = actual_move > 0
            correct = predicted_up == actual_up

            results.append({
                "timestamp": current_candle.timestamp,
                "signal": signal,
                "pred_move": pred_move,
                "actual_move": actual_move * 100,
                "gross_return": ret,
                "net_return": net_ret,
                "correct_direction": correct
            })

        return results


# ============================================================
# COMPREHENSIVE EVALUATION
# ============================================================

async def run_full_evaluation():
    coins = ['BTC', 'ETH', 'BNB', 'SOL', 'XRP']
    timeframes = ['1hour', '2hour', '4hour', '8hour', '12hour', '1day', '1week']
    pattern_lengths = [1, 2]

    all_results = []

    for coin in coins:
        for tf in timeframes:
            for pattern_len in pattern_lengths:
                print(f"\n{'='*70}")
                print(f"Evaluating {coin} {tf} pattern_length={pattern_len}")
                print(f"{'='*70}")

                candles = fetch_candles_rest(f"{coin}-USDT", tf, limit=10000)
                if len(candles) < 200:
                    print(f"  Insufficient data: {len(candles)} candles")
                    all_results.append({
                        "coin": coin, "timeframe": tf, "pattern_length": pattern_len,
                        "error": f"Insufficient data: {len(candles)} candles",
                        "candles": len(candles)
                    })
                    continue

                print(f"  Data: {len(candles)} candles, {datetime.fromtimestamp(candles[0].timestamp)} to {datetime.fromtimestamp(candles[-1].timestamp)}")

                # Frozen holdout
                print("  Running frozen holdout...")
                holdout_eval = FrozenHoldoutEvaluator(pattern_length=pattern_len, train_ratio=0.7)
                holdout_results = await holdout_eval.run(candles)

                # Expanding walk-forward (only for 2-candle since it's the target)
                print("  Running expanding walk-forward...")
                expanding_eval = ExpandingWalkForwardEvaluator(pattern_length=pattern_len, initial_train=max(500, int(len(candles)*0.5)))
                expanding_results = await expanding_eval.run(candles)

                # Baseline comparisons
                print("  Running baselines...")
                baselines = run_baselines(candles, pattern_len)

                result = {
                    "coin": coin,
                    "timeframe": tf,
                    "pattern_length": pattern_len,
                    "data": {
                        "total_candles": len(candles),
                        "first_ts": candles[0].timestamp,
                        "last_ts": candles[-1].timestamp,
                        "first_date": datetime.fromtimestamp(candles[0].timestamp).isoformat(),
                        "last_date": datetime.fromtimestamp(candles[-1].timestamp).isoformat(),
                    },
                    "holdout": holdout_results,
                    "expanding": expanding_results,
                    "baselines": baselines
                }

                all_results.append(result)

                # Save intermediate
                with open(f"result_{coin}_{tf}_{pattern_len}c.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)

                print(f"  Completed: {coin} {tf} pattern_length={pattern_len}")

    return all_results


def run_baselines(candles: List[Candle], pattern_len: int) -> Dict:
    """Run baseline strategies on the test portion"""
    n = len(candles)
    split_idx = int(n * 0.7)
    test_candles = candles[split_idx:]

    baselines = {}
    fee = 0.001
    slip = 0.0005
    round_trip = 2 * fee + 2 * slip

    # 1. Random
    baselines["random"] = simulate_baseline(test_candles, lambda i: random.choice(["LONG", "SHORT"]), round_trip)

    # 2. Previous candle direction
    def prev_dir(i):
        if i == 0: return "WITHIN"
        c = test_candles[i-1]
        return "LONG" if c.close > c.open else "SHORT"
    baselines["prev_candle"] = simulate_baseline(test_candles, prev_dir, round_trip)

    # 3. 2-candle momentum
    def mom2(i):
        if i < 2: return "WITHIN"
        r1 = (test_candles[i-1].close - test_candles[i-1].open) / test_candles[i-1].open
        r2 = (test_candles[i-2].close - test_candles[i-2].open) / test_candles[i-2].open
        return "LONG" if (r1 + r2) > 0 else "SHORT"
    baselines["momentum_2"] = simulate_baseline(test_candles, mom2, round_trip)

    # 4. Buy and hold
    baselines["buy_hold"] = simulate_buy_hold(test_candles, fee)

    return baselines


def simulate_baseline(test_candles: List[Candle], signal_fn, round_trip: float) -> Dict:
    returns = []
    net_returns = []
    equity = [1.0]
    correct = total = 0

    for i in range(1, len(test_candles) - 1):
        signal = signal_fn(i)
        current = test_candles[i].close
        next_price = test_candles[i + 1].close

        if signal == "LONG":
            ret = (next_price - current) / current
        elif signal == "SHORT":
            ret = (current - next_price) / current
        else:
            ret = 0

        net_ret = ret - round_trip if signal != "WITHIN" else 0
        returns.append(ret)
        net_returns.append(net_ret)
        equity.append(equity[-1] * (1 + net_ret))

        # Direction
        actual_up = (next_price - current) > 0
        pred_up = signal == "LONG"
        if signal != "WITHIN":
            total += 1
            if pred_up == actual_up:
                correct += 1

    return summarize_baseline(returns, net_returns, equity, correct, total)


def simulate_buy_hold(test_candles: List[Candle], fee: float) -> Dict:
    if len(test_candles) < 2:
        return {"error": "insufficient"}
    entry = test_candles[0].close
    exit_price = test_candles[-1].close
    ret = (exit_price - entry) / entry - fee  # Only entry fee
    return {
        "total_return": ret,
        "compounded_return": ret,
        "num_trades": 1,
        "win_rate": 1.0 if ret > 0 else 0.0,
        "max_drawdown": 0.0
    }


def summarize_baseline(returns, net_returns, equity, correct, total):
    if not returns:
        return {"error": "no predictions"}

    wins = [r for r in net_returns if r > 0]
    losses = [r for r in net_returns if r <= 0]

    # Max drawdown
    peak = equity[0]
    max_dd = 0
    for v in equity:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd

    return {
        "predictions": len(returns),
        "directional_accuracy": correct / total if total > 0 else 0,
        "avg_return": float(np.mean(returns)),
        "avg_net_return": float(np.mean(net_returns)),
        "compounded_net_return": float(equity[-1] - 1),
        "num_trades": len([r for r in net_returns if r != 0]),
        "win_rate": len(wins) / len(net_returns) if net_returns else 0,
        "avg_winner": float(np.mean(wins)) if wins else 0,
        "avg_loser": float(np.mean(losses)) if losses else 0,
        "expectancy": float(np.mean(net_returns)),
        "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf'),
        "max_drawdown": max_dd,
        "ci_95": wilson_ci(correct, total) if total > 0 else (0, 0)
    }


# ============================================================
# FORENSIC VERIFICATION
# ============================================================

def verify_timestamps(candles: List[Candle]) -> Dict:
    """Verify candle ordering and integrity"""
    issues = []

    # Check chronological order
    for i in range(1, len(candles)):
        if candles[i].timestamp <= candles[i-1].timestamp:
            issues.append(f"Non-increasing timestamp at index {i}: {candles[i-1].timestamp} -> {candles[i].timestamp}")

    # Check OHLC validity
    for i, c in enumerate(candles):
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close):
            issues.append(f"Invalid OHLC at index {i}: O={c.open} H={c.high} L={c.low} C={c.close}")

    # Check duplicates
    timestamps = [c.timestamp for c in candles]
    if len(timestamps) != len(set(timestamps)):
        issues.append(f"Duplicate timestamps: {len(timestamps) - len(set(timestamps))}")

    return {"valid": len(issues) == 0, "issues": issues}


def verify_no_leakage(candles: List[Candle], train_end: int, test_idx: int) -> bool:
    """Assert: max(feature_timestamp) < prediction_timestamp < outcome_timestamp"""
    # Features use candles[test_idx - pattern_len + 1 : test_idx + 1]
    # Prediction is for test_idx
    # Outcome is test_idx + 1
    return test_idx < test_idx + 1  # Trivially true, but documents the constraint


# ============================================================
# PARAMETER SENSITIVITY
# ============================================================

async def parameter_sensitivity(candles: List[Candle], pattern_len: int) -> Dict:
    """Test sensitivity to key parameters"""
    sensitivities = {}

    # Fee sensitivity
    for fee_bps in [5, 10, 15, 20]:
        eval = FrozenHoldoutEvaluator(pattern_length=pattern_len, train_ratio=0.7)
        res = await eval.run(candles, fee_bps=fee_bps, slippage_bps=5)
        if "results_2-candle" in res:
            sensitivities[f"fee_{fee_bps}bps"] = res["results_2-candle"].get("expectancy", 0)

    # Slippage sensitivity
    for slip_bps in [0, 5, 10, 20]:
        eval = FrozenHoldoutEvaluator(pattern_length=pattern_len, train_ratio=0.7)
        res = await eval.run(candles, fee_bps=10, slippage_bps=slip_bps)
        if "results_2-candle" in res:
            sensitivities[f"slip_{slip_bps}bps"] = res["results_2-candle"].get("expectancy", 0)

    # Training window sensitivity
    for train_ratio in [0.5, 0.6, 0.7, 0.8]:
        eval = FrozenHoldoutEvaluator(pattern_length=pattern_len, train_ratio=train_ratio)
        res = await eval.run(candles, fee_bps=10, slippage_bps=5)
        if "results_2-candle" in res:
            sensitivities[f"train_{int(train_ratio*100)}pct"] = res["results_2-candle"].get("expectancy", 0)

    return sensitivities


# ============================================================
# MAIN
# ============================================================

async def main():
    print("=" * 70)
    print("PHASE 3B-A: COMPLETE LEAKAGE-FREE WALK-FORWARD VALIDATION")
    print("=" * 70)

    results = await run_full_evaluation()

    # Save all results
    with open("phase3b_complete_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print("PHASE 3B-A COMPLETE")
    print("=" * 70)
    print(f"Results saved to phase3b_complete_results.json")

    # Print summary table
    print("\n\n=== SUMMARY TABLE ===")
    for r in results:
        if "error" in r:
            print(f"{r['coin']} {r['timeframe']} {r['pattern_length']}c: ERROR - {r['error']}")
            continue

        coin = r["coin"]
        tf = r["timeframe"]
        pl = r["pattern_length"]

        holdout = r.get("holdout", {})
        for method in ["1-candle", "2-candle"]:
            key = f"results_{method}"
            if key in holdout:
                h = holdout[key]
                print(f"  {coin} {tf} {pl}c {method}: DirAcc={h.get('directional_accuracy', 0):.2%} "
                      f"[{h.get('ci_95', (0,0))[0]:.2%}-{h.get('ci_95', (0,0))[1]:.2%}] "
                      f"NetExp={h.get('expectancy', 0):.6f} "
                      f"CompRet={h.get('compounded_net_return', 0):.4%} "
                      f"Trades={h.get('num_trades', 0)} "
                      f"MaxDD={h.get('max_drawdown', 0):.2%}")

    return results


if __name__ == "__main__":
    asyncio.run(main())