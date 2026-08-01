"""
title: anymize
author: bbojan
author_url: https://github.com/Bojan227
version: 1.0.0
"""

import asyncio
import aiohttp
import os
import logging
from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Set, Any

from open_webui.utils.misc import get_last_user_message, get_last_assistant_message
from open_webui.config import UPLOAD_DIR

logger = logging.getLogger(__name__)


class Filter:
    # Fields kept from each entry of GET /api/status/{job_id}/strings
    HASH_PAIR_FIELDS = (
        "original",
        "hash",
        "prefix_name",
        "internal_id",
        "placeholder",
    )

    # Keys used to hand the hash pairs from inlet() to outlet() via __metadata__
    METADATA_HASH_PAIRS_KEY = "_anymize_hash_pairs"
    METADATA_JOB_ID_KEY = "_anymize_job_id"

    # Chunk counter of stream(), kept per request next to the hash pairs
    METADATA_STREAM_CHUNKS_KEY = "_anymize_stream_chunks"

    # Field of a hash pair that carries the full token as it appears in the
    # anonymized text, e.g. "[[internal_id-S28MBV]]"
    HASH_PAIR_REPLACEMENT_FIELD = "placeholder"

    class Valves(BaseModel):
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

        allowed_categories: str = Field(
            default="",
            description=(
                "Comma-separated PII categories (hash pair prefix_name) to mask. "
                "WARNING: setting this switches anonymization to the local path — "
                "the message is masked from the hash pair table instead of using the "
                "text returned by the API, and anything outside these categories "
                "reaches the model in clear text. Empty means all categories."
            ),
        )

        disallowed_categories: str = Field(
            default="",
            description=(
                "Comma-separated PII categories to leave unmasked; wins over "
                "allowed_categories. WARNING: setting this switches anonymization to "
                "the local path — values of these categories reach the model in clear "
                "text. Empty means none are excluded."
            ),
        )

        priority: int = Field(
            default=10, description="Filter execution order, Lower values run first"
        )
        pass

    def __init__(self):
        self.toggle = True
        self.valves = self.Valves()
        self.icon = """data:image/svg+xml,%3C%3Fxml%20version%3D%221.0%22%20encoding%3D%22UTF-8%22%3F%3E%3Csvg%20id%3D%22Ebene_2%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2050.57%22%3E%3Cg%20id%3D%22Ebene_1-2%22%3E%3Cpath%20d%3D%22M62.5%2C43.07c-.97-.62-1.94-1.41-1.94-3.01v-22.54c0-7.63-3.88-12.48-9.98-14.75v39.66s.06-.05.08-.08c.62%2C3.54%2C3.45%2C5.75%2C7.87%2C5.75%2C3.01%2C0%2C5.48-.97%2C5.48-2.83%2C0-1.06-.71-1.59-1.5-2.21Z%22%2F%3E%3Cpath%20d%3D%22M21.13%2C36.09c0-10.78%2C12.73-14.85%2C29.34-15.03v-5.39c0-7.34-4.15-10.78-10.08-10.78s-6.89%2C3.36-7.69%2C6.54c-.71%2C2.83-1.33%2C5.39-5.21%2C5.39-3.01%2C0-4.69-1.77-4.69-4.42%2C0-5.3%2C6.54-11.14%2C18.38-11.14%2C3.47%2C0%2C6.65.5%2C9.38%2C1.52V0H0v50.57h50.57v-8.14c-3.53%2C3.58-8.62%2C5.67-14.59%2C5.67-8.84%2C0-14.85-4.68-14.85-12.02Z%22%2F%3E%3Cpath%20d%3D%22M31.03%2C33.96c0%2C5.13%2C4.51%2C8.57%2C10.16%2C8.57%2C3.71%2C0%2C7.16-1.5%2C9.28-3.98v-12.46c-1.59-.8-3.45-1.06-5.75-1.06-8.04%2C0-13.7%2C3.27-13.7%2C8.93Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E"""
        pass

    @property
    def local_processing(self) -> bool:
        """True when at least one of the category valves is set.

        Read on access rather than stored in __init__: Open WebUI assigns
        self.valves after construction and again whenever the valves are
        edited, so a value computed in __init__ would only ever see the
        defaults.
        """

        return bool(
            self._parse_categories(self.valves.allowed_categories)
            or self._parse_categories(self.valves.disallowed_categories)
        )

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

        url = f"https://app.anymize.ai{resource}"

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
        retry_interval: int = 10000,
        error_message: str = "Anonymization timeout: Process did not complete within expected time",
    ) -> Dict[str, Any]:

        for i in range(max_retries):
            response = await self._get_anonymization_status(job_id)
            if response["status"] == "completed":
                return response
            await asyncio.sleep(retry_interval / 1000)  # Convert ms to seconds

        raise Exception(error_message)

    async def _anonymize_text(self, text: str, language: str = "de") -> Dict[str, Any]:

        body = {
            "text": text,
            "language": language,
        }

        return await self._anymize_api_request("POST", "/api/anonymize", body)

    async def _get_anonymization_status(self, job_id: str) -> Dict[str, Any]:

        return await self._anymize_api_request("GET", f"/api/status/{job_id}")

    async def _get_hash_pairs(self, job_id: str) -> Dict[str, Any]:

        return await self._anymize_api_request("GET", f"/api/status/{job_id}/strings")

    async def _store_hash_pairs(
        self, job_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:

        try:
            response = await self._get_hash_pairs(job_id)
        except Exception as e:
            logging.warning(f"Anymize.ai hash pairs for job {job_id} unavailable: {e}")
            return []

        hash_pairs = [
            {field: pair.get(field) for field in self.HASH_PAIR_FIELDS}
            for pair in response.get("hash_pairs", [])
        ]

        if metadata is not None:
            metadata[self.METADATA_HASH_PAIRS_KEY] = hash_pairs
            metadata[self.METADATA_JOB_ID_KEY] = job_id

        if not hash_pairs:
            logging.warning(f"Anymize.ai hash pairs for job {job_id}: none returned")
            return hash_pairs

        pairs = "\n".join(
            "  "
            + ", ".join(f"{field}={pair[field]!r}" for field in self.HASH_PAIR_FIELDS)
            for pair in hash_pairs
        )
        logging.warning(
            f"Anymize.ai hash pairs for job {job_id} "
            f"({response.get('total', len(hash_pairs))} entries):\n{pairs}"
        )

        return hash_pairs

    def _parse_categories(self, value: str) -> Set[str]:

        return {part.strip().lower() for part in value.split(",") if part.strip()}

    def _filter_hash_pairs(
        self, hash_pairs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:

        allowed = self._parse_categories(self.valves.allowed_categories)
        disallowed = self._parse_categories(self.valves.disallowed_categories)

        if not allowed and not disallowed:
            return hash_pairs

        filtered = []
        for pair in hash_pairs:
            category = (pair.get("prefix_name") or "").strip().lower()
            if category in disallowed:  # disallowed wins over allowed
                continue
            if allowed and category not in allowed:
                continue
            filtered.append(pair)

        skipped = len(hash_pairs) - len(filtered)
        if skipped:
            logging.warning(
                f"Anymize.ai local anonymization: {skipped} of {len(hash_pairs)} hash pairs "
                f"skipped by category valves (allowed={sorted(allowed)}, "
                f"disallowed={sorted(disallowed)}) — values of a skipped category stay "
                f"in clear text"
            )

        return filtered

    def _anonymize_locally(
        self, text: str, hash_pairs: List[Dict[str, Any]]
    ) -> str:

        hash_pairs = self._filter_hash_pairs(hash_pairs)

        replacements = [
            (pair["original"], pair[self.HASH_PAIR_REPLACEMENT_FIELD])
            for pair in hash_pairs
            if pair.get("original") and pair.get(self.HASH_PAIR_REPLACEMENT_FIELD)
        ]

        # Longest originals first: a shorter value that is a substring of a
        # longer one ("Berlin" in "Berliner Str. 42, 10115 Berlin") would
        # otherwise cut the longer one apart before it can be replaced.
        replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

        for original, placeholder in replacements:
            text = text.replace(original, placeholder)

        return text

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
                    "https://app.anymize.ai/api/ocr", headers=headers, data=data
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

            response = await self._anonymize_text(
                content_to_anonymize, self.valves.language
            )
            logging.warning(f"Anymize.ai JobID: {response['job_id']}")
            result = await self._poll_status(response["job_id"])
            hash_pairs = await self._store_hash_pairs(response["job_id"], metadata)

            if self.local_processing and hash_pairs:
                # Category valves are set: mask locally from the hash pairs
                # instead of using the text the API returned.
                final_content = self._anonymize_locally(
                    content_to_anonymize, hash_pairs
                )
                logging.warning(
                    f"Anymize.ai job {response['job_id']}: anonymized locally from "
                    f"{len(hash_pairs)} hash pairs (before category filtering)"
                )
                if final_content == content_to_anonymize:
                    logging.warning(
                        f"Anymize.ai local anonymization for job {response['job_id']} "
                        f"replaced nothing — the message goes to the model unmasked"
                    )
            else:
                # Either no category valve is set, or there are no hash pairs to
                # work with. The latter is what Zero Data Retention looks like
                # from here: ZDR is an account setting, not a request parameter,
                # and with it enabled anymize.ai keeps no placeholder-to-original
                # mapping, so GET /api/status/{job_id}/strings returns nothing.
                # The local path would then replace nothing and send the original
                # message in clear text, so fall back to the text the API masked.
                if self.local_processing:
                    logging.warning(
                        f"Anymize.ai job {response['job_id']}: no hash pairs available "
                        f"(Zero Data Retention enabled?) — using the anonymized text "
                        f"from the API instead of local anonymization"
                    )
                else:
                    logging.warning(
                        f"Anymize.ai job {response['job_id']}: using the anonymized "
                        f"text from the API"
                    )
                final_content = result["anonymized_text_raw"]

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

    async def stream(
        self,
        event: dict,
        __event_emitter__=None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Log every chunk of a streamed response and pass it through untouched.

        Prototype: this hook observes only. De-anonymization still happens in
        outlet() on the finished message. Every parameter but the event has a
        default because it is not guaranteed which extras Open WebUI injects
        on the stream path.
        """

        if not self.toggle:
            return event

        try:
            # The counter lives in __metadata__, not on the instance: Open WebUI
            # shares one Filter across all users and chats, so instance state
            # would mix up concurrent responses.
            chunk = "?"
            if __metadata__ is not None:
                chunk = __metadata__.get(self.METADATA_STREAM_CHUNKS_KEY, 0) + 1
                __metadata__[self.METADATA_STREAM_CHUNKS_KEY] = chunk

            logging.warning(f"Anymize.ai stream chunk {chunk}: {event!r}")

        except Exception as e:
            # Never raise from here — an exception would tear down the response
            # mid-stream, and this hook only observes.
            logging.warning(f"Anymize.ai stream logging failed: {e}")

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
