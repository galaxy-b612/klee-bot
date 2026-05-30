"""Bot instance model — maps a (bot_id, group_id) pair to downstream endpoints."""

import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BotInstance(Base):
    __tablename__ = "bot_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    napcat_name: Mapped[str] = mapped_column(String(64), default="")
    napcat_http_url: Mapped[str] = mapped_column(String(256), default="")
    napcat_ws_url: Mapped[str] = mapped_column(String(256), default="")
    hermes_endpoint: Mapped[str] = mapped_column(String(256), default="")
    astrbot_endpoint: Mapped[str] = mapped_column(String(256), default="")
    yunzai_endpoint: Mapped[str] = mapped_column(String(256), default="")
    meme_endpoint: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
