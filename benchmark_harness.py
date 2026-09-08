#!/usr/bin/env python3
"""
BENCHMARK HARNESS — Phase 3A Prediction Forensic Audit
Isolated research artifact — does NOT modify production code.
"""

import asyncio
import numpy as np
import json
from datetime import datetime, timedelta
from kucoin.client import Market
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import random
from typing import Any

market = Market(url='https://api.kucoin.com')

@dataclass
class Memory:
    """Training memory structure"""
    pattern: List[float]  # 2 candles: [pct_change_1, pct_change_2]
    high_diff: float      # future high as % of start price
    low_diff: float       # future low as % of start price
    weight: float = 1.0
    high_weight: float = 1.0
    low_weight: float = 1.0

@dataclass 
class Candle:
    open: float
    high: float
    low: float
    close: float
    timestamp: int

class PredictorSimulator:
    """
    Faithful simulation of pt_thinker.py logic.
    Tests both current (buggy) and corrected (2-candle) matching.
    """
    
    def __init__(self, threshold: float = 0.5, distance: float = 0.5):
        self.threshold = threshold
        self.distance = distance / 100  # convert to decimal
        
    def compute_pct_change(self, open_price: float, close_price: float) -> float:
        """Convert OHLC to percentage change"""
        if open_price == 0:
            return 0.0
        return (close_price - open_price) / open_price * 100
    
    def build_current_pattern(self, candles: List[Candle], pattern_length: int = 2) -> List[float]:
        """Build pattern from last N candles"""
        pattern = []
        for c in candles[-pattern_length:]:
            pct = self.compute_pct_change(c.open, c.close)
            pattern.append(pct)
        return pattern
    
    def build_memory_from_history(self, candles: List[Candle], pattern_length: int = 2) -> List[Memory]:
        """Extract training memories from historical candles"""
        memories = []
        # Need pattern_length + 1 candles (pattern + future outcome)
        for i in range(len(candles) - pattern_length - 1):
            # Build pattern from candles[i:i+pattern_length]
            pattern = []
            for j in range(pattern_length):
                pct = self.compute_pct_change(candles[i+j].open, candles[i+j].close)
                pattern.append(pct)
            
            # Future outcome: next candle after pattern
            start_price = candles[i + pattern_length - 1].close
            high_diff = (candles[i + pattern_length].high - start_price) / start_price * 100
            low_diff = (candles[i + pattern_length].low - start_price) / start_price * 100
            
            memories.append(Memory(
                pattern=pattern,
                high_diff=high_diff,
                low_diff=low_diff
            ))
        
        return memories
    
    def match_memory_current_bug(self, current_candle_pct: float, memory: Memory) -> float:
        """CURRENT BUGGY MATCHING: only compares first candle"""
        memory_candle = memory.pattern[0] if isinstance(memory.pattern, list) else memory.pattern
        if current_candle_pct == 0 and memory_candle == 0:
            return 0.0
        return abs(current_candle_pct - memory_candle) / ((abs(current_candle_pct) + abs(memory_candle)) / 2) * 100
    
    def match_memory_correct(self, current_pattern: List[float], memory: Memory) -> float:
        """CORRECT MATCHING: compares all pattern elements"""
        if len(memory.pattern) != len(current_pattern):
            return float('inf')
        diffs = []
        for c, m in zip(current_pattern, memory.pattern):
            if c == 0 and m == 0:
                diffs.append(0)
            else:
                diffs.append(abs(c - m) / ((abs(c) + abs(m)) / 2) * 100)
        return np.mean(diffs)
    
    def predict(self, current_pattern: List[float], memories: List[Memory], 
                weights: List[float], high_weights: List[float], low_weights: List[float],
                use_correct_matching: bool = False) -> Tuple[float, float, float]:
        """Generate prediction using weighted average of matching memories"""
        
        matches = []
        for i, mem in enumerate(memories):
            if use_correct_matching:
                diff = self.match_memory_correct(current_pattern, mem)
            else:
                # Current buggy behavior: only compare last candle (pattern[-1])
                diff = self.match_memory_current_bug(current_pattern[-1] if current_pattern else 0, mem)
            
            if diff <= self.threshold:
                w = weights[i] if i < len(weights) else 1.0
                hw = high_weights[i] if i < len(high_weights) else 1.0
                lw = low_weights[i] if i < len(low_weights) else 1.0
                matches.append((mem, w, hw, lw))
        
        if not matches:
            return 0.0, 0.0, 0.0
        
        # Weighted averages - based on thinker logic
        # thinker stores "future move" as last element of memory_pattern
        # But in our Memory dataclass, we store pattern and high/low_diff separately
        # The thinker uses memory_pattern[-1] as the "future move"
        # Since we store pattern and high/low_diff separately, we need to adapt
        
        total_w = sum(w for _, w, _, _ in matches)
        if total_w == 0:
            return 0.0, 0.0, 0.0
        
        # Pred move: weighted average of stored future moves
        # In trainer, memory_pattern[-1] is the future close pct change
        # But we don't store that directly. Let's use high_diff as proxy for upward move
        pred_move = sum(m.high_diff * w for m, w, _, _ in matches) / total_w
        high_move = sum(m.high_diff * hw for m, _, hw, _ in matches) / sum(hw for _, _, hw, _ in matches) if sum(hw for _, _, hw, _ in matches) > 0 else 0
        low_move = sum(m.low_diff * lw for m, _, _, lw in matches) / sum(lw for _, _, _, lw in matches) if sum(lw for _, _, _, lw in matches) > 0 else 0
        
        return pred_move, high_move, low_move
    
    def generate_signal(self, current_price: float, pred_move: float, 
                       high_move: float, low_move: float) -> Tuple[str, float, float]:
        """Generate LONG/SHORT/WITHIN signal with bounds"""
        # From thinker: predicted price = current * (1 + pred_move/100)
        # Then bounds = predicted ± distance
        pred_price = current_price * (1 + pred_move / 100)
        high_price = current_price * (1 + high_move / 100)
        low_price = current_price * (1 + low_move / 100)
        
        # Bounds from thinker: predicted ± distance
        high_bound = pred_price * (1 + self.distance)
        low_bound = pred_price * (1 - self.distance)
        
        if current_price > high_bound:
            return "SHORT", high_bound, low_bound
        elif current_price < low_bound:
            return "LONG", high_bound, low_bound
        else:
            return "WITHIN", high_bound, low_bound


async def fetch_candles(market: Market, symbol: str, timeframe: str, limit: int = 5000) -> List[Candle]:
    """Fetch historical candles from KuCoin"""
    try:
        history = market.get_kline(symbol, timeframe)
        candles = []
        for row in history:
            if len(row) >= 5:
                candles.append(Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4])
                ))
        return candles[-limit:] if len(candles) > limit else candles
    except Exception as e:
        print(f"Error fetching {symbol} {timeframe}: {e}")
        return []


async def run_backtest(
    coin: str, 
    timeframe: str, 
    train_split: float = 0.7,
    use_correct_matching: bool = False
) -> Dict:
    """Run walk-forward backtest for one coin/timeframe"""
    
    market = Market(url='https://api.kucoin.com')
    symbol = f"{coin}-USDT"
    
    # Fetch data
    candles = await fetch_candles(market, symbol, timeframe, limit=10000)
    if len(candles) < 100:
        return {"error": f"Insufficient data: {len(candles)} candles"}
    
    # Split
    split_idx = int(len(candles) * train_split)
    train_candles = candles[:split_idx]
    test_candles = candles[split_idx:]
    
    print(f"{coin} {timeframe}: Train={len(train_candles)}, Test={len(test_candles)}")
    
    # Build memories from training data
    simulator = PredictorSimulator(threshold=0.5, distance=0.5)
    memories = simulator.build_memory_from_history(train_candles, pattern_length=2)
    
    # Initialize weights (all 1.0 like fresh training)
    weights = [1.0] * len(memories)
    high_weights = [1.0] * len(memories)
    low_weights = [1.0] * len(memories)
    
    if not memories:
        return {"error": "No memories generated"}
    
    print(f"  Generated {len(memories)} memories from training data")
    
    # Walk-forward test
    results = {
        "coin": coin,
        "timeframe": timeframe,
        "train_samples": len(train_candles),
        "test_samples": len(test_candles),
        "memories": len(memories),
        "use_correct_matching": use_correct_matching,
        "predictions": 0,
        "long_signals": 0,
        "short_signals": 0,
        "within_signals": 0,
        "correct_direction": 0,
        "total_direction": 0,
        "returns": [],
        "mfe": [],  # Maximum Favorable Excursion
        "mae": [],  # Maximum Adverse Excursion
    }
    
    # Walk-forward: for each test candle, predict next, then update weights
    for i in range(1, len(test_candles) - 1):
        current_candle = test_candles[i]
        next_candle = test_candles[i + 1]
        
        # Build current pattern (last 2 candles)
        current_pattern = [
            (test_candles[i-1].close - test_candles[i-1].open) / test_candles[i-1].open * 100,
            (current_candle.close - current_candle.open) / current_candle.open * 100
        ]
        
        current_price = current_candle.close
        
        # Predict
        pred_move, high_move, low_move = simulator.predict(
            current_pattern, memories, 
            weights, high_weights, low_weights,
            use_correct_matching=use_correct_matching
        )
        
        signal, high_bound, low_bound = simulator.generate_signal(
            current_price, pred_move, high_move, low_move
        )
        
        # Actual outcome
        next_price = next_candle.close
        actual_move = (next_price - current_price) / current_price * 100
        
        # Record results
        results["predictions"] += 1
        if signal == "LONG":
            results["long_signals"] += 1
        elif signal == "SHORT":
            results["short_signals"] += 1
        else:
            results["within_signals"] += 1
        
        # Directional accuracy
        predicted_up = pred_move > 0
        actual_up = ((next_price - current_price) / current_price * 100) > 0
        if predicted_up == actual_up:
            results["correct_direction"] += 1
        results["total_direction"] += 1
        
        # Returns
        if signal == "LONG":
            ret = (next_price - current_price) / current_price
        elif signal == "SHORT":
            ret = (current_price - next_price) / current_price
        else:
            ret = 0
        results["returns"].append(ret)
        
        # MFE/MAE (simplified: use next candle high/low)
        if signal == "LONG":
            mfe = (next_candle.high - current_price) / current_price
            mae = (current_price - next_candle.low) / current_price
        elif signal == "SHORT":
            mfe = (current_price - next_candle.low) / current_price
            mae = (next_candle.high - current_price) / current_price
        else:
            mfe = mae = 0
        results["mfe"].append(mfe)
        results["mae"].append(mae)
        
        # Update weights based on prediction accuracy (simplified)
        for j, mem in enumerate(memories):
            diff = simulator.match_memory_correct if use_correct_matching else simulator.match_memory_current_bug
            if use_correct_matching:
                diff_val = diff(current_pattern, memories[j])
            else:
                diff_val = diff(current_pattern[-1], memories[j])
            if diff_val <= simulator.threshold:
                if (pred_move > 0 and ((next_candle.close - current_price) / current_price * 100) > 0) or \
                   (pred_move < 0 and ((next_candle.close - current_price) / current_price * 100) < 0):
                    weights[j] = min(weights[j] + 0.25, 2.0)
                else:
                    weights[j] = max(weights[j] - 0.25, 0.0)
    
    # Summary
    if results["total_direction"] > 0:
        results["directional_accuracy"] = results["correct_direction"] / results["total_direction"]
    if results["returns"]:
        results["avg_return"] = float(np.mean(results["returns"]))
        results["median_return"] = float(np.median(results["returns"]))
    if results["mfe"]:
        results["avg_mfe"] = float(np.mean(results["mfe"]))
    if results["mae"]:
        results["avg_mae"] = float(np.mean(results["mae"]))
    
    return results


async def main():
    """Run baseline benchmarks for all coins/timeframes"""
    
    coins = ['BTC', 'ETH', 'BNB', 'SOL', 'XRP']
    timeframes = ['1hour', '4hour', '1day']
    
    print("=" * 60)
    print("PHASE 3A BASELINE BENCHMARK")
    print("=" * 60)
    print(f"Testing pattern matching: BUGGY (1-candle) vs CORRECT (2-candle)")
    print()
    
    all_results = []
    
    for coin in coins:
        for tf in timeframes:
            print(f"\n--- {coin} {tf} ---")
            
            # Test buggy matching
            print("  Testing BUGGY matching (1-candle)...")
            buggy = await run_backtest(coin, tf, use_correct_matching=False)
            
            # Test correct matching
            print("  Testing CORRECT matching (2-candle)...")
            correct = await run_backtest(coin, tf, use_correct_matching=True)
            
            all_results.append({
                "coin": coin,
                "timeframe": tf,
                "buggy": buggy,
                "correct": correct
            })
    
    # Save results
    with open("benchmark_results.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    
    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for r in all_results:
        b = r["buggy"]
        c = r["correct"]
        print(f"\n{r['coin']} {r['timeframe']}:")
        print(f"  Buggy:  DirAcc={b.get('directional_accuracy',0):.2%}, AvgRet={b.get('avg_return',0):.4f}, Sigs={b.get('predictions',0)}")
        print(f"  Correct:DirAcc={c.get('directional_accuracy',0):.2%}, AvgRet={c.get('avg_return',0):.4f}, Sigs={c.get('predictions',0)}")
    
    return all_results


if __name__ == "__main__":
    asyncio.run(main())