"""One script backtest, run in a child process: how an AI agent's
`backtest_script` executes code it wrote.

Strategy scripts are full Python with no language sandbox (script_sandbox.py
says why). Run inside the backend, an agent's script shares the process that
holds the decrypted wallet key, the CLOB credentials and every log handler,
and a clean-auditing backtest could return the key in its own tool result
(2026-10-11 audit). So an agent's backtest runs here instead.

What the child gets, and what it does not (2026-10-12 audit, C2/H2):

  • CODE: code an agent wrote has already passed the agent allowlist
    (script_audit.validate_agent) in the parent — pure computation, no
    imports beyond a small stdlib set, no dunders, no getattr/open/eval.
    That is the primary barrier; everything below is containment for when a
    barrier fails.
  • DATA: never the real database. The parent builds a purpose-built
    EXTRACT in a fresh temporary directory (`build_extract`): only the
    recorded research rows the backtest reads (crypto ticks + windows,
    whale trades, momentum alerts), for the requested period. No app_kv
    (remote-control pairing), no agent audit log, no trade tables, no
    settings. The child opens that copy `mode=ro&immutable=1`. Its path is
    the only database path it is told.
  • PATHS: userData, the vault, APPDATA, LOCALAPPDATA, USERPROFILE and HOME
    all point into that temporary directory, and secret-looking environment
    variables are removed, so the app's own path logic cannot lead it to the
    real profile or the DPAPI-protected credential files.
  • PROCESSES (Windows): the parent puts the child in a Job Object with an
    active-process limit of 1 (it cannot start a program), kill-on-close,
    and UI restrictions (no clipboard, no desktop switching) — see `contain`.
  • NETWORK: sockets refused inside the process before the script is
    compiled. Python code could undo that, which is why the allowlist and
    the job exist.
  • stdout is reserved for the result (the script's prints go to stderr),
    and the parent kills it if it outlives the backtest's wall-clock cap.

Residual risk, stated plainly: the child still runs under the user's Windows
account at the user's integrity level. A script that escaped the allowlist
could read files that account can read by absolute path, and DPAPI would
decrypt for it. A low-integrity / AppContainer token would close that and is
not done here (it needs CreateProcessAsUser plumbing that asyncio's
subprocess API does not expose). The permission that allows any of this
stays off by default, and Autopilot never gets it.

Protocol: one JSON object on stdin
  {"db", "cfg", "code", "assets", "env", "sinceDays"}
and one JSON object on stdout: script_backtest.run's result, or
{"scriptError": "..."} when the run itself failed.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


class NetworkOff(OSError):
    pass


def _refuse(*_a, **_k):
    raise NetworkOff("the network is off in a script backtest")


def _no_network() -> None:
    """Refuse sockets for everything that runs after this. Python code can
    undo it; it stops the casual and accidental cases."""
    class _NoSocket(socket.socket):
        def __init__(self, *_a, **_k):
            _refuse()
    socket.socket = _NoSocket            # type: ignore[misc]
    socket.create_connection = _refuse   # type: ignore[assignment]
    socket.getaddrinfo = _refuse         # type: ignore[assignment]
    socket.socketpair = _refuse          # type: ignore[assignment]


def _read_only_db(path: Path) -> None:
    """Point db at the extract the parent built, opened read-only and
    immutable: the backtest only reads, and the child is never told where
    the user's real database is."""
    import db

    def _open_ro(p: str) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{Path(p).resolve().as_uri()}?mode=ro&immutable=1",
                               uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    db.db_path = lambda: path
    db._open_conn = _open_ro


# ── parent side: the extract ─────────────────────────────────────────────

# The tables script_backtest reads, and how much of each the extract keeps.
# `{days}` is the requested period plus a day of slack (timestamps are stored
# in two textual formats; the child applies the exact cut itself).
_EXTRACT: tuple[tuple[str, str, str], ...] = (
    # table, time column (archive de-dup key with id), WHERE
    ("crypto15m_ticks", "observed_at",
     "observed_at >= datetime('now', :days) AND network = :env"),
    ("crypto15m_signals", "observed_at",
     "network = :env AND ticker IN (SELECT ticker FROM main.crypto15m_ticks)"),
    ("whale_trades", "created_at", "created_at >= datetime('now', :days)"),
    ("alerts", "created_at", "created_at >= datetime('now', :days)"),
)


def _uri(p: Path, mode: str) -> str:
    return f"{p.resolve().as_uri()}?mode={mode}"


def build_extract(src: Path, dest: Path, since_days: int, env: str,
                  archive: Optional[Path] = None) -> dict[str, int]:
    """Copy just the research rows a script backtest reads from the real DB
    (`src`, attached READ-ONLY) and its research archive into a new SQLite
    file at `dest`. Returns rows copied per table.

    Run in the PARENT. The child is handed `dest`, never `src`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_uri(dest, "rwc"), uri=True, timeout=30)
    counts: dict[str, int] = {}
    try:
        con.execute("ATTACH DATABASE ? AS src", (_uri(src, "ro"),))
        has_arch = False
        if archive is not None and archive.exists():
            try:
                con.execute("ATTACH DATABASE ? AS arch", (_uri(archive, "ro"),))
                has_arch = True
            except sqlite3.Error:
                has_arch = False
        params = {"days": f"-{int(since_days) + 1} days", "env": str(env)}
        for table, tcol, where in _EXTRACT:
            info = list(con.execute(f"PRAGMA src.table_info({table})"))
            if not info:
                continue
            cols = [r[1] for r in info]
            decl = ", ".join(f'"{r[1]}" {r[2] or ""}'.strip() for r in info)
            con.execute(f"CREATE TABLE main.{table} ({decl})")
            collist = ", ".join(f'"{c}"' for c in cols)
            con.execute(f"INSERT INTO main.{table} ({collist}) "
                        f"SELECT {collist} FROM src.{table} WHERE {where}", params)
            if has_arch:
                arch_cols = {r[1] for r in con.execute(f"PRAGMA arch.table_info({table})")}
                if arch_cols and "id" in arch_cols and tcol in arch_cols:
                    con.execute(f"CREATE INDEX main.kp_x_{table} ON {table}(id)")
                    sel = ", ".join(f'a."{c}"' if c in arch_cols else "NULL" for c in cols)
                    try:
                        # A restored backup can hold rows the archive already
                        # has; count those once (same rule as db.research_source).
                        con.execute(
                            f"INSERT INTO main.{table} ({collist}) SELECT {sel} "
                            f"FROM arch.{table} a WHERE {_qualify(where)} "
                            f"AND NOT EXISTS (SELECT 1 FROM main.{table} m "
                            f'WHERE m.id = a.id AND m."{tcol}" = a."{tcol}")', params)
                    except sqlite3.Error:
                        pass    # an archive from an older schema: live rows only
            con.commit()
            counts[table] = int(con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
        con.execute("DETACH DATABASE src")
        if has_arch:
            con.execute("DETACH DATABASE arch")
    finally:
        con.close()
    return counts


def _qualify(where: str) -> str:
    """The extract WHEREs, reading the archive row `a`. Each column the
    clause names is qualified so `ticker IN (SELECT ticker FROM main…)`
    compares the ARCHIVE row's ticker."""
    out = where
    for c in ("observed_at", "created_at", "network"):
        out = out.replace(f"{c} ", f'a."{c}" ')
    out = out.replace("AND ticker IN", 'AND a."ticker" IN')
    return out


# ── parent side: the Job Object ──────────────────────────────────────────

def contain(pid: int) -> Any:
    """Put process `pid` in a new Windows Job Object that allows ONE active
    process (so it cannot start another program), kills it if the job handle
    closes, and denies clipboard / desktop / global-atom / system-setting
    access. Returns the job handle (keep it until the child exits, then
    `release` it), or None off Windows or when the OS refuses (logged by the
    caller; the allowlist still stands)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Ext(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _UI(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicUIRestrictions = 4
    JobObjectExtendedLimitInformation = 9
    UI_ALL = 0xFF   # handles, clipboard r/w, system params, display, atoms, desktop, exit
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    ok = False
    try:
        ext = _Ext()
        ext.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        ext.BasicLimitInformation.ActiveProcessLimit = 1
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(ext), ctypes.sizeof(ext)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject(limits) failed")
        ui = _UI(UI_ALL)
        if not k32.SetInformationJobObject(job, JobObjectBasicUIRestrictions,
                                           ctypes.byref(ui), ctypes.sizeof(ui)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject(ui) failed")
        hp = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not hp:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not k32.AssignProcessToJobObject(job, hp):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            k32.CloseHandle(hp)
        ok = True
        return job
    finally:
        if not ok:
            k32.CloseHandle(job)


def release(job: Any) -> None:
    """Close a job handle from `contain` (kills anything still in it)."""
    if not job or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle(job)


# ── child side ───────────────────────────────────────────────────────────

def main() -> int:
    out = sys.stdout
    # the script's own prints must not corrupt the one JSON line we answer with
    sys.stdout = sys.stderr
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        _read_only_db(Path(req["db"]))
        import script_backtest   # imported before the network goes off
        _no_network()
        res = script_backtest.run(
            dict(req.get("cfg") or {}), str(req.get("code") or ""),
            assets=req.get("assets"), env=str(req.get("env") or "mainnet"),
            since_days=int(req.get("sinceDays") or 60),
        )
    except Exception as e:  # noqa: BLE001 — anything at all is the answer
        res = {"scriptError": f"{type(e).__name__}: {e}", "n": 0, "trades": [],
               "caveats": [f"The isolated backtest failed: {e}"]}
    out.write(json.dumps(res, default=str))
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
