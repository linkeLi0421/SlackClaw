from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class SlackApiError(Exception):
    endpoint: str
    error: str
    payload: dict

    def __str__(self) -> str:
        return f"Slack API error in {self.endpoint}: {self.error}"


class SlackWebClient:
    def __init__(self, token: str) -> None:
        self._token = token

    def api_call(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        token: str | None = None,
        retry_429_once: bool = True,
    ) -> dict:
        url = f"https://slack.com/api/{endpoint}"
        if params:
            url = url + "?" + urllib.parse.urlencode(params)

        headers = {"Authorization": f"Bearer {token or self._token}"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and retry_429_once:
                retry_after = exc.headers.get("Retry-After")
                wait_seconds = 1
                if retry_after:
                    try:
                        wait_seconds = max(1, int(retry_after))
                    except ValueError:
                        wait_seconds = 1
                time.sleep(wait_seconds)
                return self.api_call(
                    method,
                    endpoint,
                    params=params,
                    json_body=json_body,
                    retry_429_once=False,
                )
            details = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Slack HTTP error {exc.code} in {endpoint}: {details}") from exc
        except Exception as exc:  # pragma: no cover - network exceptions depend on env
            raise RuntimeError(f"Slack request failed in {endpoint}: {exc}") from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Slack returned invalid JSON in {endpoint}") from exc

        if not payload.get("ok"):
            raise SlackApiError(endpoint=endpoint, error=payload.get("error") or "unknown_error", payload=payload)
        return payload

    def auth_test(self) -> dict:
        return self.api_call("POST", "auth.test", json_body={})

    def conversations_history(
        self,
        *,
        channel_id: str,
        oldest: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, str | int] = {
            "channel": channel_id,
            "limit": limit,
            "inclusive": "false",
        }
        if oldest:
            params["oldest"] = oldest
        if cursor:
            params["cursor"] = cursor
        return self.api_call("GET", "conversations.history", params=params)

    def chat_post_message(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict] | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "channel": channel_id,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        return self.api_call("POST", "chat.postMessage", json_body=payload)

    def apps_connections_open(self, *, app_token: str) -> dict:
        return self.api_call("POST", "apps.connections.open", json_body={}, token=app_token)

    def upload_file(
        self,
        *,
        channel_id: str,
        filepath: str,
        filename: str = "",
        title: str = "",
        thread_ts: str | None = None,
        initial_comment: str = "",
    ) -> dict:
        import os

        if not filename:
            filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)

        # Step 1: get upload URL (form-encoded — JSON silently fails for this endpoint)
        form_data = urllib.parse.urlencode({"filename": filename, "length": file_size}).encode("utf-8")
        step1_req = urllib.request.Request(
            "https://slack.com/api/files.getUploadURLExternal",
            data=form_data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(step1_req, timeout=30) as resp:
                url_resp = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"files.getUploadURLExternal request failed: {exc}") from exc
        if not url_resp.get("ok"):
            raise RuntimeError(f"files.getUploadURLExternal failed: {url_resp.get('error', 'unknown')}")
        upload_url = str(url_resp.get("upload_url") or "")
        file_id = str(url_resp.get("file_id") or "")
        if not upload_url or not file_id:
            raise RuntimeError("files.getUploadURLExternal did not return upload_url or file_id")

        # Step 2: POST file bytes to the upload URL (no Bearer token)
        with open(filepath, "rb") as f:
            file_bytes = f.read()
        boundary = "----SlackClawUploadBoundary"
        body_parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n",
        ]
        body = body_parts[0].encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        post_req = urllib.request.Request(upload_url, data=body, method="POST")
        post_req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(post_req, timeout=60) as _resp:
                pass
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"file upload POST failed ({exc.code}): {details}") from exc
        except Exception as exc:
            raise RuntimeError(f"file upload POST request failed: {exc}") from exc

        # Step 3: complete upload
        file_entry: dict[str, str] = {"id": file_id}
        if title:
            file_entry["title"] = title
        complete_body: dict[str, object] = {
            "files": [file_entry],
            "channel_id": channel_id,
        }
        if thread_ts:
            complete_body["thread_ts"] = thread_ts
        if initial_comment:
            complete_body["initial_comment"] = initial_comment
        return self.api_call("POST", "files.completeUploadExternal", json_body=complete_body)

    def download_private_file(self, url: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Slack file download failed ({exc.code}): {details}") from exc
        except Exception as exc:  # pragma: no cover - network exceptions depend on env
            raise RuntimeError(f"Slack file download request failed: {exc}") from exc
