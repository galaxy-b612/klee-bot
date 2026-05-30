"""OneBot v11 event schemas for request validation.

Reference: https://github.com/botuniverse/onebot-11/blob/master/event/message.md
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Message segment
# ---------------------------------------------------------------------------

class OneBotMessageSegment(BaseModel):
    """A single segment in a OneBot message array."""
    type: str = Field(..., description="Segment type: text, image, at, face, record, video, file, reply, forward, etc.")
    data: dict[str, Any] = Field(default_factory=dict, description="Segment data payload")


# ---------------------------------------------------------------------------
# Sender info
# ---------------------------------------------------------------------------

class OneBotSender(BaseModel):
    user_id: int = 0
    nickname: str = ""
    card: str = ""
    sex: str = "unknown"
    age: int = 0
    role: Optional[str] = None
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# Top-level event
# ---------------------------------------------------------------------------

class OneBotEvent(BaseModel):
    """OneBot v11 message event (both group and private).

    Only the fields used by Klee Core are strictly validated; extra fields
    are allowed so the full event can be forwarded to downstream unchanged.
    """
    time: Optional[int] = None
    self_id: int = Field(..., description="Bot's own QQ number")
    post_type: str = Field(..., description="Event type, e.g. 'message'")
    message_type: str = Field(default="group", description="'group' or 'private'")
    sub_type: str = Field(default="normal")
    message_id: int = Field(default=0, description="Platform-side message id")
    group_id: Optional[int] = Field(default=None, description="Group id (group messages only)")
    user_id: Optional[int] = Field(default=None, description="Sender user id")
    anonymous: Optional[Any] = None
    message: list[OneBotMessageSegment] = Field(
        default_factory=list, description="Message content as segment array"
    )
    raw_message: str = Field(default="", description="Plain-text representation")
    font: int = Field(default=0)
    sender: OneBotSender = Field(default_factory=OneBotSender)

    model_config = ConfigDict(extra="allow")
