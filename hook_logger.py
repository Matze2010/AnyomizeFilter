"""
title: hook_logger
author: Mathias Gisch
version: 0.2.0
description: Loggt jeden Aufruf von inlet(), stream(), outlet() und jedes Tool-Ergebnis, ohne etwas zu verändern.
"""

import functools
import inspect
import logging
import weakref
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
# Two functions are patched, because neither covers all tools on its own:
#
#   get_updated_tool_function(function, extra_params)
#       Called by middleware right before a tool runs on the native function
#       calling path. It rebuilds the callable from function.__function__ and
#       function.__extra_params__, which means a wrapper placed anywhere
#       earlier is discarded — functools.wraps copies exactly those two
#       attributes along, so the rebuild finds them and starts over from the
#       original. Wrapping its return value is therefore the only wrapper that
#       survives, and it is also the one place where MCP tools show up: their
#       registry entries are built inside middleware and never pass get_tools.
#
#   get_tools(request, tool_ids, user, extra_params)
#       Hands out the callables of the plugin tools. On the legacy function
#       calling path middleware calls those directly, without asking
#       get_updated_tool_function first, so they have to be wrapped here. This
#       is also where tool names are learned for the log lines.
#
# Not covered: tools of direct tool servers (entries with direct=True). Open
# WebUI executes them in the browser through an event, so nothing runs
# server-side that could be wrapped.
#
# PROTOTYPE. This is a monkey patch of Open WebUI internals and can break on
# any upgrade. It observes only; tool arguments, results and exceptions are
# passed through untouched.
# --------------------------------------------------------------------------

# Marks a function this module has already wrapped, so that reloading the
# filter in Open WebUI does not stack wrapper upon wrapper.
_PATCH_FLAG = "_hook_logger_wrapped"

# Keys under which the tool registry of get_tools() stores the callable. Which
# one is used depends on the Open WebUI version.
_TOOL_CALLABLE_KEYS = ("callable", "tool")

# Tool callable -> tool name, learned from the registry of get_tools(). The
# callable middleware finally invokes is rebuilt and therefore a different
# object, so the original under __function__ is registered as well. Weak keys:
# the map must not keep tool modules alive across reloads.
_tool_names: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

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


def _register_tool_name(fn, name: str) -> None:
    """Remember the name of a tool callable and of the original behind it."""

    for candidate in (fn, getattr(fn, "__function__", None)):
        if candidate is None:
            continue
        try:
            _tool_names[candidate] = name
        except TypeError:
            # Not weak-referenceable, e.g. a builtin. Nothing to remember.
            pass


def _tool_name_for(fn) -> str:
    """Best known name of a tool callable, for the log line."""

    for candidate in (fn, getattr(fn, "__function__", None)):
        if candidate is None:
            continue
        try:
            if candidate in _tool_names:
                return _tool_names[candidate]
        except TypeError:
            pass

    return getattr(fn, "__name__", None) or "?"


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
                if not callable(fn):
                    continue
                _register_tool_name(fn, name)
                if not getattr(fn, _PATCH_FLAG, False):
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
    return True


def _install_get_updated_tool_function_patch() -> bool:
    """Wrap the callable middleware runs on the native path. Idempotent.

    This has to patch the last step before the call: get_updated_tool_function
    rebuilds the callable from __function__, so any wrapper installed earlier
    is gone by the time the tool actually runs.
    """

    patched_any = False

    for module_name in ("open_webui.utils.middleware", "open_webui.utils.tools"):
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception as e:
            logging.warning(f"hook_logger: cannot patch {module_name} ({e})")
            continue

        original = getattr(module, "get_updated_tool_function", None)
        if original is None:
            continue
        if getattr(original, _PATCH_FLAG, False):
            patched_any = True
            continue

        @functools.wraps(original)
        async def patched(function, extra_params, _original=original):
            resolved = await _original(function, extra_params)
            if not callable(resolved) or getattr(resolved, _PATCH_FLAG, False):
                return resolved
            return _wrap_tool_callable(_tool_name_for(resolved), resolved)

        setattr(patched, _PATCH_FLAG, True)
        setattr(module, "get_updated_tool_function", patched)
        patched_any = True

    return patched_any


def _install_tool_patches() -> None:
    """Install both patches and report in the log what is covered."""

    try:
        via_get_tools = _install_get_tools_patch()
        via_updated = _install_get_updated_tool_function_patch()
    except Exception as e:
        logging.warning(f"hook_logger: tool interception failed ({e})")
        return

    logging.warning(
        "hook_logger: tool interception installed "
        f"(get_tools={via_get_tools}, get_updated_tool_function={via_updated})"
    )
    if not via_updated:
        logging.warning(
            "hook_logger: get_updated_tool_function not found — native function "
            "calling and MCP tools will not be logged on this Open WebUI version"
        )


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
        _install_tool_patches()

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
