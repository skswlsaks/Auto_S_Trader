#!/usr/bin/env python3
"""Model Arena MCP proxy: connects any stdio MCP client (Claude Code / Desktop, Codex, Cursor ...) to the
hosted Model Arena server on Amazon Bedrock AgentCore, signing in with the team's Cognito account.

Standard library only (Python 3.9+). One-time setup:

    python3 arena_mcp_proxy.py setup <setup-string from the admin>
    python3 arena_mcp_proxy.py login        # opens the browser (Cognito), stores tokens (chmod 600)
    python3 arena_mcp_proxy.py test         # lists the server's tools

Then register `python3 /path/to/arena_mcp_proxy.py` as a stdio MCP server in your AI tool.
Local file paths in tool arguments (trades files, model files, experiment folders) are read here and
sent as content, because the hosted server cannot see your disk.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HOME = Path(os.environ.get("ARENA_MCP_HOME", Path.home() / ".config" / "model-arena"))
CONFIG, CREDS = HOME / "config.json", HOME / "credentials.json"
CALLBACK_PORT = 8976
STUDY_FILES = ("trades.parquet", "protocol.json", "study.json", "REPORT.md", "summary.csv", "summary.json", "results.json", "holdout-lock.json")
_lock = threading.Lock()
_protocol_version: str | None = None


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _write_private(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def config() -> dict:
    if not CONFIG.exists():
        raise SystemExit("not set up: run `arena_mcp_proxy.py setup <setup-string>` (ask the Model Arena admin)")
    return json.loads(CONFIG.read_text())


# ---------------------------------------------------------------- Cognito (authorization code + PKCE)

def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _claims(jwt: str) -> dict:
    p = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def _save_tokens(tok: dict, refresh: str | None = None):
    c = _claims(tok["id_token"])
    _write_private(CREDS, {"id_token": tok["id_token"], "refresh_token": tok.get("refresh_token") or refresh,
                           "expires_at": c["exp"], "email": c.get("email")})


def login():
    cfg = config()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, redirect = secrets.token_urlsafe(16), f"http://localhost:{CALLBACK_PORT}/callback"
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = got.get("state") == state and "code" in got
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<p>Model Arena 로그인 완료. 이 창을 닫아도 됩니다.</p>" if ok else "<p>로그인 실패. 다시 시도하세요.</p>").encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), H)
    url = f"https://{cfg['domain']}/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect, "scope": "openid email profile",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    eprint(f"브라우저에서 로그인하세요 (자동으로 열리지 않으면 이 주소를 여세요):\n{url}")
    webbrowser.open(url)
    srv.timeout = 300
    while "code" not in got and "error" not in got:
        srv.handle_request()
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"login failed: {got.get('error', 'state mismatch')}")
    tok = _post_form(f"https://{cfg['domain']}/oauth2/token", {"grant_type": "authorization_code", "client_id": cfg["client_id"],
                                                                 "code": got["code"], "redirect_uri": redirect, "code_verifier": verifier})
    _save_tokens(tok)
    eprint(f"로그인됨: {json.loads(CREDS.read_text())['email']}")


def id_token(force_refresh: bool = False) -> str:
    if not CREDS.exists():
        raise PermissionError("Model Arena 로그인이 필요합니다: python3 arena_mcp_proxy.py login")
    cr = json.loads(CREDS.read_text())
    if force_refresh or cr["expires_at"] - time.time() < 120:
        cfg = config()
        try:
            tok = _post_form(f"https://{cfg['domain']}/oauth2/token", {"grant_type": "refresh_token", "client_id": cfg["client_id"], "refresh_token": cr["refresh_token"]})
        except urllib.error.HTTPError as e:
            raise PermissionError("로그인이 만료되었습니다: python3 arena_mcp_proxy.py login") from e
        _save_tokens(tok, refresh=cr["refresh_token"])
        cr = json.loads(CREDS.read_text())
    return cr["id_token"]


# ---------------------------------------------------------------- local paths -> content

def _git_info(start: Path) -> dict | None:
    def git(*a):
        r = subprocess.run(["git", "-C", str(start), *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    commit = git("rev-parse", "HEAD")
    if not commit:
        return None
    status = git("status", "--porcelain") or ""
    remote = (git("config", "--get", "remote.origin.url") or "").removesuffix(".git")
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.split(":", 1)[1]
    return {"repo": remote or None, "commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status.strip()), "dirty_files": [l[3:] for l in status.splitlines() if l.strip()][:20]}


def _b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def transform(name: str, args: dict) -> dict:
    args = dict(args or {})
    if name == "arena_create_run":
        tp = args.pop("trades_path", None)
        if tp:
            p = Path(tp).expanduser()
            if p.suffix == ".json":
                args["trades"] = json.loads(p.read_text())
            else:
                args["trades_file_b64"], args["trades_format"] = _b64(p), p.suffix.lstrip(".")
        arts = args.pop("artifacts", None)
        if arts:
            args["artifacts_b64"] = {Path(a).name: _b64(Path(a).expanduser()) for a in arts}
        ins = args.pop("inputs", None)
        if ins:
            args["inputs_info"] = [{"path": Path(i).name, "sha256": hashlib.sha256(Path(i).expanduser().read_bytes()).hexdigest(),
                                    "bytes": Path(i).expanduser().stat().st_size} for i in ins]
        if "code" not in args:
            info = _git_info(Path(tp).expanduser().parent if tp else Path.cwd())
            if info:
                args["code"] = info
    elif name in ("arena_preview_study", "arena_submit_study"):
        folder = args.pop("folder", None)
        if folder:
            f = Path(folder).expanduser().resolve()
            args["files_b64"] = {n: _b64(f / n) for n in STUDY_FILES if (f / n).exists()}
            args["study_name"] = f"{f.parent.name}/{f.name}"
    return args


# ---------------------------------------------------------------- JSON-RPC forwarding

def _send_up(body: bytes, retry: bool = True) -> list[dict]:
    cfg = config()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {id_token()}"}
    if _protocol_version:
        headers["MCP-Protocol-Version"] = _protocol_version
    req = urllib.request.Request(cfg["url"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw, ctype = r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            id_token(force_refresh=True)
            return _send_up(body, retry=False)
        raise
    if not raw:
        return []
    if "text/event-stream" in ctype:
        return [json.loads(line[5:]) for line in raw.decode().splitlines() if line.startswith("data:") and line[5:].strip()]
    msg = json.loads(raw)
    return msg if isinstance(msg, list) else [msg]


def _emit(obj: dict):
    with _lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _handle(line: str):
    global _protocol_version
    try:
        msg = json.loads(line)
    except ValueError:
        return
    mid = msg.get("id")
    try:
        if msg.get("method") == "tools/call":
            p = msg.setdefault("params", {})
            p["arguments"] = transform(p.get("name", ""), p.get("arguments"))
        for out in _send_up(json.dumps(msg).encode()):
            if msg.get("method") == "initialize" and "result" in out:
                _protocol_version = out["result"].get("protocolVersion")
            _emit(out)
    except PermissionError as e:
        if mid is not None:
            _emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32001, "message": str(e)}})
    except Exception as e:  # network, file and server errors go back to the client as JSON-RPC errors
        if mid is not None:
            detail = e.read().decode()[:300] if isinstance(e, urllib.error.HTTPError) else ""
            _emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"Model Arena proxy: {e} {detail}".strip()}})


def serve():
    workers = []
    for line in sys.stdin:
        if line.strip():
            t = threading.Thread(target=_handle, args=(line,), daemon=True)
            t.start()
            workers.append(t)
    for t in workers:  # stdin closed: finish in-flight requests before exiting
        t.join(timeout=300)


# ---------------------------------------------------------------- CLI

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "setup":
        if len(sys.argv) < 3:
            raise SystemExit("usage: arena_mcp_proxy.py setup <setup-string>")
        cfg = json.loads(base64.urlsafe_b64decode(sys.argv[2] + "=" * (-len(sys.argv[2]) % 4)))
        _write_private(CONFIG, cfg)
        eprint(f"saved {CONFIG}. Next: python3 {sys.argv[0]} login")
    elif cmd == "login":
        login()
    elif cmd == "logout":
        CREDS.unlink(missing_ok=True)
        eprint("logged out")
    elif cmd == "whoami":
        cr = json.loads(CREDS.read_text()) if CREDS.exists() else {}
        eprint(json.dumps({"email": cr.get("email"), "token_expires_in_s": int(cr.get("expires_at", 0) - time.time()) if cr else None,
                           "server": config().get("url")}, ensure_ascii=False))
    elif cmd == "test":
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                              "clientInfo": {"name": "arena-proxy-test", "version": "1"}}}
        res = _send_up(json.dumps(init).encode())
        eprint("server:", res[0]["result"]["serverInfo"])
        tools = _send_up(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())[0]["result"]["tools"]
        who = _send_up(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "arena_whoami", "arguments": {}}}).encode())[0]
        eprint(f"tools: {len(tools)}; whoami: {who['result']['content'][0]['text']}")
    elif cmd == "serve":
        serve()
    else:
        raise SystemExit(f"unknown command {cmd}: setup | login | logout | whoami | test | serve")


if __name__ == "__main__":
    main()
