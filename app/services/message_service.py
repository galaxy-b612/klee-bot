"""Message service — normalizes OneBot events, creates Message + Attachment records."""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import (
    detect_mentions_bot,
    detect_message_type,
    detect_reply_target,
    extract_attachments,
    extract_normalized_text,
    extract_text,
)
from app.models import Attachment, Message

logger = logging.getLogger(__name__)


async def create_message_from_event(
    session: AsyncSession,
    event,        # OneBotEvent (Pydantic model)
    bot_id: str,
) -> Message:
    """Normalize a OneBot v11 event into a Message and persist to database.

    Args:
        session: Async database session.
        event: Validated OneBotEvent Pydantic model.
        bot_id: Bot instance identifier (e.g. "klee_main").

    Returns:
        The persisted Message ORM object (with attachments loaded).
    """
    segments = event.message or []
    self_id = event.self_id

    msg_type = detect_message_type(segments)
    text_raw = extract_text(segments)
    text_norm = extract_normalized_text(segments)
    mentions = detect_mentions_bot(segments, self_id)
    reply_to = detect_reply_target(segments)
    attachments_data = extract_attachments(segments)

    group_id = event.group_id or 0
    user_id = event.user_id or 0
    nickname = (event.sender.nickname or event.sender.card or "").strip()

    raw_json = event.model_dump_json(exclude_none=True)

    message = Message(
        platform_message_id=event.message_id or None,
        bot_id=bot_id,
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        message_type=msg_type,
        text_content=text_raw,
        normalized_content=text_norm,
        reply_to_message_id=reply_to,
        mentions_bot=mentions,
        raw_event_json=raw_json,
    )

    session.add(message)
    await session.flush()  # Get message.id for attachments

    # Create attachment records
    for att_data in attachments_data:
        attachment = Attachment(
            message_id=message.id,  # type: ignore[arg-type]
            **att_data,
        )
        session.add(attachment)

    await session.commit()
    await session.refresh(message, attribute_names=["attachments"])

    logger.info(
        "Message stored: id=%s type=%s group=%s user=%s bot=%s",
        message.id, msg_type, group_id, user_id, bot_id,
    )
    return message
