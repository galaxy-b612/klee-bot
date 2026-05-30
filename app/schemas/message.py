"""Response schemas for the messages API."""

from __future__ import annotations

import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AttachmentResponse(BaseModel):
    id: int
    attachment_type: str
    file_id: str = ""
    url: str = ""
    local_path: str = ""
    mime_type: str = ""
    file_size: Optional[int] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    summary: str = ""
    analyzed: bool = False

    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    id: int
    platform_message_id: Optional[int] = None
    bot_id: str
    group_id: int
    user_id: int
    nickname: str = ""
    message_type: str
    text_content: str = ""
    normalized_content: str = ""
    reply_to_message_id: Optional[int] = None
    mentions_bot: bool = False
    created_at: datetime.datetime
    attachments: list[AttachmentResponse] = []

    model_config = ConfigDict(from_attributes=True)


class RecentMessagesResponse(BaseModel):
    group_id: int
    messages: list[MessageResponse]
    count: int
