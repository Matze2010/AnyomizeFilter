"""
title: anonymize.py
description: "Filter for anonymizing and deanonymizing text and files using an external API. The filter can be toggled on/off and configured via valves."
author: Mathias Gisch
version: 1.4.0
"""

import asyncio
import aiohttp
import fnmatch
import functools
import inspect
import os
import logging
from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Any

from open_webui.utils.misc import get_last_user_message, get_last_assistant_message
from open_webui.config import UPLOAD_DIR

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tool result interception
#
# Filters only wrap the chat boundary. The result of a tool call never passes
# inlet(), stream() or outlet(): Open WebUI runs the tool inside
# utils/middleware.py, normalizes the result in process_tool_result(), appends
# a role="tool" message and calls the model again without re-entering the
# filter chain. A tool that reads a case file, a database or a mailbox would
# therefore hand PII to the model in clear text even with this filter active.
#
# process_tool_result(request, tool_function_name, tool_result, tool_type,
#                     direct_tool=False, metadata=None, user=None)
#     -> (tool_result, tool_result_files, tool_result_embeds)
#
# It is defined at module level in middleware and called through the module
# global, so replacing middleware.process_tool_result covers every call site
# (native and prompt-based function calling, MCP, direct tools).
#
# The wrapper runs AFTER the original: by then dict, list, tuple, HTMLResponse
# and MCP items are already normalized to a string, so Filter.tool() only ever
# sees text.
#
# PROTOTYPE. This is a monkey patch of Open WebUI internals and can break on
# any upgrade. If process_tool_result is missing, the filter keeps working
# without tool anonymization and says so in the log.
# --------------------------------------------------------------------------

# Logged when the patch is installed. Open WebUI shows no version anywhere, so
# this is the only way to tell from a log which source is actually running.
_VERSION = "1.4.0"

# Marks a function this module has already wrapped, so that reloading the
# filter in Open WebUI does not stack wrapper upon wrapper.
_PATCH_FLAG = "_anymize_tool_wrapped"

# Where a wrapper keeps the function it replaced. Open WebUI re-execs the whole
# function module whenever its valves are edited or the source is updated, and
# everything the previous module object installed stays in place — wrappers
# that read the valves of the previous instance. Nothing is therefore ever
# skipped because it is "already wrapped": the wrapper is peeled off along this
# attribute first and rebuilt by the module loaded last.
_PATCH_ORIGINAL = "_anymize_tool_original"

# The Filter instance whose valves the wrapper reads. Open WebUI keeps one
# instance per function, and the wrapper lives outside the class, so the
# instance registers itself here in __init__.
_active_filter = None


def _unwrap(fn):
    """Strip wrappers of this module, including those of an older version."""

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


def _bind_tool_call(original, args, kwargs) -> Dict[str, Any]:
    """Resolve the arguments of a process_tool_result() call by name.

    Middleware calls positionally, but binding against the signature of the
    original keeps this working if Open WebUI reorders or adds parameters.
    """

    try:
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception as e:
        logging.warning(f"Anymize could not read the tool call arguments: {e}")
        return {}


def _describe_argument(name: str, value: Any) -> str:
    """One `name=value` pair for the call log, compact enough for a log line.

    The full payload of the parameters that can carry PII — the tool result
    and the metadata — is only written when the valve log_tool_payload is on.
    """

    payload = bool(_active_filter is not None and _active_filter.valves.log_tool_payload)

    if name == "tool_result" and not payload:
        # The result is exactly what this filter exists to keep out of places
        # like the log — type and size only, however short it is.
        size = f" len={len(value)}" if hasattr(value, "__len__") else ""
        return f"{name}=<{type(value).__name__}{size}>"

    if isinstance(value, (str, bool, int, float)) or value is None:
        if isinstance(value, str) and not payload and len(value) > 200:
            return f"{name}=<str len={len(value)}>"
        return f"{name}={value!r}"

    if isinstance(value, dict):
        if payload:
            return f"{name}={value!r}"
        # Enough to correlate a call with the inlet/outlet lines of the same
        # request without writing the whole dict into the log.
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


def _log_tool_call(arguments: Dict[str, Any]) -> None:
    """Log one call of process_tool_result() with all of its parameters.

    Driven by the bound arguments rather than a fixed list, so parameters
    added by a future Open WebUI version show up on their own.
    """

    try:
        fields = " ".join(
            _describe_argument(name, value) for name, value in arguments.items()
        )
        logging.warning(f"Anymize process_tool_result: {fields}")
    except Exception as e:
        logging.warning(f"Anymize process_tool_result logging failed: {e}")


async def _event_emitter_for(metadata: Optional[Dict[str, Any]]):
    """Build an event emitter for a request from its metadata dict.

    process_tool_result() gets no emitter: it lives in the caller as a closure
    variable and cannot be reached from here. Open WebUI builds its own the
    same way, from the very dict that is handed to process_tool_result() —
    get_event_emitter() reads user_id, chat_id and message_id from it and
    returns None when one of them is missing.

    Never raises: a broken status display must not break the anonymization.
    """

    if not metadata:
        return None

    try:
        from open_webui.socket.main import get_event_emitter

        emitter = get_event_emitter(metadata)
        # async in current versions, plain function in older ones
        if inspect.isawaitable(emitter):
            emitter = await emitter

        return emitter
    except Exception as e:
        logging.warning(f"Anymize could not build an event emitter: {e}")
        return None


def _parse_tool_patterns(raw: Optional[str]) -> List[str]:
    """Split a comma-separated valve into normalized match patterns."""

    if not raw:
        return []

    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _parse_label_ids(raw: Optional[str]) -> List[str]:
    """Split the comma-separated label_ids valve into single label IDs.

    Unlike _parse_tool_patterns() the entries keep their case: they are sent
    to the backend as they are, and label IDs may well be case-sensitive.
    """

    if not raw:
        return []

    return [part.strip() for part in raw.split(",") if part.strip()]


def _tool_excluded(tool_name: str, raw: Optional[str]) -> bool:
    """True when tool_name matches an entry of the exclusion valve.

    Case-insensitive; entries may use shell wildcards (`*`, `?`, `[...]`) so
    that a whole MCP server can be excluded with one line, e.g. `enaio_*`.
    fnmatchcase() rather than fnmatch(), because the latter additionally
    normalizes by platform rules — both sides are lowercased here already.

    Never raises: a broken pattern must not withhold a tool result.
    """

    name = (tool_name or "").strip().lower()
    if not name:
        return False

    try:
        return any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in _parse_tool_patterns(raw)
        )
    except Exception as e:
        logging.warning(f"Anymize tool_filter_exclude could not be evaluated: {e}")
        return False


async def _anonymize_tool_result(tool_result, arguments: Dict[str, Any]):
    """Hand one tool result to Filter.tool(). Fails closed."""

    if _active_filter is None:
        # No filter instance registered yet — nothing can be anonymized, and
        # withholding the result here would break tool calling for a filter
        # that is not even loaded.
        return tool_result

    try:
        return await _active_filter.tool(
            tool_result,
            __tool_name__=arguments.get("tool_function_name") or "?",
            __metadata__=arguments.get("metadata"),
            __user__=arguments.get("user"),
        )
    except Exception as e:
        # Fail closed: the raw result must never reach the model just because
        # the wrapper itself broke.
        logging.warning(f"Anymize tool interception failed: {e}")
        return (
            f"❌ Anonymization of the result of "
            f"{arguments.get('tool_function_name') or 'the tool'} failed: {e}. "
            f"The result was withheld."
        )


def _install_process_tool_result_patch() -> bool:
    """Patch middleware.process_tool_result. Idempotent across reloads."""

    try:
        from open_webui.utils import middleware
    except Exception as e:
        logging.warning(f"Anymize: no tool result interception ({e})")
        return False

    installed = getattr(middleware, "process_tool_result", None)
    if installed is None:
        logging.warning(
            "Anymize: middleware.process_tool_result not found — tool results are "
            "NOT anonymized on this Open WebUI version"
        )
        return False

    # Rebuild from the untouched function, so a patch of an earlier module
    # object is replaced instead of wrapped a second time.
    original = _unwrap(installed)

    @functools.wraps(original)
    async def patched(*args, **kwargs):
        # Logged before the original runs, so a call that hangs downstream is
        # still visible in the log.
        arguments = _bind_tool_call(original, args, kwargs)
        _log_tool_call(arguments)

        tool_result, tool_result_files, tool_result_embeds = await original(
            *args, **kwargs
        )
        tool_result = await _anonymize_tool_result(tool_result, arguments)
        return tool_result, tool_result_files, tool_result_embeds

    setattr(patched, _PATCH_FLAG, True)
    setattr(patched, _PATCH_ORIGINAL, original)
    middleware.process_tool_result = patched

    if installed is not original:
        logging.warning(
            "Anymize: replaced the process_tool_result patch of an older version"
        )

    logging.warning(f"Anymize {_VERSION}: process_tool_result patch installed")
    return True


class Filter:
    # Fields kept from each entry of GET /api/status/{job_id}/strings
    HASH_PAIR_FIELDS = (
        "original",
        "hash",
        "prefix_name",
        "placeholder",
    )

    # Keys used to hand the hash pairs from inlet() to outlet() via __metadata__
    METADATA_HASH_PAIRS_KEY = "_anymize_hash_pairs"
    METADATA_JOB_ID_KEY = "_anymize_job_id"

    # Job IDs of the tool results anonymized during this request, in call
    # order. Kept apart from METADATA_JOB_ID_KEY, which stays the job of the
    # input anonymization done in inlet().
    METADATA_TOOL_JOB_IDS_KEY = "_anymize_tool_job_ids"

    class Valves(BaseModel):
        backend_url: str = Field(
            default="https://app.anymize.ai",
            description=(
                "Base URL of the anymize backend, without trailing path. "
                "Change only to reach a self-hosted or staging instance."
            ),
        )

        anymize_api_key: str = Field(
            default="",
            description="Your anymize API key (format: anymize_xxxxxxxxxxxxx)",
        )

        language: str = Field(
            default="de",
            description="Language used for anonymization and OCR",
            json_schema_extra={
                "enum": [
                    "de",
                    "en",
                    "fr",
                    "es",
                    "it",
                ]
            },
        )

        label_ids: str = Field(
            default="",
            description=(
                "Comma-separated label IDs sent as 'label_ids' with every "
                "POST /api/anonymize call, restricting which entity types are "
                "detected and masked, e.g. 'PERSON, IBAN'. Values are sent "
                "unchanged, case-sensitive. Empty means the field is omitted "
                "and the backend applies its own default set of labels."
            ),
        )

        input_filter: str = Field(
            "text_anonymization",
            description="Controls how sensitive data in the input is processed before analysis",
            json_schema_extra={
                "enum": [
                    "text_anonymization",
                    "file_anonymization",
                    "text_file_anonymization",
                ]
            },
        )
        output_filter: str = Field(
            "deanonymized",
            description="Controls how sensitive data is handled in the response output",
            json_schema_extra={
                "enum": [
                    "anonymized",
                    "deanonymized",
                ]
            },
        )

        tool_filter: bool = Field(
            default=True,
            description=(
                "Anonymize the result of every tool call before it reaches the "
                "model. Needs the monkey patch of middleware.process_tool_result "
                "that is installed when this filter loads; tool results never "
                "reach inlet/stream/outlet. If the anonymization fails, the "
                "result is withheld from the model."
            ),
        )

        tool_filter_exclude: str = Field(
            default="",
            description=(
                "Comma-separated tool names whose result is NOT anonymized and "
                "is handed to the model unchanged. Matched against "
                "tool_function_name, case-insensitive, with shell wildcards: "
                "'get_time, enaio_*'. Only effective while tool_filter is on. "
                "WARNING: the result of an excluded tool reaches the model in "
                "clear text. Empty means every tool result is anonymized."
            ),
        )

        store_hash_pairs: bool = Field(
            default=False,
            description=(
                "After every successful anonymization, fetch the placeholder-to-"
                "original mapping from GET /api/status/{job_id}/strings and keep it "
                "in __metadata__ for the rest of the request; outlet() then logs the "
                "collected table once. WARNING: writes the original values in clear "
                "text into the server log. Off means the mapping is never fetched."
            ),
        )

        log_tool_payload: bool = Field(
            default=False,
            description=(
                "Log the result of each tool call before and after anonymization. "
                "WARNING: writes the raw tool result in clear text into the server "
                "log. Off means one compact line per tool result with lengths only."
            ),
        )

        priority: int = Field(
            default=10, description="Filter execution order, Lower values run first"
        )
        pass

    def __init__(self):
        global _active_filter

        self.toggle = True
        self.valves = self.Valves()

        # The tool result wrapper lives at module level and reads the valves
        # from here.
        _active_filter = self
        _install_process_tool_result_patch()
        self.icon = """data:image/svg+xml,%3C%3Fxml%20version%3D%221.0%22%20encoding%3D%22UTF-8%22%3F%3E%3Csvg%20id%3D%22Ebene_2%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2050.57%22%3E%3Cg%20id%3D%22Ebene_1-2%22%3E%3Cpath%20d%3D%22M62.5%2C43.07c-.97-.62-1.94-1.41-1.94-3.01v-22.54c0-7.63-3.88-12.48-9.98-14.75v39.66s.06-.05.08-.08c.62%2C3.54%2C3.45%2C5.75%2C7.87%2C5.75%2C3.01%2C0%2C5.48-.97%2C5.48-2.83%2C0-1.06-.71-1.59-1.5-2.21Z%22%2F%3E%3Cpath%20d%3D%22M21.13%2C36.09c0-10.78%2C12.73-14.85%2C29.34-15.03v-5.39c0-7.34-4.15-10.78-10.08-10.78s-6.89%2C3.36-7.69%2C6.54c-.71%2C2.83-1.33%2C5.39-5.21%2C5.39-3.01%2C0-4.69-1.77-4.69-4.42%2C0-5.3%2C6.54-11.14%2C18.38-11.14%2C3.47%2C0%2C6.65.5%2C9.38%2C1.52V0H0v50.57h50.57v-8.14c-3.53%2C3.58-8.62%2C5.67-14.59%2C5.67-8.84%2C0-14.85-4.68-14.85-12.02Z%22%2F%3E%3Cpath%20d%3D%22M31.03%2C33.96c0%2C5.13%2C4.51%2C8.57%2C10.16%2C8.57%2C3.71%2C0%2C7.16-1.5%2C9.28-3.98v-12.46c-1.59-.8-3.45-1.06-5.75-1.06-8.04%2C0-13.7%2C3.27-13.7%2C8.93Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E"""
        pass

    @property
    def base_url(self) -> str:
        """Backend base URL from the valve, trailing slashes removed.

        Resource paths are joined as f"{base_url}{resource}" and already
        start with "/", so a trailing slash in the valve would produce
        "//api/...". A valve emptied in the UI falls back to the default
        rather than sending the requests to a relative path.
        """

        return self.valves.backend_url.strip().rstrip("/") or "https://app.anymize.ai"

    async def _anymize_api_request(
        self,
        method: str,
        resource: str,
        body: Dict[str, Any] = {},
        qs: Dict[str, Any] = {},
    ) -> Dict[str, Any]:

        headers = {
            "Authorization": f"Bearer {self.valves.anymize_api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}{resource}"

        async with aiohttp.ClientSession() as session:
            if method == "POST":
                async with session.post(
                    url, headers=headers, json=body, params=qs
                ) as response:
                    return await response.json()
            elif method == "GET":
                async with session.get(url, headers=headers, params=qs) as response:
                    return await response.json()

    async def _poll_status(
        self,
        job_id: str,
        max_retries: int = 150,
        retry_interval: int = 500,
        error_message: str = "Anonymization timeout: Process did not complete within expected time",
    ) -> Dict[str, Any]:

        for i in range(max_retries):
            response = await self._get_anonymization_status(job_id)
            if response["status"] == "completed":
                return response
            await asyncio.sleep(retry_interval / 1000)  # Convert ms to seconds

        raise Exception(error_message)

    async def _anonymize_text(
        self,
        text: str,
        language: str = "de",
        label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:

        body: Dict[str, Any] = {
            "text": text,
            "language": language,
        }

        # An empty selection stays out of the body entirely, so the backend
        # keeps applying its own default set of labels.
        if label_ids:
            body["label_ids"] = label_ids

        return await self._anymize_api_request("POST", "/api/anonymize", body)

    async def _get_anonymization_status(self, job_id: str) -> Dict[str, Any]:

        return await self._anymize_api_request("GET", f"/api/status/{job_id}")

    async def _get_hash_pairs(self, job_id: str) -> Dict[str, Any]:

        return await self._anymize_api_request("GET", f"/api/status/{job_id}/strings")

    async def _store_hash_pairs(
        self,
        job_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        is_tool_result: bool = False,
    ) -> List[Dict[str, Any]]:

        if not self.valves.store_hash_pairs:
            return []

        try:
            response = await self._get_hash_pairs(job_id)
        except Exception as e:
            logging.warning(f"Anonymize hash pairs for job {job_id} unavailable: {e}")
            return []

        hash_pairs = [
            {field: pair.get(field) for field in self.HASH_PAIR_FIELDS}
            for pair in response.get("hash_pairs", [])
        ]

        if metadata is not None:
            # Append instead of assign: a request runs one anonymization for the
            # user message and one more per tool result, and the pairs of the
            # earlier jobs must survive.
            metadata[self.METADATA_HASH_PAIRS_KEY] = (
                metadata.get(self.METADATA_HASH_PAIRS_KEY) or []
            ) + hash_pairs

            if is_tool_result:
                metadata[self.METADATA_TOOL_JOB_IDS_KEY] = (
                    metadata.get(self.METADATA_TOOL_JOB_IDS_KEY) or []
                ) + [job_id]
            else:
                metadata[self.METADATA_JOB_ID_KEY] = job_id

        if not hash_pairs:
            logging.warning(f"Anonymize hash pairs for job {job_id}: none returned")

        return hash_pairs

    def _log_hash_pairs(self, metadata: Optional[Dict[str, Any]]) -> None:
        """Log the mapping collected during this request, once, at the end.

        The pairs of every job of the request are written in one place instead
        of one block per anonymization. Nothing was collected when the valve
        store_hash_pairs is off, and then nothing is logged either.
        """

        if metadata is None:
            return

        hash_pairs = metadata.get(self.METADATA_HASH_PAIRS_KEY) or []
        if not hash_pairs:
            return

        job_id = metadata.get(self.METADATA_JOB_ID_KEY)
        tool_job_ids = metadata.get(self.METADATA_TOOL_JOB_IDS_KEY) or []

        pairs = "\n".join(
            "  "
            + ", ".join(f"{field}={pair[field]!r}" for field in self.HASH_PAIR_FIELDS)
            for pair in hash_pairs
        )
        logging.warning(
            f"Anonymize hash pairs of this request "
            f"(input job {job_id}, tool jobs {tool_job_ids}, "
            f"{len(hash_pairs)} entries):\n{pairs}"
        )

    async def _deanonymize_text(self, text: str) -> Dict[str, Any]:

        body = {
            "text": text,
        }

        return await self._anymize_api_request("POST", "/api/deanonymize", body)

    # file anonymization methods
    async def upload_file_from_path_for_ocr(self, file_path: str) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.valves.anymize_api_key}"}
        file_name = os.path.basename(file_path)

        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()

            with open(file_path, "rb") as file:
                data.add_field("file", file, filename=file_name)
                data.add_field("language", self.valves.language)

                async with session.post(
                    f"{self.base_url}/api/ocr", headers=headers, data=data
                ) as response:
                    return await response.json()

    def get_file_paths(self, body: Dict[str, Any]) -> List[str]:
        try:
            files = body.get("files", [])
            file_paths = []

            for file_entry in files:
                file_info = file_entry.get("file", {})
                file_id = file_info.get("id")
                file_name = file_info.get("filename")

                if file_id and file_name:
                    full_path = os.path.join(UPLOAD_DIR, f"{file_id}_{file_name}")
                    file_paths.append(full_path)
                else:
                    print(f"Missing file_id or filename for file: {file_info}")
            return file_paths
        except (KeyError, TypeError) as e:
            print(f"Error retrieving file paths: {e}")
            return []

    async def process_multiple_files_for_ocr(
        self, file_paths: List[str], event_emitter=None
    ) -> List[Dict[str, Any]]:

        if not file_paths:
            return []

        try:
            upload_tasks = [
                self.upload_file_from_path_for_ocr(file_path)
                for file_path in file_paths
            ]
            upload_responses = await asyncio.gather(
                *upload_tasks, return_exceptions=True
            )

            job_ids = []
            for i, response in enumerate(upload_responses):
                if isinstance(response, Exception):
                    print(f"Failed to upload file {file_paths[i]}: {response}")
                    continue

                job_id = response.get("job_id")
                if job_id:
                    job_ids.append(job_id)
                else:
                    print(f"No job_id returned for file {file_paths[i]}: {response}")

            # Step 3: Poll all job_ids concurrently
            if not job_ids:
                print("No valid job_ids to poll")
                return []

            polling_tasks = [self._poll_status(job_id) for job_id in job_ids]
            results = await asyncio.gather(*polling_tasks, return_exceptions=True)

            # Step 4: Handle polling results
            successful_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"Failed to poll job_id {job_ids[i]}: {result}")
                else:
                    successful_results.append(result)

            return successful_results

        except Exception as e:
            print(f"Error processing multiple files: {e}")
            if event_emitter:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": f"❌ Anonymization failed: {str(e)}",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return []

    async def _anonymize_content(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        is_tool_result: bool = False,
        context: str = "",
    ) -> tuple:
        """Anonymize one piece of text end to end: submit, poll, mask.

        Returns the masked text and the completed status response, so the
        caller can still reach fields like "systemprompt". Shared by
        process_input() and tool() so both take the same path.
        """

        response = await self._anonymize_text(
            text,
            self.valves.language,
            _parse_label_ids(self.valves.label_ids),
        )
        job_id = response["job_id"]
        logging.warning(f"Anonymize JobID: {job_id}{context}")

        result = await self._poll_status(job_id)

        # Collects the mapping in __metadata__ for outlet() to log, and only
        # when the valve store_hash_pairs is set. The masked text itself always
        # comes from the API and does not depend on it.
        await self._store_hash_pairs(job_id, metadata, is_tool_result)

        logging.warning(
            f"Anonymize job {job_id}: using the anonymized text from the API"
        )
        final_content = result["anonymized_text_raw"]

        return final_content, result

    async def process_input(
        self,
        body,
        input_filter: str,
        event_emitter,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        content_to_anonymize = ""
        system_prompt = ""

        # Collect content based on input_filter
        if input_filter in ["file_anonymization", "text_file_anonymization"]:
            # Process files
            file_paths = self.get_file_paths(body)
            if file_paths:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": "Processing files...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

                ocr_results = await self.process_multiple_files_for_ocr(
                    file_paths, event_emitter
                )
                if ocr_results:
                    file_texts = [
                        result.get("anonymized_text_raw", "")
                        for result in ocr_results
                        if result.get("anonymized_text_raw")
                    ]
                    content_to_anonymize += "\n\n".join(file_texts)
                    system_prompt = ocr_results[0].get("systemprompt", "")

        if input_filter in [
            "text_anonymization",
            "text_file_anonymization",
            "file_anonymization",
        ]:
            # Add text content
            last_message = get_last_user_message(body["messages"])
            if last_message:
                if content_to_anonymize:  # If we already have file content
                    content_to_anonymize += f"\n\n{last_message}"
                else:
                    content_to_anonymize = last_message

        if not content_to_anonymize:
            return body

        # Anonymize combined content if we have any
        if content_to_anonymize and input_filter in [
            "text_anonymization",
            "text_file_anonymization",
        ]:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": "Anonymizing content...",
                        "done": False,
                        "hidden": False,
                    },
                }
            )

            final_content, result = await self._anonymize_content(
                content_to_anonymize, metadata
            )

            # Combine anonymized content with system prompt
            if result.get("systemprompt"):
                final_content += f"\n\n{result['systemprompt']}"
            elif system_prompt:  # Fallback to OCR system prompt if available
                final_content += f"\n\n{system_prompt}"
        else:
            final_content = content_to_anonymize
            if system_prompt:
                final_content += f"\n\n{system_prompt}"

        # Update the last user message
        for message in reversed(body["messages"]):
            if message["role"] == "user":
                message["content"] = final_content
                break

        await event_emitter(
            {
                "type": "status",
                "data": {
                    "description": "",
                    "done": False,
                    "hidden": True,
                },
            }
        )

        return body

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.toggle:
            return body

        try:
            return await self.process_input(
                body,
                input_filter=self.valves.input_filter,
                event_emitter=__event_emitter__,
                metadata=__metadata__,
            )

        except Exception as e:

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"❌ Anonymization failed: {str(e)}",
                        "done": True,
                        "hidden": False,
                    },
                }
            )

            raise Exception(f"Anonymization failed: {str(e)}")

    async def tool(
        self,
        tool_result: Optional[str],
        __tool_name__: str = "?",
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Anonymize the result of one tool call before the model sees it.

        Not an Open WebUI hook: it is called from the monkey patch of
        middleware.process_tool_result() installed when this module loads,
        after the original has normalized the result to a string.

        Fails closed. If the anonymization fails, the raw result is replaced by
        an error message instead of being handed to the model in clear text —
        the model then sees a failed tool call and the chat keeps running.
        """

        if not self.toggle or not self.valves.tool_filter:
            return tool_result

        if not isinstance(tool_result, str) or not tool_result.strip():
            return tool_result

        # Checked before the emitter is built: an excluded tool produces no
        # status in the chat at all, exactly as if tool_filter were off.
        if _tool_excluded(__tool_name__, self.valves.tool_filter_exclude):
            if self.valves.log_tool_payload:
                logging.warning(
                    f"Anonymize tool_result skipped: name={__tool_name__} "
                    f"{tool_result!r}"
                )
            else:
                logging.warning(
                    f"Anonymize tool_result skipped: name={__tool_name__} "
                    f"len={len(tool_result)} — excluded by tool_filter_exclude, "
                    f"handed to the model unchanged"
                )
            return tool_result

        # Built from __metadata__ rather than passed in — see _event_emitter_for().
        # Stays None outside a live chat request; every status is then skipped.
        event_emitter = await _event_emitter_for(__metadata__)

        async def status(description: str, done: bool, hidden: bool) -> None:
            if event_emitter is None:
                return
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": description,
                            "done": done,
                            "hidden": hidden,
                        },
                    }
                )
            except Exception as e:
                logging.warning(f"Anonymize tool_result status failed: {e}")

        try:
            if self.valves.log_tool_payload:
                logging.warning(
                    f"Anonymize tool_result before: name={__tool_name__} "
                    f"{tool_result!r}"
                )

            await status(f"Anonymizing result of {__tool_name__}...", False, False)

            final_content, _ = await self._anonymize_content(
                tool_result,
                __metadata__,
                is_tool_result=True,
                context=f" (tool {__tool_name__})",
            )

            # The systemprompt of the status response is deliberately not
            # appended here: it belongs on the user message, where inlet()
            # already put it, not into a role="tool" message.

            if self.valves.log_tool_payload:
                logging.warning(
                    f"Anonymize tool_result after: name={__tool_name__} "
                    f"{final_content!r}"
                )
            else:
                logging.warning(
                    f"Anonymize tool_result: name={__tool_name__} "
                    f"len={len(tool_result)} -> {len(final_content)}"
                )

            await status("", False, True)

            return final_content

        except Exception as e:
            logging.warning(
                f"Anonymize tool_result failed: name={__tool_name__} "
                f"len={len(tool_result)}: {e} — result withheld from the model"
            )
            await status(
                f"❌ Anonymization of the result of {__tool_name__} failed: {e}",
                True,
                False,
            )
            return (
                f"❌ Anonymization of the result of {__tool_name__} failed: {e}. "
                f"The result was withheld."
            )

    async def stream(
        self,
        event: dict,
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Pass every chunk of a streamed response through untouched.

        De-anonymization happens in outlet() on the finished message. Every
        parameter but the event has a default because it is not guaranteed
        which extras Open WebUI injects on the stream path.
        """

        return event

    async def outlet(
        self,
        body: dict,
        __event_emitter__,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.toggle:
            return body

        # Own try/except and placed before the de-anonymization: the table is
        # logged even when POST /api/deanonymize fails, and a broken log line
        # never costs the response.
        try:
            self._log_hash_pairs(__metadata__)
        except Exception as e:
            logging.warning(f"Anonymize hash pair logging failed: {e}")

        try:
            assistant_message = get_last_assistant_message(body["messages"])
            if self.valves.output_filter == "deanonymized":
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "De-anonymizing content....",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

                result = await self._deanonymize_text(assistant_message)
                if result.get("text", "").strip():
                    for message in reversed(body["messages"]):
                        if message["role"] == "assistant":
                            message["content"] = result["text"]
                            break

                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "",
                            "done": True,
                            "hidden": True,
                        },
                    }
                )

            return body

        except Exception as e:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"❌ De-anonymization failed: {str(e)}",
                        "done": True,
                        "hidden": False,
                    },
                }
            )

            for message in reversed(body["messages"]):
                if message["role"] == "assistant":
                    message["content"] = (
                        f"❌ De-anonymization failed: {str(e)}\n\nOriginal response: {message['content']}"
                    )
                    break

            return body
