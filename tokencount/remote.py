"""Talking to the TokenCounting API, and to GitHub for `tokencount login`.

Standard library only. Everything stored locally lives in ~/.tokencount
(or $TOKENCOUNT_HOME): the anonymous contributor key, the GitHub login and
which records were already sent.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .records import home_dir

# Filled in once the public API and the project's GitHub OAuth App exist.
DEFAULT_API_URL: str | None = None
DEFAULT_GITHUB_CLIENT_ID: str | None = None

BATCH_SIZE = 25  # keeps each request well within the API's CPU budget
RETRY_DELAYS = (1.0, 3.0)  # seconds between attempts after a network error
USER_AGENT = f"tokencount/{__version__}"


class RemoteError(Exception):
    pass


def api_url(override: str | None = None) -> str | None:
    url = override or os.environ.get("TOKENCOUNT_API") or DEFAULT_API_URL
    return url.rstrip("/") if url else None


def github_client_id() -> str | None:
    return os.environ.get("TOKENCOUNT_GITHUB_CLIENT_ID") or DEFAULT_GITHUB_CLIENT_ID


def _github_urls() -> tuple[str, str]:
    return (
        os.environ.get("TOKENCOUNT_GITHUB_URL", "https://github.com").rstrip("/"),
        os.environ.get("TOKENCOUNT_GITHUB_API_URL", "https://api.github.com").rstrip("/"),
    )


def request(
    method: str,
    url: str,
    *,
    json_body: object = None,
    form: dict | None = None,
    headers: dict | None = None,
    timeout: float = 60,
) -> tuple[int, dict]:
    """Send a request and return (status, parsed JSON body). Retries network errors."""
    data = None
    all_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == len(RETRY_DELAYS):
                raise RemoteError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {exc}") from exc
            time.sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def _parse(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"error": raw.decode("utf-8", "replace")[:300]}
    return body if isinstance(body, dict) else {"data": body}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- identity -----------------------------------------------------------------


def contributor_key() -> str:
    """Random key that lets the API recognise repeat anonymous submissions."""
    path = home_dir() / "contributor_key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    _write_private(path, key + "\n")
    return key


def load_github() -> dict | None:
    try:
        data = json.loads((home_dir() / "github.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("token") else None


def save_github(token: str, login: str) -> None:
    _write_private(home_dir() / "github.json", json.dumps({"token": token, "login": login}) + "\n")


def forget_github() -> bool:
    path = home_dir() / "github.json"
    if path.exists():
        path.unlink()
        return True
    return False


def device_login(client_id: str, out: Callable[[str], None], sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:
    """GitHub OAuth device flow. Returns (token, login). The token carries no scopes."""
    web, api = _github_urls()
    status, start = request("POST", f"{web}/login/device/code", form={"client_id": client_id, "scope": ""})
    if status != 200 or "device_code" not in start:
        raise RemoteError(f"GitHub did not start the login ({status}): {start.get('error_description') or start.get('error')}")
    out(f"Open {start['verification_uri']} and enter the code {start['user_code']}")
    interval = float(start.get("interval", 5))
    deadline = time.monotonic() + float(start.get("expires_in", 900))
    token = None
    while time.monotonic() < deadline:
        sleep(interval)
        _, poll = request(
            "POST",
            f"{web}/login/oauth/access_token",
            form={
                "client_id": client_id,
                "device_code": start["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if poll.get("access_token"):
            token = poll["access_token"]
            break
        error = poll.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise RemoteError(f"GitHub login failed: {poll.get('error_description') or error}")
    if token is None:
        raise RemoteError("The login code expired before it was entered. Run tokencount login again.")
    status, user = request("GET", f"{api}/user", headers={"Authorization": f"Bearer {token}"})
    if status != 200 or "login" not in user:
        raise RemoteError(f"GitHub did not return your account ({status}).")
    return token, user["login"]


# --- what was already sent ----------------------------------------------------


def digest(record: dict) -> str:
    """Fingerprint of a record's content. The importer version is left out, so upgrading
    tokencount doesn't resend everything."""
    content = {k: v for k, v in record.items() if k != "importer"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class SubmitState:
    """Digest of every record each API has accepted, so unchanged records aren't resent."""

    def __init__(self, path: Path | None = None):
        self.path = path or home_dir() / "submitted.json"
        try:
            self.data: dict[str, dict[str, str]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def is_current(self, api: str, record: dict) -> bool:
        return self.data.get(api, {}).get(record["record_id"]) == digest(record)

    def mark(self, api: str, record: dict) -> None:
        self.data.setdefault(api, {})[record["record_id"]] = digest(record)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1) + "\n", encoding="utf-8")


def post_records(api: str, records: list[dict], headers: dict) -> tuple[int, dict]:
    return request("POST", f"{api}/v1/records", json_body={"records": records}, headers=headers, timeout=120)
