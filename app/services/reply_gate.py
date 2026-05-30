"""Reply Gate — rule-based scoring engine for Hermes conversation decisions.

Determines whether the bot should reply to a normal chat message using
a weighted scoring system. NO LLM calls — pure rule-based.

Flow:
    ReplyGate.score(message, group_profile, user_profile, sleep) → ReplyGateResult
    ReplyGateResult.score >= 80 → route to Hermes
    ReplyGateResult.score 50–79  → create delayed reply task
    ReplyGateResult.score < 50   → record only
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.services.group_activity import GroupActivityTracker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class NormalizedMessage:
    """Input: normalized message content."""
    text: str
    mentions_bot: bool
    is_reply_to_bot: bool = False
    user_id: int = 0
    group_id: int = 0


@dataclass
class UserProfile:
    """Input: user's relationship with the bot."""
    user_id: int = 0
    favor_score: float = 0.0       # 0–100, from affection system


@dataclass
class SleepState:
    """Input: bot sleep schedule (placeholder for SleepManager)."""
    is_sleeping: bool = False
    sleep_start: str = "23:00"
    sleep_end: str = "07:30"


@dataclass
class ReplyGateResult:
    """Output: scoring decision with trigger level and reasons."""
    should_reply: bool
    score: int
    trigger_level: str           # "mention" | "emotion_high" | "soft_topic" | "none"
    reason: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Keyword sets (compile once)
# ---------------------------------------------------------------------------

# Genshin-related keywords
_GENSHIN_KEYWORDS = re.compile(
    r"原神|提瓦特|蒙德|璃月|稻妻|须弥|枫丹|纳塔|至冬|"
    r"深渊|七圣召唤|七神|神之眼|神之心|"
    r"可莉|钟离|绫华|雷神|万叶|纳西妲|芙宁娜|仆人|克洛琳德|"
    r"迪卢克|刻晴|甘雨|胡桃|夜兰|妮露|艾尔海森|赛诺|"
    r"温迪|公子|魈|申鹤|神子|心海|散兵"
)

_GACHA_ARTIFACT_KEYWORDS = re.compile(
    r"抽卡|十连|歪了|保底|小保底|大保底|"
    r"圣遗物|副词条|主词条|强化|双爆|"
    r"突破|天赋|培养|练度|面板|命座|"
    r"专武|武器池|常驻|限定"
)

_SAD_EMOTION_KEYWORDS = re.compile(
    r"(好|很|真|太)(难过|伤心|累|烦|郁闷|崩溃|痛苦|绝望|emo)|"
    r"哭(了|死|泣)|呜呜|QAQ|T_T|"
    r"不想(上班|上学|活了|动)|"
    r"(气死|烦死|累死|困死)我了|"
    r"心态(崩|炸|爆炸)|破防|"
    r"完全没(心情|状态|动力)"
)

_HELP_REQUEST_KEYWORDS = re.compile(
    r"帮(我|忙|一下)|救命|求助|"
    r"怎么(办|做|弄|打|配)|如何|"
    r"请问|问一下|谁知道|有没有人|"
    r"(推荐|建议|指导)(一下)?"
)

_MEANINGLESS_SHORT = re.compile(
    r"^[。，！？…\.\,!?\s]*$|"       # pure punctuation
    r"^[哈嘿哼嗯哦啊唉啧]{1,3}$|"    # interjections only
    r"^[6６]{1,3}$|"                 # 666
    r"^[?？]{1,3}$"                  # pure questions marks
)


# ---------------------------------------------------------------------------
# ReplyGate
# ---------------------------------------------------------------------------

class ReplyGate:
    """Rule-based scoring engine for Hermes reply decisions.

    Scoring factors (all configurable):
        Strong bonuses:
            @机器人                     +100
            回复机器人上一条消息          +80
            句子开头是"可莉"             +80
            明确请求帮助                 +70

        Medium bonuses:
            原神相关话题                 +30
            抽卡/圣遗物/角色培养         +30
            情绪低落                    +50
            高好感用户主动对话           +20

        Penalties:
            机器人刚回复过 (30s内)       -60
            群消息密度过高 (>5 msg/s)    -40
            连续图片/表情刷屏            -40
            消息太短且无意义             -30
            处于睡眠状态                 (由 SleepManager 接管)

    Thresholds:
        >= 80:   should_reply = True
        50–79:   delayed reply task
        < 50:    record only
    """

    # ── Scoring weights (configurable) ──

    AT_BOT = 100
    REPLY_TO_BOT = 80
    STARTS_WITH_KLEE = 80
    HELP_REQUEST = 70

    GENSHIN_TOPIC = 30
    GACHA_ARTIFACT = 30
    SAD_EMOTION = 50
    HIGH_FAVOR = 20

    COOLDOWN_PENALTY = -60
    DENSITY_PENALTY = -40
    IMAGE_SPAM_PENALTY = -40
    MEANINGLESS_PENALTY = -30

    # ── Thresholds ──

    REPLY_THRESHOLD = 80
    DELAY_THRESHOLD = 50

    # ── Cooldown / rate-limit params ──

    COOLDOWN_SECONDS = 30.0
    MAX_HOURLY_REPLIES = 8
    DENSITY_THRESHOLD = 5.0          # messages/sec
    IMAGE_SPAM_THRESHOLD = 0.7       # 70% media ratio
    FAVOR_THRESHOLD = 60.0           # minimum favor for HIGH_FAVOR bonus
    SHORT_MSG_MAX_LEN = 2            # text length threshold for meaningless

    def __init__(self, tracker: GroupActivityTracker | None = None):
        self.tracker = tracker or GroupActivityTracker()

    # ── Public API ───────────────────────────────────────────

    def score(
        self,
        message: NormalizedMessage,
        user: UserProfile | None = None,
        sleep: SleepState | None = None,
    ) -> ReplyGateResult:
        """Score a message and return the reply decision.

        Args:
            message: Normalized message content.
            user: User profile (optional — defaults to neutral).
            sleep: Sleep state (optional — defaults to awake).

        Returns:
            ReplyGateResult with score, trigger_level, and reasons.
        """
        user = user or UserProfile()
        sleep = sleep or SleepState()

        reasons: list[str] = []
        score = 0

        # ── Strong bonuses ──

        if message.mentions_bot:
            score += self.AT_BOT
            reasons.append("mentioned_bot")

        if message.is_reply_to_bot:
            score += self.REPLY_TO_BOT
            reasons.append("reply_to_bot")

        if message.text.startswith("可莉"):
            score += self.STARTS_WITH_KLEE
            reasons.append("starts_klee")

        if _HELP_REQUEST_KEYWORDS.search(message.text):
            score += self.HELP_REQUEST
            reasons.append("help_request")

        # ── Medium bonuses ──

        if _GENSHIN_KEYWORDS.search(message.text):
            score += self.GENSHIN_TOPIC
            reasons.append("genshin_topic")

        if _GACHA_ARTIFACT_KEYWORDS.search(message.text):
            score += self.GACHA_ARTIFACT
            reasons.append("gacha_artifact")

        if _SAD_EMOTION_KEYWORDS.search(message.text):
            score += self.SAD_EMOTION
            reasons.append("sad_emotion")

        if user.favor_score >= self.FAVOR_THRESHOLD:
            score += self.HIGH_FAVOR
            reasons.append("high_favor")

        # ── Penalties ──

        if self.tracker.is_in_cooldown(message.group_id, self.COOLDOWN_SECONDS):
            score += self.COOLDOWN_PENALTY
            reasons.append("cooldown_penalty")

        density = self.tracker.get_density(message.group_id)
        if density > self.DENSITY_THRESHOLD:
            score += self.DENSITY_PENALTY
            reasons.append("density_penalty")

        image_ratio = self.tracker.get_image_spam_ratio(message.group_id)
        if image_ratio > self.IMAGE_SPAM_THRESHOLD:
            score += self.IMAGE_SPAM_PENALTY
            reasons.append("image_spam")

        if len(message.text.strip()) <= self.SHORT_MSG_MAX_LEN:
            if _MEANINGLESS_SHORT.search(message.text):
                score += self.MEANINGLESS_PENALTY
                reasons.append("meaningless_short")

        # ── Sleep state check (placeholder) ──

        if sleep.is_sleeping:
            reasons.append("sleep_active")
            score -= 200  # heavily penalize — sleep manager should override
            trigger = "none"
            should_reply = False
            return ReplyGateResult(
                should_reply=False, score=score,
                trigger_level=trigger, reason=reasons,
            )

        # ── Hourly cap ──

        if self.tracker.is_hourly_capped(message.group_id, self.MAX_HOURLY_REPLIES):
            reasons.append("hourly_capped")
            score -= 50

        # ── Determine trigger level ──

        if "mentioned_bot" in reasons:
            trigger_level = "mention"
        elif "sad_emotion" in reasons and score >= self.REPLY_THRESHOLD:
            trigger_level = "emotion_high"
        elif score >= self.REPLY_THRESHOLD:
            trigger_level = "soft_topic"
        elif score >= self.DELAY_THRESHOLD and score < self.REPLY_THRESHOLD:
            trigger_level = "delayed"
        else:
            trigger_level = "none"

        # ── Final decision ──

        should_reply = score >= self.REPLY_THRESHOLD

        return ReplyGateResult(
            should_reply=should_reply,
            score=score,
            trigger_level=trigger_level,
            reason=reasons,
        )

    # ── Convenience ──────────────────────────────────────────

    def record_message(self, group_id: int, is_media: bool = False):
        """Record an incoming message for activity tracking."""
        self.tracker.record_message(group_id, is_media)

    def record_reply(self, group_id: int):
        """Record a bot reply for cooldown and hourly cap tracking."""
        self.tracker.record_reply(group_id)

    def reset(self, group_id: int | None = None):
        """Reset tracker state (for tests)."""
        self.tracker.reset(group_id)
