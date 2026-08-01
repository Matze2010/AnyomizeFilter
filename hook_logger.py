"""
title: hook_logger
author: Mathias Gisch
version: 0.2.0
description: Loggt jeden Aufruf von inlet(), stream(), outlet() und jedes Tool-Ergebnis, ohne etwas zu verändern.
"""

import functools
import inspect
import logging
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------
# Tool interception
#
# Filters only wrap the chat boundary. The result of a tool call never passes
# inlet(), stream() or outlet(): with native function calling Open WebUI runs
# the tool inside utils/middleware.py, appends a role="tool" message and calls
# the model again without re-entering the filter chain; on the legacy path the
# result is injected as context after inlet() already ran.
#
# The only place both paths share is get_tools(), which hands out the callables
# that middleware.py invokes. Wrapping those callables makes every tool call
# and every tool result observable.
#
# PROTOTYPE. This is a monkey patch of Open WebUI internals and can break on
# any upgrade — the name of the tool registry key in particular differs between
# versions. It observes only; tool arguments, results and exceptions are passed
# through untouched.
# --------------------------------------------------------------------------

# Marks a function this module has already wrapped, so that reloading the
# filter in Open WebUI does not stack wrapper upon wrapper.
_PATCH_FLAG = "_hook_logger_wrapped"

# Keys under which the tool registry of get_tools() stores the callable. Which
# one is used depends on the Open WebUI version.
_TOOL_CALLABLE_KEYS = ("callable", "tool")

# The Filter instance whose valves the tool wrappers read. Open WebUI keeps one
# instance per function, and the wrappers live outside the class, so the
# instance registers itself here in __init__.
_active_filter = None


def _tool_logging_enabled() -> bool:
    return bool(
        _active_filter is not None and _active_filter.valves.log_tools
    )


def _log_tool(line: str) -> None:
    try:
        logging.warning(f"hook_logger {line}")
    except Exception as e:
        logging.warning(f"hook_logger tool logging failed: {e}")


def _describe_result(result: Any) -> str:
    """Compact description of a tool result: type and size, no content."""

    try:
        size = f" len={len(result)}" if hasattr(result, "__len__") else ""
        return f"type={type(result).__name__}{size}"
    except Exception:
        return f"type={type(result).__name__}"


def _wrap_tool_callable(name: str, fn):
    """Return fn wrapped in call/result logging. Never swallows exceptions."""

    def _before(kwargs: Dict[str, Any]) -> None:
        if not _tool_logging_enabled():
            return
        if _active_filter.valves.log_payload:
            _log_tool(f"tool_call: name={name} args={kwargs!r}")
        else:
            _log_tool(f"tool_call: name={name} args={sorted(kwargs)}")

    def _after(result: Any) -> None:
        if not _tool_logging_enabled():
            return
        if _active_filter.valves.log_payload:
            _log_tool(f"tool_result: name={name} result={result!r}")
        else:
            _log_tool(f"tool_result: name={name} {_describe_result(result)}")

    def _failed(exc: BaseException) -> None:
        if _tool_logging_enabled():
            _log_tool(f"tool_result: name={name} raised {type(exc).__name__}: {exc}")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            _before(kwargs)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as e:
                _failed(e)
                raise
            _after(result)
            return result

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _before(kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                _failed(e)
                raise
            _after(result)
            return result

    setattr(wrapper, _PATCH_FLAG, True)
    return wrapper


def _wrap_tool_registry(tools: Any) -> Any:
    """Wrap every tool callable in the registry returned by get_tools()."""

    try:
        for name, entry in tools.items():
            if not isinstance(entry, dict):
                continue
            for key in _TOOL_CALLABLE_KEYS:
                fn = entry.get(key)
                if callable(fn) and not getattr(fn, _PATCH_FLAG, False):
                    entry[key] = _wrap_tool_callable(name, fn)
    except Exception as e:
        # A broken registry must not break tool calling.
        logging.warning(f"hook_logger could not wrap tools: {e}")

    return tools


def _install_get_tools_patch() -> bool:
    """Patch middleware.get_tools so its callables log. Idempotent."""

    try:
        # Patch the name bound in middleware, not the one in utils.tools:
        # middleware does "from ...tools import get_tools", so replacing the
        # original in utils.tools would have no effect on the caller.
        from open_webui.utils import middleware
    except Exception as e:
        logging.warning(f"hook_logger: no tool interception ({e})")
        return False

    original = getattr(middleware, "get_tools", None)
    if original is None:
        logging.warning("hook_logger: middleware.get_tools not found, no interception")
        return False
    if getattr(original, _PATCH_FLAG, False):
        return True

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def patched(*args, **kwargs):
            return _wrap_tool_registry(await original(*args, **kwargs))

    else:

        @functools.wraps(original)
        def patched(*args, **kwargs):
            return _wrap_tool_registry(original(*args, **kwargs))

    setattr(patched, _PATCH_FLAG, True)
    middleware.get_tools = patched
    logging.warning("hook_logger: tool interception installed")
    return True


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

        log_tools: bool = Field(
            default=True,
            description=(
                "Log every tool call and its result. Needs the monkey patch of "
                "middleware.get_tools that is installed when this filter loads; "
                "tool results never reach inlet/stream/outlet."
            ),
        )

        log_payload: bool = Field(
            default=False,
            description=(
                "Log the full body/event of each hook call and the arguments and "
                "results of each tool call. WARNING: writes user messages in clear "
                "text into the server log. Off means one compact line per call "
                "with metadata only."
            ),
        )

    def __init__(self):
        global _active_filter

        self.valves = self.Valves()

        # The tool wrappers live at module level and read the valves from here.
        _active_filter = self
        _install_get_tools_patch()

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
