"""stdio relay for the MCP server — ``krypt-polybot-us-backend --mcp``.

Some AI clients (Claude Desktop, most notably) only launch MCP servers as a
child process speaking newline-delimited JSON-RPC on stdin/stdout. The real
server lives inside the RUNNING app (``mcp_server.py``), because that is where
the account, the books and the ledgers are; this process just carries lines
between the two.

It finds the app through ``<userData>/mcp/bridge.json`` (port + pid, no
secret) and reads the bearer token from the encrypted credential store
itself, so the client's config file holds a path and nothing else.
``--agent <id>`` picks which of the user's named agents this client is: the
relay sends THAT agent's token, and the token is what the server trusts. No
flag = the built-in Default agent, which is every config made before named
agents existed.

If the app is closed, the relay still answers ``initialize`` (or, from a
2026-07-28 client, ``server/discover``) and ``tools/list`` — a client that
sees a dead server at startup usually hides it until restarted — and every
tool call says, in words, that the app needs to be open with AI agent access
on.

stdout carries protocol only. Nothing else may ever be written to it, so this
module imports nothing that logs to stdout, and diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

_out_lock = threading.Lock()
_state_lock = threading.Lock()
_session_id: Optional[str] = None
_init_params: Optional[dict] = None
_need_init = True
_agent_id = "default"

NOT_RUNNING = (
    "Krypt PolyBot US is not reachable. Open the app, go to AI Agents and turn "
    "on 'Allow AI agents' — then try again. (The app must stay open while an "
    "agent uses it: the account and the order books live there.)")
NO_TOKEN = (
    "The agent this client is set up as no longer exists in Krypt PolyBot US, or "
    "has no token. Re-add it from the AI Agents page.")


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[krypt-polybot-us mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _send(obj: Any) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _endpoint() -> Optional[tuple[str, str]]:
    """(url, token) for the running app, or None."""
    try:
        import mcp_server
        info = json.loads(mcp_server.bridge_path().read_text(encoding="utf-8"))
        port = int(info["port"])
        token = mcp_server.load_token(_agent_id, fresh=True)
    except Exception:  # noqa: BLE001
        return None
    if not token:
        return None
    return f"http://127.0.0.1:{port}/mcp", token


def _why_unreachable() -> str:
    """Why _endpoint() found nothing. The app writes bridge.json when it starts
    serving agents, so a readable one with no token for this agent means the
    agent is gone (deleted, its token pruned) or the config names one that
    never existed — opening the app would not fix that."""
    try:
        import mcp_server
        json.loads(mcp_server.bridge_path().read_text(encoding="utf-8"))["port"]
    except Exception:  # noqa: BLE001
        return NOT_RUNNING
    return NO_TOKEN


class _Unreachable(Exception):
    pass


def _post(payload: Any, *, timeout: float = 900.0) -> tuple[Optional[Any], Optional[str]]:
    """POST one message; → (parsed JSON body or None for 202, session id)."""
    ep = _endpoint()
    if ep is None:
        raise _Unreachable(_why_unreachable())
    url, token = ep
    import mcp_server
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Host": url.split("/")[2],
    }
    modern = (isinstance(payload, dict) and "id" in payload
              and mcp_server.request_era(payload.get("params"))[0] is not None)
    if modern:
        # Over HTTP a modern request repeats its version, method and name in
        # headers; the client spoke stdio and sent none, so we add them.
        headers.update(mcp_server.modern_headers(payload))
    else:
        with _state_lock:
            if _session_id:
                headers["Mcp-Session-Id"] = _session_id
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            sid = r.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            # The modern revision answers some JSON-RPC errors with these
            # statuses (an unknown method is a 404). The error in the body is
            # the answer; the status only says what kind it is.
            try:
                err = json.loads(e.read() or b"null")
            except (ValueError, OSError):
                err = None
            if isinstance(err, dict) and "error" in err:
                return err, None
        if e.code == 401:
            raise _Unreachable(
                "The app refused this agent's token — the agent may have been "
                "deleted. Check it on the AI Agents page.")
        if e.code == 403:
            raise _Unreachable(
                "This agent is switched off in Krypt PolyBot US. Switch it back on "
                "on the AI Agents page.")
        if e.code == 429:
            raise _Unreachable("Rate limit: too many calls in a minute. Slow down.")
        if e.code == 503:
            raise _Unreachable(NOT_RUNNING)
        raise _Unreachable(f"The app answered HTTP {e.code}.")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        _log(f"bridge unreachable: {e}")
        raise _Unreachable(NOT_RUNNING)
    return (json.loads(body) if body else None), sid


def _upstream_init() -> Optional[str]:
    """(Re)introduce this client to the app, so the AI Agents page shows its
    real name. Done lazily: the app may have started after the client did, or
    restarted since. → the app's instructions for this agent."""
    global _session_id, _need_init
    with _state_lock:
        params = _init_params
    if params is None:
        return None
    resp, sid = _post({"jsonrpc": "2.0", "id": "relay-init", "method": "initialize",
                       "params": params}, timeout=15)
    with _state_lock:
        _session_id = sid
        _need_init = False
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
    result = resp.get("result") if isinstance(resp, dict) else None
    text = result.get("instructions") if isinstance(result, dict) else None
    return text if isinstance(text, str) and text else None


def _local_initialize(msg: dict, instructions: Optional[str] = None) -> dict:
    """`instructions` is the app's text for this agent (its guide and hard
    rules). The app is the only place that knows them — the agents live in
    its config, and this process has none — so with the app closed the
    generic text stands in."""
    import mcp_server
    asked = str((msg.get("params") or {}).get("protocolVersion") or "")
    version = asked if asked in mcp_server.PROTOCOL_VERSIONS else mcp_server.PROTOCOL_VERSIONS[0]
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
        "protocolVersion": version,
        "capabilities": mcp_server.CAPABILITIES,
        "serverInfo": mcp_server.server_info(),
        "instructions": instructions or mcp_server.INSTRUCTIONS,
    }}


def _handle(msg: Any) -> None:
    global _init_params, _need_init
    if not isinstance(msg, dict):
        _send({"jsonrpc": "2.0", "id": None,
               "error": {"code": -32600, "message": "invalid request"}})
        return
    import mcp_server
    method = msg.get("method")
    rid = msg.get("id")
    is_request = "id" in msg and isinstance(method, str)
    # The modern revision (2026-07-28): every request carries its own version.
    # Checked here too, so a client probing a version we don't speak is told
    # which ones we do even with the app closed.
    modern, bad = mcp_server.request_era(msg.get("params"))
    if bad is not None:
        if is_request:
            _send({"jsonrpc": "2.0", "id": rid, "error": bad})
        return

    if method == "server/discover" and modern and is_request:
        # A modern client's opening probe. The app's answer carries this
        # agent's guide and rules; with the app closed we still answer, or a
        # client that hears nothing falls back to the old handshake.
        try:
            resp, _ = _post(msg, timeout=15)
        except _Unreachable:
            resp = None
        _send(resp if isinstance(resp, dict) else {
            "jsonrpc": "2.0", "id": rid,
            "result": mcp_server.discover_result(mcp_server.INSTRUCTIONS)})
        return

    if method == "initialize":
        with _state_lock:
            _init_params = dict(msg.get("params") or {})
            _need_init = True
        # Introduce the client to the app first: its answer carries this
        # agent's guide and hard rules. Answered locally either way, so a
        # client started before the app still comes up with every tool
        # listed. (instructions_for cannot run here: this process has no
        # config, and importing the service would wire its logging to
        # stdout, which is the protocol.)
        try:
            instructions = _upstream_init()
        except _Unreachable:
            instructions = None
        _send(_local_initialize(msg, instructions))
        return

    if method == "notifications/initialized":
        return  # sent upstream by _upstream_init

    if method in ("tools/list", "ping", "prompts/list", "prompts/get"):
        # Served locally: these are static, and answering them must not
        # depend on the app being open.
        import mcp_tools
        try:
            if method == "tools/list":
                result: dict = {"tools": mcp_tools.list_tools()}
            elif method == "prompts/list":
                result = {"prompts": mcp_server.list_prompts()}
            elif method == "prompts/get":
                p = msg.get("params") or {}
                result = mcp_server.get_prompt(str(p.get("name") or ""),
                                               p.get("arguments") or {})
            else:
                result = {}
        except (KeyError, ValueError) as e:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32602, "message": f"bad prompt request: {e}"}})
            return
        _send({"jsonrpc": "2.0", "id": rid,
               "result": mcp_server.modern_result(method, result) if modern else result})
        return

    try:
        with _state_lock:
            need = _need_init
        if need:
            _upstream_init()
        resp, _ = _post(msg)
        if resp is not None and is_request:
            _send(resp)
    except _Unreachable as e:
        if not is_request:
            return
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": mcp_server.modern_result(method, result) if modern else result})
        else:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32603, "message": str(e)}})
        with _state_lock:
            _need_init = True


def _parse_agent(argv: list[str]) -> str:
    if "--agent" in argv:
        i = argv.index("--agent")
        if i + 1 < len(argv):
            return argv[i + 1].strip().lower()
    return "default"


_inflight: set = set()


def _end_session() -> None:
    """Tell the app this client has gone, so it can say so instead of showing it as
    connected. Best effort: a client closing must never wait on the app."""
    with _state_lock:
        sid = _session_id
    ep = _endpoint() if sid else None
    if ep is None:
        return
    url, token = ep
    req = urllib.request.Request(url, method="DELETE", headers={
        "Authorization": f"Bearer {token}", "Host": url.split("/")[2], "Mcp-Session-Id": sid,
    })
    try:
        with urllib.request.urlopen(req, timeout=3.0) as r:
            r.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        _log(f"could not end the session: {e}")


def _spawn(msg: Any) -> None:
    """Handle one message on its own thread (a slow tool call must not hold
    up a ping), remembered until it finishes so EOF can wait for it."""
    def _run() -> None:
        try:
            _handle(msg)
        finally:
            _inflight.discard(threading.current_thread())
    t = threading.Thread(target=_run, daemon=True)
    _inflight.add(t)
    t.start()


def main() -> int:
    global _agent_id
    _agent_id = _parse_agent(sys.argv)
    _log(f"relay started (agent {_agent_id})")
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            # stdin closed. Let calls already in flight answer before exiting:
            # a client that writes a request and closes its end still gets
            # its reply (daemon threads would otherwise die with us).
            deadline = time.monotonic() + 60.0
            for t in list(_inflight):
                t.join(max(0.0, deadline - time.monotonic()))
            _end_session()
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, list):
            for m in msg:
                _spawn(m)
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            _handle(msg)  # in order: nothing may overtake initialize
            continue
        _spawn(msg)
