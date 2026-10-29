from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import atomic_json

BASE = "https://openapi.tossinvest.com"
KEYS = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCESS_TOKEN")


def credentials(path: Path) -> dict:
    values = {}
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError("Credential file must have mode 600; run chmod 600 on that file")
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or key not in KEYS:
                raise ValueError("Credential file contains an unsupported key or invalid assignment")
            parts = shlex.split(value, comments=True)
            if len(parts) > 1:
                raise ValueError("Quote credential values containing whitespace")
            values[key] = parts[0] if parts else ""
    for key in KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def credential_status(path):
    c = credentials(Path(path))
    return {"file_exists": Path(path).exists(), "client_id_set": bool(c.get(KEYS[0])),
            "client_secret_set": bool(c.get(KEYS[1])), "access_token_set": bool(c.get(KEYS[2]))}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TossAPI:
    """Only public market-data GETs and the OAuth token POST are allowed."""

    def __init__(self, credential_path, min_interval=.35, transport=None, sleeper=time.sleep):
        self.values = credentials(Path(credential_path))
        self.cache_path = Path(credential_path).parent / ".toss-token-cache.json"
        self.min_interval = min_interval
        self.transport = transport or urllib.request.build_opener(NoRedirect()).open
        self.sleep = sleeper
        self.last_request = 0.
        self.token = self.values.get("TOSS_ACCESS_TOKEN")
        self.expires = float("inf") if self.token else 0.
        self.client_hash = hashlib.sha256(self.values.get("TOSS_CLIENT_ID", "").encode()).hexdigest()
        if not self.token and self.cache_path.exists():
            if self.cache_path.stat().st_mode & 0o077:
                raise ValueError("Token cache must have mode 600")
            cached = json.loads(self.cache_path.read_text())
            if cached.get("client_hash") == self.client_hash:
                self.token, self.expires = cached.get("access_token"), cached.get("expires_at", 0)

    def _request(self, request):
        for attempt in range(6):
            self.sleep(max(0., self.min_interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.transport(request, timeout=20) as response:
                    body = json.load(response)
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    if remaining == "0":
                        self.sleep(1.)
                    return body
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise RuntimeError(f"Toss {request.get_method()} failed: HTTP {error.code}; response omitted to protect credentials") from None
                retry = error.headers.get("Retry-After", "")
                self.sleep(min(60., float(retry) if retry.isdigit() else 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 5:
                    raise RuntimeError("Toss network request failed after retries; no request/credential data logged") from None
                self.sleep(min(30., 2 ** attempt))
        raise RuntimeError("Unreachable API retry state")

    def access_token(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        if not all(self.values.get(k) for k in KEYS[:2]):
            raise ValueError("Fill TOSS_CLIENT_ID and TOSS_CLIENT_SECRET in .env.toss.local, or set TOSS_ACCESS_TOKEN")
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.values[KEYS[0]],
                                       "client_secret": self.values[KEYS[1]]}).encode()
        request = urllib.request.Request(BASE + "/oauth2/token", data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        body = self._request(request)
        if not isinstance(body.get("access_token"), str) or not body["access_token"] or not 0 < body.get("expires_in", 0) < 10**8:
            raise ValueError("Invalid OAuth response; body omitted")
        self.token, self.expires = body["access_token"], time.time() + body["expires_in"]
        atomic_json(self.cache_path, {"client_hash": self.client_hash, "access_token": self.token,
                                     "expires_at": self.expires}, mode=0o600)
        return self.token

    def get(self, path, **query):
        if path not in ("/api/v1/candles", "/api/v1/sectors", "/api/v1/stocks/all") and not re.fullmatch(r"/api/v1/sectors/[A-Za-z0-9_-]{1,40}/(?:stocks|etfs)", path):
            raise ValueError("Read-only endpoint allowlist rejected path")
        request = urllib.request.Request(BASE + path + "?" + urllib.parse.urlencode(query),
                                         headers={"Authorization": "Bearer " + self.access_token()}, method="GET")
        body = self._request(request)
        if not isinstance(body, dict) or body.get("result") is None:
            raise ValueError("Toss response missing result; body omitted")
        return body["result"]
