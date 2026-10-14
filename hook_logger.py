"""
title: hook_logger
author: Mathias Gisch
version: 0.6.0
description: Loggt jeden Aufruf von inlet(), stream(), outlet(), process_tool_result() und jedes Tool-Ergebnis, ohne etwas zu verändern. Optional (Valve outlet_overwrite, aus): ersetzt in outlet() die fertige Antwort komplett — Proof of Concept.
"""

import functools
import inspect
import logging
import re
import time
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
# Three functions are patched, because none of them covers all tools on its own:
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
#   process_tool_result(request, tool_function_name, tool_result, tool_type,
#                       direct_tool=False, metadata=None, user=None)
#           -> (tool_result, tool_result_files, tool_result_embeds)
#       The step after the call: middleware normalizes every result here (dict,
#       list, tuple, HTMLResponse and MCP items all become a string) before it
#       is appended as a role="tool" message. It is defined at module level in
#       middleware and called through the module global, so replacing it covers
#       every call site — including the direct tools the two wrappers above
#       never see. This is also where anonymize.py hooks in, which makes the
#       line logged here the reference when debugging that filter.
#
# Not covered by the two wrappers: tools of direct tool servers (entries with
# direct=True). Open WebUI executes them in the browser through an event, so
# nothing runs server-side that could be wrapped — their result does pass
# process_tool_result() though.
#
# PROTOTYPE. This is a monkey patch of Open WebUI internals and can break on
# any upgrade. It observes only; tool arguments, results and exceptions are
# passed through untouched.
# --------------------------------------------------------------------------

# Logged when the patches are installed. Open WebUI shows no version anywhere,
# so this is the only way to tell from a log which source is actually running.
_VERSION = "0.6.0"

# Marks a function this module has already wrapped, so that reloading the
# filter in Open WebUI does not stack wrapper upon wrapper.
_PATCH_FLAG = "_hook_logger_wrapped"

# Where a wrapper keeps the function it replaced. Open WebUI re-execs the whole
# function module whenever its valves are edited or the source is updated, and
# everything the previous module object installed stays in place — wrappers
# that read the valves of the previous instance. Nothing is therefore ever
# skipped because it is "already wrapped": wrappers are peeled off along this
# attribute first and rebuilt by the module loaded last.
_PATCH_ORIGINAL = "_hook_logger_original"

# Keys under which the tool registry of get_tools() stores the callable. Which
# one is used depends on the Open WebUI version.
_TOOL_CALLABLE_KEYS = ("callable", "tool")

# Tool callable -> tool name, learned from the registry of get_tools(). The
# callable middleware finally invokes is rebuilt and therefore a different
# object, so the original under __function__ is registered as well. Weak keys:
# the map must not keep tool modules alive across reloads.
_tool_names: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

# The Filter instance whose valves the tool wrappers fall back to. Open WebUI
# keeps one instance per function, and the wrappers live outside the class, so
# the instance registers itself here in __init__.
_active_filter = None

# The function ID this module was imported under, needed to read the valves as
# they are stored right now — see _stored_valves().
#
# Open WebUI names every function module after the ID the user chose on import
# (utils/plugin.py), so the module name is the only place it can be read from:
# f"function_{function_id}" up to 0.11.x, f"function_{function_id}_{uuid hex}"
# from 0.12.0 on — the 32 hex digits are stripped again. Outside Open WebUI —
# tests, a direct import — the name does not match and the ID stays None.
_OWN_FUNCTION_ID_MATCH = re.fullmatch(r"function_(.+?)(?:_[0-9a-f]{32})?", __name__)
_OWN_FUNCTION_ID = _OWN_FUNCTION_ID_MATCH.group(1) if _OWN_FUNCTION_ID_MATCH else None

# The stored valves are read per tool call, not per log field. One second is
# short enough that a valve flipped in the UI takes effect on the next call and
# long enough that a single log line does not query the database seven times.
_VALVE_CACHE_TTL = 1.0
_valve_cache: Dict[str, Any] = {"read_at": 0.0, "values": None}

# The store is read on every tool call; a failure would otherwise fill the log
# with the same line over and over.
_valve_store_warned = False


def _stored_valves() -> Optional[Dict[str, Any]]:
    """The valve values as they are stored right now, or None.

    Reading _active_filter.valves alone is not enough. The wrappers hang on
    middleware and run for every tool call of the instance, while the Filter
    object they would read belongs to the module Open WebUI loaded last — and
    that reload only happens lazily, when a request actually runs the filter.
    Editing a valve while the filter is toggled off in the chat therefore has
    no effect on the wrappers at all: they keep reading the valves the previous
    module was built with. The stored values are the ones the UI writes, so
    they are asked first and the instance is only the fallback.
    """

    global _valve_store_warned

    if _OWN_FUNCTION_ID is None:
        # Not loaded as an Open WebUI function — nothing to read from.
        return None

    now = time.monotonic()
    if (
        _valve_cache["values"] is not None
        and now - _valve_cache["read_at"] < _VALVE_CACHE_TTL
    ):
        return _valve_cache["values"]

    try:
        from open_webui.models.functions import Functions

        values = Functions.get_function_valves_by_id(_OWN_FUNCTION_ID)
    except Exception as e:
        if not _valve_store_warned:
            _valve_store_warned = True
            logging.warning(
                f"hook_logger: cannot read the stored valves ({e}) — the tool "
                f"wrappers fall back to the loaded instance, so a valve change "
                f"only takes effect once the filter module is reloaded"
            )
        return None

    if not isinstance(values, dict):
        return None

    _valve_cache["values"] = values
    _valve_cache["read_at"] = now
    return values


def _valve_enabled(name: str) -> bool:
    """True when that valve is on, read from the store where possible."""

    stored = _stored_valves()
    if stored is not None and name in stored:
        return bool(stored[name])

    return bool(
        _active_filter is not None and getattr(_active_filter.valves, name, False)
    )


def _tool_logging_enabled() -> bool:
    return _valve_enabled("log_tools")


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


def _bind_call(original, args, kwargs) -> Dict[str, Any]:
    """Resolve the arguments of a call by name, against the signature.

    Middleware calls positionally, but binding against the signature of the
    original keeps this working if Open WebUI reorders or adds parameters —
    and a new parameter then shows up in the log on its own.
    """

    try:
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception as e:
        _log_tool(f"could not read the call arguments: {e}")
        return {}


def _describe_argument(name: str, value: Any) -> str:
    """One `name=value` pair for a call log line, kept compact.

    The full payload of the parameters that can carry user data — the tool
    result and the metadata — is only written when the valve log_payload is on.
    """

    payload = _valve_enabled("log_payload")

    if name == "tool_result" and not payload:
        # The whole point of the compact mode: type and size only, however
        # short the result is.
        size = f" len={len(value)}" if hasattr(value, "__len__") else ""
        return f"{name}=<{type(value).__name__}{size}>"

    if isinstance(value, (str, bool, int, float)) or value is None:
        if isinstance(value, str) and not payload and len(value) > 200:
            return f"{name}=<str len={len(value)}>"
        return f"{name}={value!r}"

    if isinstance(value, dict):
        if payload:
            return f"{name}={value!r}"
        # Enough to correlate a call with the inlet/stream/outlet lines of the
        # same request without writing the whole dict into the log.
        known = {
            key: value.get(key)
            for key in ("chat_id", "message_id", "session_id", "user_id", "id")
            if key in value
        }
        return f"{name}={{{', '.join(f'{k}={v!r}' for k, v in known.items())}}} keys={len(value)}"

    if payload:
        return f"{name}={value!r}"

    size = f" len={len(value)}" if hasattr(value, "__len__") else ""
    return f"{name}=<{type(value).__name__}{size}>"


def _log_process_tool_result_call(arguments: Dict[str, Any]) -> None:
    """Log one call of process_tool_result() with all of its parameters."""

    try:
        fields = " ".join(
            _describe_argument(name, value) for name, value in arguments.items()
        )
    except Exception as e:
        fields = f"<could not describe the arguments: {e}>"

    _log_tool(f"process_tool_result: {fields}")


def _log_process_tool_result_return(result: Any) -> None:
    """Log what process_tool_result() returned.

    The tuple is deliberately not unpacked: this module only observes, and
    unpacking would break the moment Open WebUI changes what it returns.
    """

    try:
        if _valve_enabled("log_payload"):
            described = repr(result)
        elif isinstance(result, (tuple, list)):
            described = " | ".join(_describe_result(item) for item in result)
        else:
            described = _describe_result(result)
    except Exception as e:
        described = f"<could not describe the result: {e}>"

    _log_tool(f"process_tool_result -> {described}")


def _unwrap(fn):
    """Strip wrappers of this module, including those of an older version.

    Versions before this one had no _PATCH_ORIGINAL, but every wrapper is
    built with functools.wraps, so __wrapped__ leads back to the original
    there as well.
    """

    seen = set()
    while callable(fn) and getattr(fn, _PATCH_FLAG, False):
        if id(fn) in seen:
            break
        seen.add(id(fn))
        inner = getattr(fn, _PATCH_ORIGINAL, None) or getattr(fn, "__wrapped__", None)
        if inner is None:
            break
        fn = inner

    return fn


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
        if _valve_enabled("log_payload"):
            _log_tool(f"tool_call: name={name} args={kwargs!r}")
        else:
            _log_tool(f"tool_call: name={name} args={sorted(kwargs)}")

    def _after(result: Any) -> None:
        if not _tool_logging_enabled():
            return
        if _valve_enabled("log_payload"):
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
    setattr(wrapper, _PATCH_ORIGINAL, fn)
    return wrapper


def _wrap_tool_registry(tools: Any) -> Any:
    """Wrap every tool callable in the registry returned by get_tools()."""

    try:
        for name, entry in tools.items():
            if not isinstance(entry, dict):
                continue
            for key in _TOOL_CALLABLE_KEYS:
                fn = _unwrap(entry.get(key))
                if not callable(fn):
                    continue
                _register_tool_name(fn, name)
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

    installed = getattr(middleware, "get_tools", None)
    if installed is None:
        logging.warning("hook_logger: middleware.get_tools not found, no interception")
        return False

    # Rebuild from the untouched function, so a patch of an earlier module
    # object is replaced instead of wrapped a second time.
    original = _unwrap(installed)

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def patched(*args, **kwargs):
            return _wrap_tool_registry(await original(*args, **kwargs))

    else:

        @functools.wraps(original)
        def patched(*args, **kwargs):
            return _wrap_tool_registry(original(*args, **kwargs))

    setattr(patched, _PATCH_FLAG, True)
    setattr(patched, _PATCH_ORIGINAL, original)
    middleware.get_tools = patched
    if installed is not original:
        logging.warning("hook_logger: replaced the get_tools patch of an older version")
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

        installed = getattr(module, "get_updated_tool_function", None)
        if installed is None:
            continue

        original = _unwrap(installed)

        @functools.wraps(original)
        async def patched(function, extra_params, _original=original):
            # Peel off a wrapper of an older module version instead of leaving
            # it in charge: it would log against the valves of that version.
            resolved = _unwrap(await _original(_unwrap(function), extra_params))
            if not callable(resolved):
                return resolved
            return _wrap_tool_callable(_tool_name_for(resolved), resolved)

        setattr(patched, _PATCH_FLAG, True)
        setattr(patched, _PATCH_ORIGINAL, original)
        setattr(module, "get_updated_tool_function", patched)
        patched_any = True
        if installed is not original:
            logging.warning(
                f"hook_logger: replaced the {module_name} patch of an older version"
            )

    return patched_any


def _install_process_tool_result_patch() -> bool:
    """Log every call of middleware.process_tool_result(). Idempotent.

    Pure observation: the arguments are logged before the original runs and its
    return value is passed back untouched.
    """

    try:
        from open_webui.utils import middleware
    except Exception as e:
        logging.warning(f"hook_logger: no tool result interception ({e})")
        return False

    installed = getattr(middleware, "process_tool_result", None)
    if installed is None:
        logging.warning(
            "hook_logger: middleware.process_tool_result not found, no interception"
        )
        return False

    # Rebuild from the untouched function, so a patch of an earlier module
    # object is replaced instead of wrapped a second time.
    original = _unwrap(installed)

    @functools.wraps(original)
    async def patched(*args, **kwargs):
        enabled = _valve_enabled("log_process_tool_result")

        # Logged before the original runs, so a call that hangs downstream is
        # still visible in the log.
        if enabled:
            _log_process_tool_result_call(_bind_call(original, args, kwargs))

        result = await original(*args, **kwargs)

        if enabled:
            _log_process_tool_result_return(result)

        return result

    setattr(patched, _PATCH_FLAG, True)
    setattr(patched, _PATCH_ORIGINAL, original)
    middleware.process_tool_result = patched

    if installed is not original:
        logging.warning(
            "hook_logger: replaced the process_tool_result patch of an older version"
        )

    return True


def _install_tool_patches() -> None:
    """Install all three patches and report in the log what is covered."""

    try:
        via_get_tools = _install_get_tools_patch()
        via_updated = _install_get_updated_tool_function_patch()
        via_result = _install_process_tool_result_patch()
    except Exception as e:
        logging.warning(f"hook_logger: tool interception failed ({e})")
        return

    logging.warning(
        f"hook_logger {_VERSION}: tool interception installed "
        f"(get_tools={via_get_tools}, get_updated_tool_function={via_updated}, "
        f"process_tool_result={via_result})"
    )
    if _OWN_FUNCTION_ID is None:
        logging.warning(
            "hook_logger: own function id unknown — the tool wrappers read the "
            "valves of the loaded instance, so a valve change only takes effect "
            "once the filter module is reloaded"
        )
    else:
        logging.warning(
            f"hook_logger: valves of the tool wrappers read live from function "
            f"id {_OWN_FUNCTION_ID!r} (log_payload="
            f"{_valve_enabled('log_payload')})"
        )
    if not via_updated:
        logging.warning(
            "hook_logger: get_updated_tool_function not found — native function "
            "calling and MCP tools will not be logged on this Open WebUI version"
        )
    if not via_result:
        logging.warning(
            "hook_logger: process_tool_result not found — the normalization step "
            "of tool results will not be logged on this Open WebUI version"
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

        log_process_tool_result: bool = Field(
            default=True,
            description=(
                "Log every call of middleware.process_tool_result() with all of "
                "its parameters and describe what it returns. This is the step "
                "where Open WebUI normalizes a tool result before it goes to the "
                "model, and the only one that also covers direct tools. Needs the "
                "monkey patch installed when this filter loads."
            ),
        )

        log_payload: bool = Field(
            default=False,
            description=(
                "Log the full body/event of each hook call, the arguments and "
                "results of each tool call, and the parameters and return value of "
                "each process_tool_result() call. WARNING: writes user messages and "
                "raw tool results in clear text into the server log. Off means one "
                "compact line per call with metadata only."
            ),
        )

        outlet_overwrite: bool = Field(
            default=False,
            description=(
                "Proof of concept: replace the whole assistant answer in outlet() "
                "with outlet_overwrite_text — both content and the structured "
                "output blocks the frontend actually renders. Off keeps this "
                "filter a pure observer."
            ),
        )

        outlet_overwrite_text: str = Field(
            default=(
                "[hook_logger] outlet overwrite PoC — die gestreamte Antwort wurde "
                "ersetzt."
            ),
            description="Text that replaces the answer while outlet_overwrite is on.",
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

    def _overwrite_assistant_text(self, message: Dict[str, Any], text: str) -> str:
        """Replace the text of one assistant message. Returns a note for the log.

        Writing "content" alone is not enough from Open WebUI 0.10 on: the
        frontend renders an assistant message from the structured "output"
        blocks and falls back to "content" only when there are none
        (ContentRenderer.svelte: `{#if output?.length}`). A message that was
        streamed always carries those blocks, so the text assembled from the
        stream chunks would stay on screen. The backend compares content and
        output separately and persists both, so setting both is all it takes.
        """

        message["content"] = text

        output = message.get("output")
        if not isinstance(output, list):
            return "content only (no output blocks)"

        message_items = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        if not message_items:
            return f"content only ({len(output)} output blocks, none of type message)"

        # Everything goes into the first message block; the rest are emptied.
        # An emptied block is invisible, not broken: frontend and backend both
        # skip message blocks whose text is blank.
        message_items[0]["content"] = [{"type": "output_text", "text": text}]
        for item in message_items[1:]:
            item["content"] = []

        return f"content + {len(message_items)} message block(s)"

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Log the call, and overwrite the answer when the PoC valve is on."""

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

        if self.valves.outlet_overwrite:
            try:
                for message in reversed(body.get("messages", [])):
                    if message.get("role") != "assistant":
                        continue

                    before = message.get("content") or ""
                    has_output = isinstance(message.get("output"), list)
                    note = self._overwrite_assistant_text(
                        message, self.valves.outlet_overwrite_text
                    )
                    logging.warning(
                        f"hook_logger outlet_overwrite: id={message.get('id', '-')} "
                        f"before_len={len(before)} output_blocks={has_output} -> {note}"
                    )
                    break
            except Exception as e:
                # A prototype must never destroy an answer.
                logging.warning(f"hook_logger outlet_overwrite failed: {e}")

        return body
