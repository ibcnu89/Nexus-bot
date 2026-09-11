"""
Narrative Engine - Extracts and classifies narrative signals from token metadata.

Implements zero-cost narrative detection using lawful public data sources.
LLM is used only for classification/summarization, never for trade authorization.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger(__name__)


class NarrativeCategory(str, Enum):
    """Narrative category classifications."""
    ANIMAL_MEME = "animal_meme"
    POLITICAL = "political"
    CELEBRITY = "celebrity"
    AI = "ai"
    GAMING = "gaming"
    CRYPTO_NATIVE = "crypto_native"
    CURRENT_EVENT = "current_event"
    ABSURDIST = "absurdist"
    COMMUNITY = "community"
    COPYCAT = "copycat"
    UNKNOWN = "unknown"


class NarrativeCategoryKeywords:
    """Keyword sets for narrative classification."""
    
    # Compiled regex patterns for each category
    PATTERNS: Dict[str, List[str]] = {
        "animal_meme": [
            r"\b(dog|cat|frog|pepe|wojak|chad|shiba|inu|doge|bonk|woof|meow|paw|tail)\b",
            r"\b(animal|pet|zoo|wild|nature)\b",
        ],
        "political": [
            r"\b(trump|biden|kamala|putin|zelensky|xi|modi|netanyahu)\b",
            r"\b(election|vote|campaign|policy|senate|congress|parliament)\b",
            r"\b(democrat|republican|liberal|conservative|left|right|wing)\b",
            r"\b(maga|woke|cancel|culture|war|peace|sanction)\b",
        ],
        "celebrity": [
            r"\b(elon|musk|bezos|gates|zuckerberg|buffett)\b",
            r"\b(kanye|kardashian|swift|beyonce|drake|bieber|rihanna)\b",
            r"\b(celebrity|star|famous|influencer|youtuber|streamer)\b",
        ],
        "ai": [
            r"\b(ai|artificial|intelligence|gpt|llm|chatgpt|claude|gemini|bard)\b",
            r"\b(machine|learning|neural|network|transformer|diffusion|stable)\b",
            r"\b(openai|anthropic|google|microsoft|nvidia|amd|intel)\b",
            r"\b(agent|autonomous|automation|singularity|agi|asi)\b",
        ],
        "gaming": [
            r"\b(game|gaming|play|player|esports|tournament|stream)\b",
            r"\b(nft|play.?to.?earn|p2e|metaverse|virtual|world|avatar)\b",
            r"\b(axie|sandbox|decentraland|illuvium|star.?atlas)\b",
        ],
        "crypto_native": [
            r"\b(defi|dao|yield|farm|stake|liquidity|pool|swap|dex|amm)\b",
            r"\b(eth|btc|sol|bsc|avax|polygon|arbitrum|optimism|base|linea)\b",
            r"\b(bridge|rollup|layer.?2|l2|zk|optimistic|validium)\b",
            r"\b(airdrop|whale|hodl|diamond.?hands|paper.?hands|rug|pull)\b",
        ],
        "current_event": [
            r"\b(breaking|news|alert|urgent|developing|just.?in)\b",
            r"\b(hack|exploit|vulnerability|audit|security|bug|patch)\b",
            r"\b(launch|release|mainnet|testnet|beta|alpha|announcement)\b",
            r"\b(regulation|sec|cftc|law|legal|compliance|license)\b",
        ],
        "absurdist": [
            r"\b(schizo|delulu|cope|seethe|ngmi|wagmi|hfsp|lfgg)\b",
            r"\b(meme|memeable|funny|hilarious|absurd|surreal|bizarre)\b",
            r"\b(number|go.?up|go.?down|pump|dump|moon|crash|rekt)\b",
        ],
        "community": [
            r"\b(community|family|gang|army|squad|team|together|united)\b",
            r"\b(holder|diamond.?hands|strong.?hands|conviction|believer)\b",
            r"\b(telegram|discord|twitter|x.?com|reddit|forum)\b",
        ],
        "copycat": [
            r"\b(copy|clone|fork|rip.?off|knockoff|imitation|derivative)\b",
            r"\b(similar|same|identical|exact|duplicate|replica)\b",
            r"\b(v2|v3|next|new|improved|better|original|og)\b",
        ],
        "unknown": [],
    }
    
    # Suspicious patterns for risk
    SUSPICIOUS_PATTERNS = [
        r"(rug|scam|honeypot|fake|phish|drain|steal|exploit)",
        r"(guaranteed|risk.?free|100x|1000x|moon|guaranteed)",
        r"(insider|dev.?wallet|team.?wallet|unlocked|unvested)",
    ]
    
    # Compiled regex cache
    _compiled: Dict[str, List[re.Pattern]] = {}
    
    @classmethod
    def get_patterns(cls, category: str) -> List[re.Pattern]:
        if category not in cls._compiled:
            patterns = cls.PATTERNS.get(category, [])
            cls._compiled[category] = [re.compile(p, re.IGNORECASE) for p in patterns]
        return cls._compiled[category]
    
    @classmethod
    def get_suspicious_patterns(cls) -> List[re.Pattern]:
        if "suspicious" not in cls._compiled:
            cls._compiled["suspicious"] = [re.compile(p, re.IGNORECASE) for p in cls.SUSPICIOUS_PATTERNS]
        return cls._compiled["suspicious"]


@dataclass
class NarrativeSignal:
    """Narrative signal output."""
    category: str = "unknown"
    score: float = 0.0  # 0-100
    confidence: float = 0.0  # 0-1
    reasons: List[str] = field(default_factory=list)
    sources_observed: int = 0
    unique_sources: int = 0
    mentions_total: int = 0
    mentions_delta: float = 0.0
    unique_sources_delta: float = 0.0
    engagement_delta: float = 0.0
    observation_window_seconds: float = 0.0
    missing_data: bool = False
    unknown_state: bool = False


class NarrativeEngine:
    """
    Zero-cost narrative detection using lawful public data sources.
    
    LLM is used only for classification/summarization, never for trade authorization.
    """
    
    def __init__(
        self,
        llm_classifier: Optional[Any] = None,
        max_lookback_hours: int = 24,
    ):
        self.llm_classifier = llm_classifier
        self.max_lookback_hours = max_lookback_hours
        
        # Track historical snapshots for velocity
        self._history: Dict[str, List[Dict]] = defaultdict(list)
        self.max_history_size = 100
    
    def analyze(
        self,
        candidate_mint: str,
        discovery_data: Dict[str, Any],
        enrichment_data: Optional[Dict] = None,
        social_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Analyze narrative signals for a token.
        
        Args:
            candidate_mint: Token mint address
            discovery_data: Discovery event data (symbol, name, uri, etc.)
            enrichment_data: On-chain enrichment data
            social_data: Optional social media data (if available)
            
        Returns:
            Narrative signal dictionary
        """
        signal = self._analyze_text_signals(discovery_data, enrichment_data)
        
        # Track historical snapshot for velocity
        snapshot = {
            "timestamp": time.time(),
            "category": signal.get("category", "unknown"),
            "score": signal.get("score", 0.0),
            "confidence": signal.get("confidence", 0.0),
        }
        
        self._history[candidate_mint].append(snapshot)
        if len(self._history[candidate_mint]) > 100:
            self._history[candidate_mint].pop(0)
        
        # Calculate velocity
        velocity = self._calculate_velocity(candidate_mint, signal)
        signal["mentions_delta"] = velocity.get("mentions_delta", 0.0)
        signal["unique_sources_delta"] = velocity.get("unique_sources_delta", 0.0)
        signal["engagement_delta"] = velocity.get("engagement_delta", 0.0)
        
        # Check for copycat
        copycat_score = self._check_copycat(discovery_data)
        if copycat_score > 0.5:
            signal["category"] = "copycat"
            signal["reasons"].append("copycat_detected")
        
        # Check for suspicious patterns
        suspicious = self._check_suspicious(discovery_data)
        if suspicious:
            signal["reasons"].append("suspicious_patterns_detected")
            signal["score"] = max(0, signal.get("score", 0) - 20)
        
        # Determine missing data state
        signal["missing_data"] = self._check_missing_data(discovery_data)
        signal["unknown_state"] = signal.get("score", 0) == 0 and not signal.get("reasons")
        
        return signal
    
    def _analyze_text_signals(
        self,
        discovery_data: Dict,
        enrichment_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Extract narrative signals from text metadata."""
        signal = {
            "category": "unknown",
            "score": 0.0,
            "confidence": 0.0,
            "reasons": [],
            "sources_observed": 0,
            "unique_sources": 0,
            "mentions_total": 0,
            "missing_data": False,
            "unknown_state": True,
        }
        
        # Collect all text sources
        texts = []
        sources = []
        
        # Discovery metadata
        for field in ["name", "symbol", "uri", "creator"]:
            value = discovery_data.get(field) if isinstance(discovery_data, dict) else None
            if value:
                texts.append(str(value))
                sources.append(f"discovery_{field}")
        
        # Enrichment data (if available)
        if enrichment_data:
            for field in ["name", "symbol", "description"]:
                value = enrichment_data.get(field)
                if value:
                    texts.append(str(value))
                    sources.append(f"enrichment_{field}")
        
        if not texts:
            return {"category": "unknown", "score": 0.0, "confidence": 0.0, "reasons": ["no_text_data"], "missing_data": True}
        
        # Combine all text
        combined_text = " ".join(texts).lower()
        
        # Score each category
        category_scores = {}
        all_reasons = []
        
        categories = [
            "animal_meme", "political", "celebrity", "ai", "gaming",
            "crypto_native", "current_event", "absurdist", "community", "copycat"
        ]
        
        for category in categories:
            patterns = NarrativeCategoryKeywords.get_patterns(category)
            matches = 0
            matched_terms = []
            for pattern in patterns:
                matches_found = pattern.findall(combined_text)
                if matches_found:
                    matches += len(matches_found)
                    matched_terms.extend(matches_found)
            
            if matches > 0:
                # Score based on match count and uniqueness
                score = min(matches * 15, 80)
                unique_terms = len(set(matched_terms))
                if unique_terms > 2:
                    score += 10
                
                category_scores[category] = min(score, 100)
                if matches > 0:
                    all_reasons.append(f"{category}:{matches}_matches")
        
        # Determine best category
        if category_scores:
            best_category = max(category_scores, key=category_scores.get)
            best_score = category_scores[best_category]
            
            # Normalize confidence based on match strength
            confidence = min(1.0, category_scores[best_category] / 80.0)
            
            return {
                "category": best_category,
                "score": min(category_scores[best_category], 100),
                "confidence": confidence,
                "reasons": [f"matched_{cat}:{score}" for cat, score in category_scores.items() if score > 0],
                "sources_observed": 1,
                "unique_sources": 1,
                "mentions_total": sum(1 for _ in combined_text.split() if _),
                "missing_data": False,
                "unknown_state": False,
            }
        
        return {"category": "unknown", "score": 0.0, "confidence": 0.0, "reasons": ["no_narrative_matches"], "missing_data": True}
    
    def _calculate_velocity(self, mint: str, signal: Dict) -> Dict[str, float]:
        """Calculate narrative velocity from historical snapshots."""
        history = self._history.get(mint, [])
        if len(history) < 2:
            return {"mentions_delta": 0.0, "unique_sources_delta": 0.0, "engagement_delta": 0.0}
        
        # Compare current with previous snapshot
        current = {"score": signal.get("score", 0), "confidence": signal.get("confidence", 0)}
        previous = history[-2] if len(history) >= 2 else history[-1]
        
        score_delta = current.get("score", 0) - previous.get("score", 0)
        confidence_delta = current.get("confidence", 0) - previous.get("confidence", 0)
        
        # Time delta
        time_delta = time.time() - previous.get("timestamp", time.time())
        if time_delta > 0:
            score_velocity = score_delta / (time_delta / 3600)  # per hour
        else:
            score_velocity = 0
        
        return {
            "mentions_delta": score_velocity,
            "unique_sources_delta": confidence_delta / max(time_delta / 3600, 1),
            "engagement_delta": score_velocity * 0.5,
        }
    
    def _check_copycat(self, discovery_data: Dict) -> float:
        """Check for copycat patterns."""
        if not discovery_data:
            return 0.0
        
        score = 0.0
        text = " ".join([
            str(discovery_data.get("name", "")),
            str(discovery_data.get("symbol", "")),
            str(discovery_data.get("uri", "")),
        ]).lower()
        
        copycat_patterns = NarrativeCategoryKeywords.get_patterns("copycat")
        for pattern in copycat_patterns:
            if pattern.search(text):
                return 1.0
        
        return 0.0
    
    def _check_suspicious(self, discovery_data: Dict) -> bool:
        """Check for suspicious patterns in token metadata."""
        if not discovery_data:
            return False
        
        text = " ".join([
            str(discovery_data.get("name", "")),
            str(discovery_data.get("symbol", "")),
            str(discovery_data.get("uri", "")),
            str(discovery_data.get("name", "")),
        ]).lower()
        
        for pattern in NarrativeCategoryKeywords.get_suspicious_patterns():
            if pattern.search(text):
                return True
        return False
    
    def _check_missing_data(self, discovery_data: Dict) -> bool:
        """Check if critical metadata is missing."""
        if not discovery_data:
            return True
        
        required_fields = ["name", "symbol", "uri", "creator"]
        missing = sum(1 for field in ["name", "symbol", "uri", "creator"] 
                     if not discovery_data.get(field))
        return missing >= 2


# LLM-based classification (optional, for enhanced accuracy)
class LLMNarrativeClassifier:
    """
    Optional LLM-based narrative classifier.
    
    Used only for classification/summarization.
    NEVER directly authorizes trades.
    """
    
    def __init__(self, model: str = "local", prompt_template: str = None):
        self.model = model
        self.prompt_template = prompt_template or self._default_prompt()
    
    def _default_prompt(self) -> str:
        return """Classify the narrative category of this crypto token based on its metadata.

Token: {symbol} ({name})
URI: {uri}
Creator: {creator}

Categories: animal_meme, political, celebrity, ai, gaming, crypto_native, current_event, absurdist, community, copycat, unknown

Respond with JSON: {"category": "...", "confidence": 0.0, "reasoning": "..."}
Only use the categories listed above. Be conservative with confidence."""
    
    def classify(self, discovery_data: Dict) -> Dict[str, Any]:
        """Classify using LLM (placeholder - requires actual LLM integration)."""
        # This is a placeholder - actual implementation would call local or API LLM
        # For now, return deterministic fallback
        return {
            "category": "unknown",
            "confidence": 0.0,
            "reasoning": "LLM not configured"
        }


def create_narrative_engine(config: Optional[Dict] = None) -> 'NarrativeEngine':
    """Factory to create configured NarrativeEngine."""
    config = config or {}
    return NarrativeEngine(
        llm_classifier=None,  # Set to LLMNarrativeClassifier() if LLM available
        max_lookback_hours=config.get("max_lookback_hours", 24),
    )