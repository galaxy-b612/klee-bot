"""Klee Core configuration loader — bots, routing, commands, downstreams, reply arbiter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# bot config models
# ---------------------------------------------------------------------------

class DownstreamEndpointConfig(BaseModel):
    astrbot_url: str = ""
    yunzai_url: str = ""
    hermes_url: str = ""
    meme_url: str = ""


class BotConfig(BaseModel):
    bot_id: str
    self_id: int
    napcat_name: str = ""
    napcat_http_url: str = ""
    napcat_ws_url: str = ""
    groups: list[int] = []
    downstream: DownstreamEndpointConfig = Field(default_factory=DownstreamEndpointConfig)
    enabled: bool = True


# ---------------------------------------------------------------------------
# routing / command config models
# ---------------------------------------------------------------------------

class SummaryTimeWindow(BaseModel):
    start: str = "22:30"
    end: str = "23:30"


class ReplyArbiterConfig(BaseModel):
    fake_success_when_blocked: bool = True
    allow_summary_scheduled_messages: bool = True
    trusted_summary_downstreams: list[str] = ["astrbot_main"]
    summary_message_allowed_groups: list[int] = []
    summary_message_allowed_actions: list[str] = [
        "send_msg", "send_group_msg", "send_forward_msg", "send_group_forward_msg",
    ]
    summary_message_keywords: list[str] = []
    summary_message_time_windows: list[SummaryTimeWindow] = []
    allow_summary_outside_time_window: bool = True

    # ── Scheduled system task whitelist (e.g. Yunzai xiaoyao daily sign-in) ──
    allow_scheduled_system_tasks: bool = False
    scheduled_system_task_downstreams: list[str] = ["yunzai"]
    scheduled_system_task_allowed_groups: list[int] = []
    scheduled_system_task_allowed_actions: list[str] = [
        "send_msg", "send_group_msg", "send_forward_msg", "send_group_forward_msg",
    ]
    scheduled_system_task_time_windows: list[SummaryTimeWindow] = []
    scheduled_system_task_max_per_group_per_window: int = 5


class KleeCoreRepeatConfig(BaseModel):
    """Klee Core 内置复读服务配置。"""
    enable: bool = True
    groups: list[int] = []
    threshold: int = 3
    window: int = 5
    cooldown_seconds: int = 60
    max_length: int = 80
    min_length: int = 1
    ignore_commands: bool = True
    ignore_links: bool = True
    ignore_cards: bool = True
    ignore_images: bool = True
    ignore_bot_messages: bool = True


class RoutingConfig(BaseModel):
    strategy: str = "parallel"
    timeout_seconds: int = 10
    max_retries: int = 0
    enabled_endpoints: list[str] = ["astrbot", "yunzai", "hermes", "meme"]

    # ── Passive intent detection ──
    enable_passive_intents: bool = True

    # ── Link parser observer ──
    enable_astrbot_observer_links: bool = True
    link_parser_targets: list[str] = ["astrbot_main"]
    link_parser_cooldown_seconds: int = 10

    # ── Card parser observer ──
    enable_astrbot_observer_cards: bool = True
    card_parser_targets: list[str] = ["astrbot_main"]
    card_parser_cooldown_seconds: int = 10

    # ── Summary observer ──
    enable_astrbot_summary_observer: bool = False
    summary_observer_targets: list[str] = ["astrbot_main"]
    summary_observer_groups: list[int] = []

    # ── AstrBot repeat observer (legacy, 默认关闭) ──
    enable_astrbot_repeat_plugin: bool = False
    repeat_observer_targets: list[str] = ["astrbot_main"]
    repeat_observer_groups: list[int] = []
    observer_reply_cooldown_seconds: int = 30

    # ── Klee Core 内置复读 ──
    klee_core_repeat: KleeCoreRepeatConfig = Field(default_factory=KleeCoreRepeatConfig)


class CommandRule(BaseModel):
    pattern: str
    handler: str
    require_mention: bool = False


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ---------------------------------------------------------------------------
# downstream transport config models
# ---------------------------------------------------------------------------

class DownstreamTransportConfig(BaseModel):
    """Configuration for one downstream service connection."""
    type: str = "onebot_reverse_ws"
    name: str = ""
    url: str = ""
    access_token: str = ""
    self_id: int = 0
    enabled: bool = True


# ---------------------------------------------------------------------------
# top-level app config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    bots: list[BotConfig] = []
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    commands: list[CommandRule] = []
    downstreams: dict[str, DownstreamTransportConfig] = {}
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    reply_arbiter: ReplyArbiterConfig = Field(default_factory=ReplyArbiterConfig)


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------

_config: Optional[AppConfig] = None
_config_dir: Optional[Path] = None


def _find_config_dir() -> Path:
    env_dir = os.environ.get("KLEE_CORE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    current = Path(__file__).resolve().parent.parent
    candidate = current / "config"
    if candidate.is_dir():
        return candidate
    cwd = Path.cwd()
    if (cwd / "config").is_dir():
        return cwd / "config"
    if cwd.name == "klee_core" and (cwd / "config").is_dir():
        return cwd / "config"
    raise FileNotFoundError(
        "Cannot locate config/ directory. "
        "Set KLEE_CORE_CONFIG_DIR env var or run from klee_core/."
    )


def load_config(config_dir: Optional[Path] = None) -> AppConfig:
    global _config, _config_dir
    if _config is not None and config_dir is None:
        return _config

    cfg_dir = config_dir or _config_dir or _find_config_dir()
    _config_dir = cfg_dir

    # bots.yaml
    bots_path = cfg_dir / "bots.yaml"
    bots: list[BotConfig] = []
    if bots_path.exists():
        bots_raw = yaml.safe_load(bots_path.read_text(encoding="utf-8")) or {}
        bots = [BotConfig(**b) for b in bots_raw.get("bots", [])]

    # routes.yaml
    routes_path = cfg_dir / "routes.yaml"
    routing = RoutingConfig()
    commands: list[CommandRule] = []
    logging_cfg = LoggingConfig()
    reply_arbiter = ReplyArbiterConfig()
    if routes_path.exists():
        routes_raw = yaml.safe_load(routes_path.read_text(encoding="utf-8")) or {}
        routing_data = routes_raw.get("routing", {})

        # Load Klee Core repeat config
        klee_repeat_data = routing_data.pop("klee_core_repeat", {})
        routing = RoutingConfig(**routing_data)
        if klee_repeat_data:
            routing.klee_core_repeat = KleeCoreRepeatConfig(**klee_repeat_data)

        commands = [CommandRule(**c) for c in routes_raw.get("commands", [])]
        logging_cfg = LoggingConfig(**routes_raw.get("logging", {}))
        reply_arbiter = ReplyArbiterConfig(**routes_raw.get("reply_arbiter", {}))

    # downstreams.yaml
    ds_path = cfg_dir / "downstreams.yaml"
    downstreams: dict[str, DownstreamTransportConfig] = {}
    if ds_path.exists():
        ds_raw = yaml.safe_load(ds_path.read_text(encoding="utf-8")) or {}
        for name, ds_data in ds_raw.get("downstreams", {}).items():
            dc = DownstreamTransportConfig(**ds_data)
            dc.name = name
            downstreams[name] = dc

    _config = AppConfig(
        bots=bots, routing=routing, commands=commands,
        downstreams=downstreams, logging=logging_cfg,
        reply_arbiter=reply_arbiter,
    )
    return _config


def get_config() -> AppConfig:
    if _config is None:
        return load_config()
    return _config


def get_bot_by_self_id(self_id: int) -> Optional[BotConfig]:
    for bot in get_config().bots:
        if bot.self_id == self_id and bot.enabled:
            return bot
    return None


def get_bot_by_id(bot_id: str) -> Optional[BotConfig]:
    for bot in get_config().bots:
        if bot.bot_id == bot_id:
            return bot
    return None


def is_group_managed(group_id: int) -> bool:
    for bot in get_config().bots:
        if bot.enabled and group_id in bot.groups:
            return True
    return False
