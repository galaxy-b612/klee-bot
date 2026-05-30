"""Intent Router — determines whether a message is a plugin command.

Matches normalized message text against configured regex patterns and
bq.json keyword set. Supports @mention-gated rules for plugins that
require the bot to be explicitly addressed.

Priority order:
  1) bq.json keyword exact-match set (O(1) lookup)
  2) bq.json prefix match (space-delimited, for text-variable memes)
  3) Regex patterns (in routes.yaml order), with require_mention gating
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import CommandRule, get_config

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """Result of intent detection on a message."""
    is_command: bool
    primary_handler: str          # "astrbot" | "yunzai" | "hermes" | "klee_core" | "none"
    matched_pattern: str | None
    reason: str


class IntentRouter:
    """Compiles command patterns and bq.json keywords for fast matching.

    Patterns are loaded from routes.yaml. The special pattern "@bq_keywords"
    triggers loading the earth-k-plugin bq.json keyword list into a set for
    exact-match and prefix-match lookups.
    """

    # Default bq.json path on the production server
    DEFAULT_BQ_PATH = "/root/Yunzai/plugins/earth-k-plugin/resources/bq.json"

    def __init__(self, rules: list[CommandRule] | None = None):
        """Initialize with optional rule override (for testing).

        Args:
            rules: Command rules. If None, loaded from config.
        """
        if rules is None:
            rules = get_config().commands

        # bq.json keyword set — loaded once, exact-match O(1)
        self._keyword_set: frozenset[str] = frozenset()

        # Regex rules stored as (pattern, handler, require_mention) tuples
        self._regex_rules: list[tuple[re.Pattern[str], str, bool]] = []

        for rule in rules:
            if rule.pattern == "@bq_keywords":
                self._keyword_set = self._load_bq_keywords()
            else:
                try:
                    compiled = re.compile(rule.pattern)
                    self._regex_rules.append(
                        (compiled, rule.handler, rule.require_mention)
                    )
                except re.error as exc:
                    logger.warning(
                        "Invalid regex pattern '%s': %s — skipping", rule.pattern, exc
                    )

        logger.info(
            "IntentRouter: %d regex rules, %d bq keywords loaded",
            len(self._regex_rules), len(self._keyword_set),
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, text: str, mentions_bot: bool = False) -> IntentResult:
        """Run intent detection on normalized message text.

        Args:
            text: Normalized message content (no @mentions, trimmed).
            mentions_bot: Whether the bot was @mentioned in the original message.

        Returns:
            IntentResult with is_command flag and matched handler.
        """
        text = text.strip()
        if not text:
            return IntentResult(
                is_command=False, primary_handler="none",
                matched_pattern=None, reason="empty_text",
            )

        # P2: bq.json exact keyword match (O(1) set lookup)
        if text in self._keyword_set:
            return IntentResult(
                is_command=True, primary_handler="yunzai",
                matched_pattern="@bq_keywords", reason="matched_bq_keyword",
            )

        # P2.1: bq.json prefix match — supports text-variable memes
        # earth-k-plugin memes accept variable text after the keyword,
        # e.g. "可莉举牌 你好" where "可莉举牌" is the trigger and
        # "你好" is the text rendered on the image.
        # Split on first whitespace; if the leading token is a known
        # bq keyword, route to Yunzai so the plugin can parse the
        # variable text from the remainder.
        parts = text.split(None, 1)
        if len(parts) > 1 and parts[0] in self._keyword_set:
            logger.debug(
                "Intent: bq prefix match keyword='%s' remainder='%s'",
                parts[0], parts[1][:30],
            )
            return IntentResult(
                is_command=True, primary_handler="yunzai",
                matched_pattern="@bq_keywords", reason="matched_bq_prefix",
            )

        # P1, P3-P10: regex patterns (in priority order)
        for pattern, handler, require_mention in self._regex_rules:
            # Skip @mention-gated rules when bot wasn't mentioned
            if require_mention and not mentions_bot:
                continue
            if pattern.search(text):
                logger.debug(
                    "Intent: matched pattern='%s' handler='%s' text='%s'",
                    pattern.pattern, handler, text[:50],
                )
                return IntentResult(
                    is_command=True, primary_handler=handler,
                    matched_pattern=pattern.pattern, reason="matched_command",
                )

        # No match
        return IntentResult(
            is_command=False, primary_handler="none",
            matched_pattern=None, reason="no_match",
        )

    # ------------------------------------------------------------------
    # bq.json keyword loading
    # ------------------------------------------------------------------

    def _load_bq_keywords(self, bq_path: str | None = None) -> frozenset[str]:
        """Load user-facing keywords from earth-k-plugin's bq.json.

        Each entry in bq.json has a 'keywords' array containing the
        Chinese trigger words users actually type (e.g. "加速", "抱", "吃").

        Args:
            bq_path: Path to bq.json. Defaults to production server path.

        Returns:
            Frozenset of unique keywords for O(1) exact-match lookups.
        """
        path = bq_path or self.DEFAULT_BQ_PATH

        # Allow override via env var
        env_path = os.environ.get("KLEE_CORE_BQ_PATH")
        if env_path:
            path = env_path

        if not os.path.isfile(path):
            logger.warning(
                "bq.json not found at '%s' — bq keyword routing disabled. "
                "Set KLEE_CORE_BQ_PATH env var to override.", path,
            )
            return frozenset()

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load bq.json from '%s': %s — skipping", path, exc)
            return frozenset()

        keywords: set[str] = set()
        for entry in data.values():
            if isinstance(entry, dict) and "keywords" in entry:
                for kw in entry["keywords"]:
                    if kw and isinstance(kw, str):
                        keywords.add(kw.strip())

        logger.info("Loaded %d keywords from bq.json (%s)", len(keywords), path)
        return frozenset(keywords)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pattern_count(self) -> int:
        return len(self._regex_rules)

    @property
    def keyword_count(self) -> int:
        return len(self._keyword_set)
