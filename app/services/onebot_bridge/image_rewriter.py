"""Image File Resolver — optionally rewrites file:// images to base64://.

Enabled only when config option `rewrite_file_image_to_base64` is True.
Disabled by default — Klee Core passes messages through unmodified.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ImageFileResolver:
    """Resolves file:// images to base64://, optionally.

    Designed for the scenario where a downstream (e.g., AstrBot) generates
    a temporary image file that Klee Core can access, but the target NapCat
    container cannot. When enabled, converts file:// references to inline
    base64:// data before forwarding to NapCat.

    DISABLED BY DEFAULT. Enable via config:
        media:
          rewrite_file_image_to_base64: true
          shared_media_dirs:
            - /shared-media
          static_media_base_url: ""

    Static media base URL: if set, file:// images under shared_media_dirs
    are rewritten to <static_media_base_url>/<relpath> instead of base64.
    """

    def __init__(
        self,
        enabled: bool = False,
        shared_media_dirs: list[str] | None = None,
        static_media_base_url: str = "",
    ):
        self.enabled = enabled
        self.shared_media_dirs: list[Path] = [
            Path(d) for d in (shared_media_dirs or []) if d
        ]
        self.static_media_base_url = static_media_base_url.rstrip("/")

    # ── Public API ─────────────────────────────────────────

    def rewrite_message(self, message: list[dict] | str) -> list[dict] | str:
        """Rewrite image segments in a message payload.

        Args:
            message: OneBot message (list of segments or CQ code string).

        Returns:
            Message with file:// images rewritten if enabled. Unchanged otherwise.
        """
        if isinstance(message, str):
            return self._rewrite_cq_string(message)
        if isinstance(message, list):
            return self._rewrite_segments(message)
        return message

    # ── Segment rewriter ───────────────────────────────────

    def _rewrite_segments(self, segments: list[dict]) -> list[dict]:
        """Rewrite image segments in a list."""
        if not self.enabled:
            return segments

        rewritten = []
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image":
                rewritten.append(self._rewrite_image_segment(seg))
            else:
                rewritten.append(seg)
        return rewritten

    def _rewrite_image_segment(self, seg: dict) -> dict:
        """Rewrite a single image segment if it uses file://."""
        file_val = seg.get("data", {}).get("file", "")
        if not file_val.startswith("file://"):
            return seg  # http://, base64://, or unknown — never touch

        file_path = file_val[7:]  # strip "file://"

        # Try static URL rewrite first
        for shared_dir in self.shared_media_dirs:
            try:
                abs_file = Path(file_path).resolve()
                abs_shared = shared_dir.resolve()
                if str(abs_file).startswith(str(abs_shared)):
                    rel_path = abs_file.relative_to(abs_shared)
                    new_url = f"{self.static_media_base_url}/{rel_path.as_posix()}"
                    logger.debug(
                        "Rewriting file:// via static URL: %s -> %s",
                        file_val, new_url,
                    )
                    new_seg = dict(seg)
                    new_seg["data"] = dict(seg["data"])
                    new_seg["data"]["file"] = new_url
                    return new_seg
            except (ValueError, OSError):
                continue

        # Try base64 conversion
        try:
            with open(file_path, "rb") as f:
                img_data = f.read()
            b64 = base64.b64encode(img_data).decode("ascii")
            mime = self._guess_mime(file_path)
            data_uri = f"base64://{b64}"
            if mime:
                # Note: OneBot base64:// already implies the MIME is auto-detected
                pass

            logger.debug(
                "Rewriting file:// to base64://: %s (%d bytes) -> %s...",
                file_path, len(img_data), data_uri[:40],
            )
            new_seg = dict(seg)
            new_seg["data"] = dict(seg["data"])
            new_seg["data"]["file"] = data_uri
            return new_seg
        except FileNotFoundError:
            logger.warning(
                "ImageFileResolver: file not found, keeping original: %s",
                file_path,
            )
            return seg
        except PermissionError:
            logger.warning(
                "ImageFileResolver: permission denied, keeping original: %s",
                file_path,
            )
            return seg
        except Exception:
            logger.exception(
                "ImageFileResolver: failed to convert file:// image: %s",
                file_path,
            )
            return seg

    # ── CQ code string rewriter ────────────────────────────

    def _rewrite_cq_string(self, text: str) -> str:
        """Rewrite file:// CQ codes in a string message."""
        import re

        if not self.enabled or "file://" not in text:
            return text

        # Match [CQ:image,file=file://...]
        def _replace_cq_image(match: re.Match) -> str:
            file_val = match.group(1)
            if not file_val.startswith("file://"):
                return match.group(0)

            file_path = file_val[7:]
            try:
                with open(file_path, "rb") as f:
                    img_data = f.read()
                b64 = base64.b64encode(img_data).decode("ascii")
                return f"[CQ:image,file=base64://{b64}]"
            except FileNotFoundError:
                logger.warning(
                    "ImageFileResolver: CQ image file not found: %s", file_path,
                )
                return match.group(0)
            except Exception:
                logger.exception(
                    "ImageFileResolver: failed to convert CQ file:// image: %s",
                    file_path,
                )
                return match.group(0)

        return re.sub(
            r"\[CQ:image,file=(file://[^\]]+)\]",
            _replace_cq_image,
            text,
        )

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _guess_mime(file_path: str) -> str:
        """Guess MIME type from file extension."""
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        return mime_map.get(ext, "application/octet-stream")

    @classmethod
    def from_config(cls, config_dict: dict[str, Any] | None) -> ImageFileResolver:
        """Create from a config dict.

        Expected config structure:
            media:
              rewrite_file_image_to_base64: false
              shared_media_dirs: []
              static_media_base_url: ""
        """
        if config_dict is None:
            config_dict = {}

        media = config_dict.get("media", {})
        return cls(
            enabled=media.get("rewrite_file_image_to_base64", False),
            shared_media_dirs=media.get("shared_media_dirs", []),
            static_media_base_url=media.get("static_media_base_url", ""),
        )
