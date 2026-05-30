"""Unit tests for PluginRouter — execution plan generation."""

import pytest

from app.services.intent_router import IntentResult
from app.services.plugin_router import ExecutionPlan, PluginRouter


@pytest.fixture
def router() -> PluginRouter:
    return PluginRouter()


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------

class TestCommandRouting:
    def test_command_to_astrbot(self, router):
        intent = IntentResult(
            is_command=True, primary_handler="astrbot",
            matched_pattern=r"海龟汤", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.primary_handler == "astrbot"
        assert plan.call_hermes is False
        assert "record" in plan.secondary_actions

    def test_command_to_yunzai(self, router):
        intent = IntentResult(
            is_command=True, primary_handler="yunzai",
            matched_pattern=r"^#", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.primary_handler == "yunzai"
        assert plan.call_hermes is False

    def test_command_to_hermes(self, router):
        intent = IntentResult(
            is_command=True, primary_handler="hermes",
            matched_pattern=r"^可莉", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.primary_handler == "hermes"
        assert plan.call_hermes is False

    def test_command_to_klee_core(self, router):
        intent = IntentResult(
            is_command=True, primary_handler="klee_core",
            matched_pattern=r"^查询好感$", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.primary_handler == "klee_core"
        assert plan.call_hermes is False

    def test_command_never_calls_hermes(self, router):
        """Plugin commands should never trigger Hermes."""
        intent = IntentResult(
            is_command=True, primary_handler="astrbot",
            matched_pattern=r"攻击", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.call_hermes is False


# ---------------------------------------------------------------------------
# Non-command routing
# ---------------------------------------------------------------------------

class TestNonCommandRouting:
    def test_normal_chat(self, router):
        intent = IntentResult(
            is_command=False, primary_handler="none",
            matched_pattern=None, reason="no_match",
        )
        plan = router.route(intent, mentions_bot=False)
        assert plan.primary_handler == "none"
        assert plan.call_hermes is False
        assert "record" in plan.secondary_actions
        assert plan.reason == "non_command_quiet"

    def test_at_mention(self, router):
        intent = IntentResult(
            is_command=False, primary_handler="none",
            matched_pattern=None, reason="no_match",
        )
        plan = router.route(intent, mentions_bot=True)
        assert plan.primary_handler == "none"
        assert plan.call_hermes is False  # Phase 2: deferred
        assert "record" in plan.secondary_actions
        assert "pending_hermes" in plan.secondary_actions


# ---------------------------------------------------------------------------
# Unknown handler fallback
# ---------------------------------------------------------------------------

class TestUnknownHandler:
    def test_unknown_handler_falls_back(self, router):
        intent = IntentResult(
            is_command=True, primary_handler="unknown_service",
            matched_pattern=r".*", reason="matched_command",
        )
        plan = router.route(intent)
        assert plan.primary_handler == "none"
        assert plan.call_hermes is False
        assert "unknown_handler" in plan.reason


# ---------------------------------------------------------------------------
# ExecutionPlan dataclass
# ---------------------------------------------------------------------------

class TestExecutionPlanDataclass:
    def test_default_values(self):
        plan = ExecutionPlan(primary_handler="astrbot")
        assert plan.secondary_actions == []
        assert plan.call_hermes is False
        assert plan.reason == ""

    def test_full_plan(self):
        plan = ExecutionPlan(
            primary_handler="yunzai",
            secondary_actions=["record", "log"],
            call_hermes=False,
            reason="command_routed",
        )
        assert plan.primary_handler == "yunzai"
        assert len(plan.secondary_actions) == 2
