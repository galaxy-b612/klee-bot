"""Message model — unified representation of an incoming chat message."""

import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_message_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    nickname: Mapped[str] = mapped_column(String(128), default="")
    message_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text",
        comment="text|image|voice|video|file|mixed|reply|forward|other"
    )
    text_content: Mapped[str] = mapped_column(Text, default="")
    normalized_content: Mapped[str] = mapped_column(Text, default="")
    reply_to_message_id: Mapped[int] = mapped_column(Integer, nullable=True)
    mentions_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_event_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationship
    attachments = relationship(
        "Attachment", back_populates="message", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id} type={self.message_type} "
            f"group={self.group_id} user={self.user_id}>"
        )
