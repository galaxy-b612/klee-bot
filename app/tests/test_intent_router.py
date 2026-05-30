"""Unit tests for IntentRouter — command pattern matching with bq.json keywords.

Covers: regex patterns, bq.json keyword set, @mention gating, priority order.
"""

import pytest

from app.config import CommandRule
from app.services.intent_router import IntentResult, IntentRouter


# ---------------------------------------------------------------------------
# Realistic bq.json sample for tests
# ---------------------------------------------------------------------------

SAMPLE_BQ = {
    "accelerate": {"keywords": ["加速"]},
    "abstinence": {"keywords": ["戒导"]},
    "hug": {"keywords": ["抱"]},
    "eat": {"keywords": ["吃"]},
    "sleep": {"keywords": ["睡不着"]},
    "multi": {"keywords": ["P5预告信", "预告信"]},
}
SAMPLE_BQ_JSON = """{"accelerate":{"keywords":["加速"]},"abstinence":{"keywords":["戒导"]},"hug":{"keywords":["抱"]},"eat":{"keywords":["吃"]},"sleep":{"keywords":["睡不着"]},"multi":{"keywords":["P5预告信","预告信"]}}"""

# Expected keyword set
EXPECTED_KW = frozenset({"加速", "戒导", "抱", "吃", "睡不着", "P5预告信", "预告信"})


# ---------------------------------------------------------------------------
# Full rule set matching production routes.yaml
# ---------------------------------------------------------------------------

TEST_RULES = [
    # P1: Yunzai # prefix
    CommandRule(pattern=r"^#", handler="yunzai"),
    # P2: bq.json keywords (handled separately in init)
    CommandRule(pattern="@bq_keywords", handler="yunzai"),
    # P3: AstrBot /-prefixed
    CommandRule(pattern=r"^/", handler="astrbot"),
    # P4: Yunzai non-# exact
    CommandRule(pattern=r"^(猜语音|原神猜语)$", handler="yunzai"),
    # P5: Yunzai parameterized
    CommandRule(pattern=r"^点歌", handler="yunzai"),
    # P6: Yunzai other
    CommandRule(pattern=r"^(娶群友|群对象列表|活跃统计)$", handler="yunzai"),
    # P7: AstrBot @bot-required
    CommandRule(pattern=r"^爆料$", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"(刚刚|刚才|最近).*?[聊说讨论谈].*?(什么|啥|话题)", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"总结一下?(群聊|话题)?|复盘一下?", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"(给)(我|我们)?总结一下?", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"^话题(总结|是什么|是啥)$", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"(刚才|最近)?发生了?(什么|啥)", handler="astrbot", require_mention=True),
    CommandRule(pattern=r"群里.*?[聊说讨论].*?(什么|啥)", handler="astrbot", require_mention=True),
    # P8: AstrBot stickers (no @ required) — includes 菲比啾比
    CommandRule(pattern=r"来个(doro|柴郡|咖波|艾露猫|三小只|吉咿卡哇|菲比|菲比啾比)", handler="astrbot"),
    # P9: AstrBot games/utility
    CommandRule(pattern=r"来局海龟汤", handler="astrbot"),
    CommandRule(pattern=r"^今日小猪$", handler="astrbot"),
    CommandRule(pattern=r"^(攻击|猛攻)$", handler="astrbot"),
    CommandRule(pattern=r"^(扣|鹿|🦌)$", handler="astrbot"),
    CommandRule(pattern=r"^吃什么", handler="astrbot"),
    CommandRule(pattern=r"^画像$", handler="astrbot"),
    # P10: Klee Core
    CommandRule(pattern=r"^查询好感$", handler="klee_core"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def router(tmp_path):
    """IntentRouter with test bq.json and rules."""
    bq_file = tmp_path / "bq.json"
    bq_file.write_text(SAMPLE_BQ_JSON, encoding="utf-8")

    import os
    os.environ["KLEE_CORE_BQ_PATH"] = str(bq_file)

    r = IntentRouter(rules=TEST_RULES)
    yield r
    os.environ.pop("KLEE_CORE_BQ_PATH", None)


# ---------------------------------------------------------------------------
# P1: Yunzai # prefix
# ---------------------------------------------------------------------------

class TestYunzaiHashCommands:
    def test_hash_any(self, router):
        r = router.detect("#今日运势")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_hash_panel(self, router):
        r = router.detect("#绫华面板")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_hash_with_mention(self, router):
        """# commands work regardless of @mention."""
        r = router.detect("#猜语音", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "yunzai"


# ---------------------------------------------------------------------------
# P2: bq.json keywords
# ---------------------------------------------------------------------------

class TestBqKeywords:
    def test_exact_match_chinese(self, router):
        r = router.detect("加速")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"
        assert r.reason == "matched_bq_keyword"

    def test_exact_match_single_char(self, router):
        r = router.detect("抱")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_exact_match_single_char_chi(self, router):
        r = router.detect("吃")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_exact_not_partial(self, router):
        """'吃' matches, but '吃什么' does NOT (exact match vs regex)."""
        r = router.detect("吃")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

        r2 = router.detect("吃什么")
        # "吃什么" not in bq set → falls through to regex → matches P9 ^吃什么
        assert r2.is_command is True
        assert r2.primary_handler == "astrbot"

    def test_exact_not_contains(self, router):
        """'加速器' is not in bq set."""
        r = router.detect("加速器")
        assert r.is_command is False  # no regex match either

    def test_bq_keyword_after_at(self, router):
        """@mention stripped, bq keyword still matches."""
        r = router.detect("加速", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "yunzai"


# ---------------------------------------------------------------------------
# P3: AstrBot /-prefixed
# ---------------------------------------------------------------------------

class TestSlashCommands:
    def test_leaks_slash(self, router):
        r = router.detect("/leaks")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_neigui_slash(self, router):
        r = router.detect("/内鬼")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_unknown_slash(self, router):
        r = router.detect("/unknown")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_hash_before_slash(self, router):
        """# prefix has higher priority than /, but # already starts."""
        r = router.detect("#/test")  # starts with # → P1
        assert r.is_command is True
        assert r.primary_handler == "yunzai"


# ---------------------------------------------------------------------------
# P4-P6: Yunzai non-# commands
# ---------------------------------------------------------------------------

class TestYunzaiNonHashCommands:
    def test_guess_voice(self, router):
        r = router.detect("猜语音")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_genshin_guess(self, router):
        r = router.detect("原神猜语")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_diange_with_song(self, router):
        r = router.detect("点歌 周杰伦")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_diange_no_space(self, router):
        r = router.detect("点歌晴天")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_diange_alone(self, router):
        r = router.detect("点歌")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"

    def test_marry(self, router):
        r = router.detect("娶群友")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"


# ---------------------------------------------------------------------------
# P7: AstrBot @bot-required
# ---------------------------------------------------------------------------

class TestAtRequiredCommands:
    def test_leaks_at_bot(self, router):
        r = router.detect("爆料", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_leaks_no_at_ignored(self, router):
        r = router.detect("爆料", mentions_bot=False)
        assert r.is_command is False  # require_mention, not met

    def test_topic_summary_at_bot(self, router):
        r = router.detect("刚刚在聊什么", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_topic_summary_no_at(self, router):
        r = router.detect("刚刚在聊什么", mentions_bot=False)
        assert r.is_command is False

    def test_zongjie_at_bot(self, router):
        r = router.detect("总结一下", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_zongjie_no_at(self, router):
        """'谁来总结一下刚刚开会说了什么' — not @bot, should NOT match."""
        r = router.detect("谁来总结一下刚刚开会说了什么", mentions_bot=False)
        assert r.is_command is False


# ---------------------------------------------------------------------------
# P8: AstrBot stickers (no @ required) — 菲比已移入此处
# ---------------------------------------------------------------------------

class TestStickerCommands:
    def test_doro(self, router):
        r = router.detect("来个doro")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_chaijun(self, router):
        r = router.detect("来个柴郡")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_kabo(self, router):
        r = router.detect("来个咖波")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_ailucat(self, router):
        r = router.detect("来个艾露猫")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_sanzhixiao(self, router):
        r = router.detect("来个三小只")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_jiiyikawa(self, router):
        r = router.detect("来个吉咿卡哇")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_feibi_no_at(self, router):
        """菲比 without @mention — still triggers (P8, no require_mention)."""
        r = router.detect("来个菲比", mentions_bot=False)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_feibi_with_at(self, router):
        r = router.detect("来个菲比", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_feibijiubi(self, router):
        r = router.detect("来个菲比啾比", mentions_bot=False)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_sticker_after_at_mention(self, router):
        """@bot stripped → 来个doro still matches."""
        r = router.detect("来个doro", mentions_bot=True)
        assert r.is_command is True
        assert r.primary_handler == "astrbot"


# ---------------------------------------------------------------------------
# P9: AstrBot games/utility
# ---------------------------------------------------------------------------

class TestAstrBotGameCommands:
    def test_turtle_soup(self, router):
        r = router.detect("来局海龟汤")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_turtle_soup_no_longer_flexible(self, router):
        """'开一局' is removed — should not match."""
        r = router.detect("开一局")
        assert r.is_command is False

    def test_pig(self, router):
        r = router.detect("今日小猪")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_attack(self, router):
        r = router.detect("攻击")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_signin_kou(self, router):
        r = router.detect("扣")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_signin_lu(self, router):
        r = router.detect("鹿")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_what_to_eat(self, router):
        r = router.detect("吃什么")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"

    def test_portrayal(self, router):
        r = router.detect("画像")
        assert r.is_command is True
        assert r.primary_handler == "astrbot"


# ---------------------------------------------------------------------------
# P10: Klee Core
# ---------------------------------------------------------------------------

class TestKleeCoreCommands:
    def test_affection_exact(self, router):
        r = router.detect("查询好感")
        assert r.is_command is True
        assert r.primary_handler == "klee_core"

    def test_affection_not_loose(self, router):
        r = router.detect("我想查询好感度")
        assert r.is_command is False


# ---------------------------------------------------------------------------
# Non-command (normal chat)
# ---------------------------------------------------------------------------

class TestNonCommandMessages:
    def test_greeting(self, router):
        r = router.detect("你好")
        assert r.is_command is False
        assert r.primary_handler == "none"

    def test_normal_chat(self, router):
        r = router.detect("今天天气真好")
        assert r.is_command is False

    def test_empty(self, router):
        r = router.detect("")
        assert r.is_command is False
        assert r.reason == "empty_text"


# ---------------------------------------------------------------------------
# Priority order verification
# ---------------------------------------------------------------------------

class TestPriorityOrder:
    def test_hash_beats_bq(self, router):
        """# prefix has higher priority than bq keywords."""
        r = router.detect("#抱")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"
        assert r.matched_pattern == "^#"

    def test_chi_vs_signin(self, router):
        """'吃' is a bq keyword → yunzai (P2 beats P9)."""
        r = router.detect("吃")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"
        assert r.reason == "matched_bq_keyword"

    def test_hash_beats_slash(self, router):
        """# prefix (P1) beats / prefix (P3)."""
        r = router.detect("#/test")
        assert r.is_command is True
        assert r.primary_handler == "yunzai"
