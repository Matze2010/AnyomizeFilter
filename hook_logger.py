"""
title: hook_logger
author: Mathias Gisch
version: 0.1.0
description: Loggt jeden Aufruf von inlet(), stream() und outlet(), ohne etwas zu verändern.
"""

import logging
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional


class Filter:
    # Chunk counter of stream(), kept per request in __metadata__
    METADATA_STREAM_CHUNKS_KEY = "_hook_logger_stream_chunks"

    class Valves(BaseModel):
        priority: int = Field(
            default=10, description="Filter execution order, lower values run first"
        )

        log_inlet: bool = Field(default=True, description="Log calls of inlet()")

        log_outlet: bool = Field(default=True, description="Log calls of outlet()")

        log_stream: bool = Field(
            default=True,
            description="Log calls of stream(). One line per chunk — noisy.",
        )

        log_payload: bool = Field(
            default=False,
            description=(
                "Log the full body/event of each hook call. WARNING: writes user "
                "messages in clear text into the server log. Off means one compact "
                "line per call with metadata only."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def _log(
        self,
        hook: str,
        payload: Any,
        metadata: Optional[Dict[str, Any]],
        user: Optional[Dict[str, Any]],
        extra: str = "",
    ) -> None:
        """Write one line about a hook call. Never raises."""

        try:
            metadata = metadata or {}
            user = user or {}

            fields = [
                f"chat={metadata.get('chat_id', '-')}",
                f"msg={metadata.get('message_id', '-')}",
                f"session={metadata.get('session_id', '-')}",
                f"user={user.get('id', '-')}",
            ]
            if extra:
                fields.append(extra)
            if self.valves.log_payload:
                fields.append(f"payload={payload!r}")

            logging.warning(f"hook_logger {hook}: " + " ".join(fields))

        except Exception as e:
            # A logging failure must never break a request or a running stream.
            logging.warning(f"hook_logger {hook} logging failed: {e}")

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Log the call and pass the body through untouched.

        Every parameter but the body has a default because it is not
        guaranteed which extras Open WebUI injects on which path.
        """

        if self.valves.log_inlet:
            extra = ""
            try:
                extra = (
                    f"model={body.get('model', '-')} "
                    f"messages={len(body.get('messages', []))}"
                )
            except Exception:
                pass

            self._log("inlet", body, __metadata__, __user__, extra)

        return body

    async def stream(
        self,
        event: dict,
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Log every chunk of a streamed response and pass it through untouched."""

        if self.valves.log_stream:
            # The counter lives in __metadata__, not on the instance: Open WebUI
            # shares one Filter across all users and chats, so instance state
            # would mix up concurrent responses.
            chunk = "?"
            try:
                if __metadata__ is not None:
                    chunk = __metadata__.get(self.METADATA_STREAM_CHUNKS_KEY, 0) + 1
                    __metadata__[self.METADATA_STREAM_CHUNKS_KEY] = chunk
            except Exception:
                pass

            self._log("stream", event, __metadata__, __user__, f"chunk={chunk}")

        return event

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Log the call and pass the body through untouched."""

        if self.valves.log_outlet:
            extra = ""
            try:
                extra = (
                    f"model={body.get('model', '-')} "
                    f"messages={len(body.get('messages', []))}"
                )
            except Exception:
                pass

            self._log("outlet", body, __metadata__, __user__, extra)

        return body
