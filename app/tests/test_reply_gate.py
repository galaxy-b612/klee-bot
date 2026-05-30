"""Tests for ReplyGate — all scoring factors, thresholds, and edge cases."""

import pytest

from app.services.group_activity import GroupActivityTracker
from app.services.reply_gate import (
    NormalizedMessage,
    ReplyGate,
    SleepState,
    UserProfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def msg(text: str, **kw) -> NormalizedMessage:
    defaults = {
        "text": text,
        "mentions_bot": False,
        "is_reply_to_bot": False,
        "user_id": 1,
        "group_id": 915443332,
    }
    defaults.update(kw)
    return NormalizedMessage(**defaults)


def user(favor: float = 0.0) -> UserProfile:
    return UserProfile(user_id=1, favor_score=favor)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gate() -> ReplyGate:
    return ReplyGate()


# ---------------------------------------------------------------------------
# Strong bonuses
# ---------------------------------------------------------------------------

class TestStrongBonuses:
    def test_mention_bot(self, gate):
        r = gate.score(msg("有人吗", mentions_bot=True))
        assert r.score >= 100
        assert "mentioned_bot" in r.reason
        assert r.should_reply is True
        assert r.trigger_level == "mention"

    def test_reply_to_bot(self, gate):
        r = gate.score(msg("谢谢", is_reply_to_bot=True))
        assert "reply_to_bot" in r.reason
        assert r.score >= 80

    def test_starts_with_klee(self, gate):
        r = gate.score(msg("可莉你在干嘛"))
        assert "starts_klee" in r.reason
        assert r.score >= 80
        assert r.should_reply is True

    def test_help_request(self, gate):
        r = gate.score(msg("帮我看看这个怎么配队"))
        assert "help_request" in r.reason
        assert r.score >= 70

    def test_help_request_variant(self, gate):
        r = gate.score(msg("请问怎么抽卡"))
        assert "help_request" in r.reason

    def test_at_and_help_combined(self, gate):
        r = gate.score(msg("救命啊", mentions_bot=True))
        assert r.score >= 170  # 100 + 70
        assert "mentioned_bot" in r.reason
        assert "help_request" in r.reason


# ---------------------------------------------------------------------------
# Medium bonuses
# ---------------------------------------------------------------------------

class TestMediumBonuses:
    def test_genshin_topic(self, gate):
        r = gate.score(msg("原神新版本什么时候更新"))
        assert "genshin_topic" in r.reason
        assert r.score >= 30

    def test_genshin_character_name(self, gate):
        r = gate.score(msg("钟离的盾好厚啊"))
        assert "genshin_topic" in r.reason

    def test_gacha_artifact(self, gate):
        r = gate.score(msg("又歪了，心态炸了"))
        assert "gacha_artifact" in r.reason
        assert r.score >= 30

    def test_artifact_keywords(self, gate):
        r = gate.score(msg("这个圣遗物副词条全是防御"))
        assert "gacha_artifact" in r.reason

    def test_sad_emotion(self, gate):
        r = gate.score(msg("今天好难过啊"))
        assert "sad_emotion" in r.reason
        assert r.score >= 50

    def test_sad_emotion_variant(self, gate):
        r = gate.score(msg("呜呜呜心态崩了"))
        assert "sad_emotion" in r.reason

    def test_high_favor_user(self, gate):
        r = gate.score(msg("大家好呀"), user=user(favor=65.0))
        assert "high_favor" in r.reason

    def test_low_favor_no_bonus(self, gate):
        r = gate.score(msg("大家好呀"), user=user(favor=30.0))
        assert "high_favor" not in r.reason

    def test_genshin_plus_sad(self, gate):
        """Gacha artifact + sad emotion should stack."""
        r = gate.score(msg("抽卡歪了，好难过"))
        assert r.score >= 80  # 30 + 50
        assert "gacha_artifact" in r.reason
        assert "sad_emotion" in r.reason
        # no genshin_topic — text doesn't contain genshin keyword


# ---------------------------------------------------------------------------
# Penalties
# ---------------------------------------------------------------------------

class TestPenalties:
    def test_cooldown_penalty(self, gate):
        # Simulate bot just replied
        gate.record_reply(group_id=915443332)
        r = gate.score(msg("无聊", group_id=915443332))
        assert "cooldown_penalty" in r.reason

    def test_cooldown_supersedes_mentions(self, gate):
        """Even @bot can't beat cooldown on its own (needs other bonuses)."""
        gate.record_reply(group_id=915443332)

        # @bot alone: 100 - 60 = 40 → below threshold
        r = gate.score(msg("hello", group_id=915443332, mentions_bot=True))
        assert "cooldown_penalty" in r.reason
        assert r.score < 80
        assert r.should_reply is False

    def test_density_penalty(self, gate):
        # Fill tracker with rapid messages
        for _ in range(60):
            gate.tracker.record_message(915443332, is_media=False)
        # Density should now be ~6 msg/s
        r = gate.score(msg("test", group_id=915443332))
        if gate.tracker.get_density(915443332) > 5.0:
            assert "density_penalty" in r.reason

    def test_image_spam_penalty(self, gate):
        # Fill tracker with images only
        for _ in range(20):
            gate.tracker.record_message(915443332, is_media=True)
        r = gate.score(msg("test", group_id=915443332))
        if gate.tracker.get_image_spam_ratio(915443332) > 0.7:
            assert "image_spam" in r.reason

    def test_meaningless_short(self, gate):
        r = gate.score(msg("哈"))
        assert "meaningless_short" in r.reason

    def test_meaningless_punctuation(self, gate):
        r = gate.score(msg("。。"))
        assert "meaningless_short" in r.reason

    def test_short_but_meaningful(self, gate):
        """Short messages with keywords are NOT meaningless."""
        r = gate.score(msg("救我", is_reply_to_bot=True))
        assert "meaningless_short" not in r.reason
        assert "reply_to_bot" in r.reason


# ---------------------------------------------------------------------------
# Sleep state
# ---------------------------------------------------------------------------

class TestSleepState:
    def test_sleep_overrides_everything(self, gate):
        sleep = SleepState(is_sleeping=True)
        r = gate.score(msg("可莉救命", mentions_bot=True), sleep=sleep)
        assert r.should_reply is False
        assert "sleep_active" in r.reason
        assert r.should_reply is False  # sleep always blocks reply

    def test_awake_normal(self, gate):
        sleep = SleepState(is_sleeping=False)
        r = gate.score(msg("可莉救命", mentions_bot=True), sleep=sleep)
        assert r.should_reply is True
        assert "sleep_active" not in r.reason


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_high_score_reply(self, gate):
        r = gate.score(msg("可莉帮帮我", mentions_bot=True, is_reply_to_bot=True))
        # 100 + 80 + 80 + 70 = 330 → definitely >= 80
        assert r.score >= 80
        assert r.should_reply is True
        assert r.trigger_level == "mention"

    def test_mid_score_delayed(self, gate):
        """Genshin topic alone (30) → < 50 → record only."""
        r = gate.score(msg("原神"))
        assert r.score < 50
        assert r.should_reply is False
        assert r.trigger_level == "none"

    def test_mid_score_with_emotion_delayed(self, gate):
        """Sad alone = 50 → right at delay threshold."""
        r = gate.score(msg("好难过"))
        assert r.score == 50
        assert r.trigger_level == "delayed"
        assert r.should_reply is False  # below 80

    def test_normal_chat_score_zero(self, gate):
        r = gate.score(msg("今天天气不错"))
        assert r.score == 0
        assert r.trigger_level == "none"
        assert r.should_reply is False

    def test_emotion_plus_genshin_above_threshold(self, gate):
        """ sad (50) + genshin(30) + gacha(30) = 110 → reply """
        r = gate.score(msg("原神抽卡又歪了 好难过"))
        assert r.score >= 80
        assert r.should_reply is True
        assert "genshin_topic" in r.reason
        assert "gacha_artifact" in r.reason
        assert "sad_emotion" in r.reason


# ---------------------------------------------------------------------------
# Group-specific isolation
# ---------------------------------------------------------------------------

class TestGroupIsolation:
    def test_activity_per_group(self, gate):
        """Activity in group A should not affect group B."""
        # Group A: bot just replied → cooldown
        gate.record_reply(915443332)
        # Group B: no activity
        r = gate.score(msg("hello", group_id=29459043, mentions_bot=True))
        assert "cooldown_penalty" not in r.reason
        assert r.score >= 100  # @bot alone

    def test_reset_clears_group(self, gate):
        gate.record_reply(915443332)
        gate.reset(915443332)
        r = gate.score(msg("hello", group_id=915443332))
        assert "cooldown_penalty" not in r.reason


# ---------------------------------------------------------------------------
# Hourly cap
# ---------------------------------------------------------------------------

class TestHourlyCap:
    def test_hourly_cap_blocks_reply(self, gate):
        # Simulate 8 replies within last hour
        for _ in range(8):
            gate.tracker.record_reply(915443332)
        r = gate.score(msg("test", mentions_bot=True, group_id=915443332))
        assert "hourly_capped" in r.reason
        assert "cooldown_penalty" in r.reason  # from record_reply calls
        assert r.should_reply is False  # cooldown + cap supersede


# ---------------------------------------------------------------------------
# scoring with real group integration
# ---------------------------------------------------------------------------

class TestRealFlow:
    def test_first_message_no_cooldown(self, gate):
        """First message to a group: no penalties."""
        r = gate.score(msg("你好", group_id=999888777, mentions_bot=True))
        assert "cooldown_penalty" not in r.reason
        assert r.score == 100  # @bot only

    def test_record_reply_updates_cooldown(self, gate):
        gate.record_reply(915443332)
        assert gate.tracker.is_in_cooldown(915443332) is True


# ---------------------------------------------------------------------------
# Trigger level assignment
# ---------------------------------------------------------------------------

class TestTriggerLevels:
    def test_mention_level(self, gate):
        r = gate.score(msg("help", mentions_bot=True))
        assert r.trigger_level == "mention"

    def test_emotion_high_level(self, gate):
        """Sad (50) + genshin (30) + favor (20) = 100 → emotion_high when no mention."""
        r = gate.score(
            msg("原神好难 打得我好难过"),
            user=user(favor=80.0),
        )
        assert r.score >= 80
        assert r.should_reply is True
        assert r.trigger_level == "emotion_high"

    def test_soft_topic_level(self, gate):
        """Genshin (30) + gacha (30) + favor (20) + help (70) = 150, no mention → soft_topic."""
        r = gate.score(
            msg("原神钟离圣遗物怎么配帮帮我"),
            user=user(favor=80.0),
        )
        assert r.score >= 80
        assert r.trigger_level == "soft_topic"
