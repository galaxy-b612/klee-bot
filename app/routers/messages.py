"""Messages query router.

GET /messages/recent — retrieve recent messages for a group.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models import Message
from app.schemas.message import AttachmentResponse, MessageResponse, RecentMessagesResponse

router = APIRouter(tags=["messages"])


@router.get("/messages/recent", response_model=RecentMessagesResponse)
async def get_recent_messages(
    group_id: int = Query(..., description="Group QQ number"),
    limit: int = Query(50, ge=1, le=200, description="Max messages to return"),
    session: AsyncSession = Depends(get_session),
):
    """Return the most recent messages for a given group.

    Messages are ordered by creation time descending (newest first).
    Attachments are eagerly loaded.
    """
    stmt = (
        select(Message)
        .where(Message.group_id == group_id)
        .options(selectinload(Message.attachments))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()

    message_responses = [
        MessageResponse(
            id=m.id,
            platform_message_id=m.platform_message_id,
            bot_id=m.bot_id,
            group_id=m.group_id,
            user_id=m.user_id,
            nickname=m.nickname,
            message_type=m.message_type,
            text_content=m.text_content,
            normalized_content=m.normalized_content,
            reply_to_message_id=m.reply_to_message_id,
            mentions_bot=m.mentions_bot,
            created_at=m.created_at,
            attachments=[
                AttachmentResponse(
                    id=a.id,
                    attachment_type=a.attachment_type,
                    file_id=a.file_id,
                    url=a.url,
                    local_path=a.local_path,
                    mime_type=a.mime_type,
                    file_size=a.file_size,
                    duration=a.duration,
                    width=a.width,
                    height=a.height,
                    summary=a.summary,
                    analyzed=a.analyzed,
                )
                for a in (m.attachments or [])
            ],
        )
        for m in messages
    ]

    return RecentMessagesResponse(
        group_id=group_id,
        messages=message_responses,
        count=len(message_responses),
    )
