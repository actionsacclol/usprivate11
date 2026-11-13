"""The alerts in the bottom-right corner: what is wrong right now, and what to do.

Each alert is a condition that is true at this moment, rebuilt on every poll. It
goes away by itself once the condition does. An alert states a fault, or something
the user asked for that isn't happening. A setting left off is not a fault, so a
fresh install on Paper has no alerts at all.

`build` is pure: service.py gathers the state, this turns it into alerts, so every
line of wording and every condition can be tested without a socket.

Alert shape: {id, severity: error|warn, title, detail, fix, action}. `fix` is never
empty, because an alert with nothing to do is only noise. `action` is one of:
    {kind: "nav", page, label}, {kind: "copy", text, label}, {kind: "resume", label}
"""
from __future__ import annotations

import re
from typing import Optional

ERROR, WARN = "error", "warn"

# Long enough for the key check and the feeds to finish on a normal start.
STARTUP_GRACE_SEC = 30.0

_API_NAME = {"gateway": "market data", "api": "trading"}


def _alert(id: str, severity: str, title: str, detail: str, fix: str,
           action: Optional[dict] = None) -> dict:
    return {"id": id, "severity": severity, "title": title, "detail": detail,
            "fix": fix, "action": action}


def _nav(page: str, label: str) -> dict:
    return {"kind": "nav", "page": page, "label": label}


def _duration(sec: int) -> str:
    if sec < 90:
        return f"{sec} seconds"
    return f"{round(sec / 60)} minutes"


def _api_alerts(outages: dict, has_key: bool) -> list[dict]:
    """One alert for the exchange when both sides fail the same way."""
    hosts = [h for h in ("gateway", "api") if h in outages and (h == "gateway" or has_key)]
    if not hosts:
        return []
    kinds = {outages[h].get("kind") for h in hosts}
    if len(hosts) == 2 and len(kinds) == 1:
        hosts = ["gateway"]
        what = "market data and trading"
    else:
        what = " and ".join(_API_NAME[h] for h in hosts) if len(kinds) == 1 else ""
    out = []
    for host in hosts:
        o = outages[host]
        name = what or _API_NAME[host]
        since = _duration(int(o.get("sinceSec") or 0))
        kind = o.get("kind")
        rid = f"api:{host}"
        if kind == "rate":
            out.append(_alert(
                rid, WARN, "Polymarket US is limiting requests",
                f"It has been refusing {name} requests for {since} because too many were sent.",
                "The app slows down and retries by itself. If this stays, turn off engines "
                "you aren't using, and close any other program using this API key."))
        elif kind == "server":
            out.append(_alert(
                rid, ERROR, "Polymarket US is having problems",
                f"Its {name} service has been returning errors for {since}. "
                "Prices and orders may not work.",
                "Nothing is wrong on your side. The app keeps retrying and this clears "
                "by itself once Polymarket US recovers. Its live status is at "
                "status.polymarketexchange.com."))
        else:
            out.append(_alert(
                rid, ERROR, "Can't reach Polymarket US",
                f"No {name} response for {since}. Prices and orders may not work.",
                "Check your internet connection and firewall. If other sites load, Polymarket "
                "US may be down or in maintenance (see status.polymarketexchange.com); this "
                "clears by itself when it's back."))
    return out


def _key_alerts(s: dict, api_down: bool, starting: bool) -> list[dict]:
    has_key, live = bool(s.get("hasKey")), bool(s.get("accountLive"))
    setup = _nav("api", "Open API Key page")
    if not has_key:
        if not live:
            return []
        return [_alert("key", ERROR, "No API key, and the app is Live",
                       "Nothing can trade until a key is saved.",
                       "Add your key on the API Key page, or switch back to Paper in Settings.",
                       _nav("api", "Set up account"))]
    # A failed check while the network is down says nothing about the key.
    if s.get("authOk") or api_down or starting:
        return []
    if not s.get("keyRejected"):
        return [_alert("key", ERROR if live else WARN, "Your API key couldn't be checked",
                       "The app couldn't confirm it with Polymarket US, so it isn't "
                       "treating your account as connected.",
                       "It keeps retrying. If this stays, click Test on the API Key page.",
                       setup)]
    if live:
        return [_alert("key", ERROR, "Your API key isn't working",
                       "Polymarket US didn't accept it, so the app can't read your balance "
                       "or place orders.",
                       "Check that your identity check is finished, then create a new key "
                       "and save it on the API Key page.", setup)]
    return [_alert("key", WARN, "Your API key isn't working",
                   "Polymarket US didn't accept it. Paper trading still works, but going "
                   "Live won't until this is fixed.",
                   "Create a new key and save it on the API Key page, or delete this one.",
                   setup)]


def _trading_alerts(s: dict) -> list[dict]:
    out = []
    kill = s.get("killReason")
    if kill:
        out.append(_alert("kill-switch", ERROR, "The kill switch is on",
                          f"No new orders can go out, including exits. Reason: {str(kill)[:160]}",
                          "When it's safe to trade again, open Bot positions and click "
                          "Turn kill switch off.",
                          _nav("positions", "Open Bot positions")))
    elif s.get("paused"):
        out.append(_alert("paused", WARN, "Trading is paused",
                          "The engines won't open or manage trades while paused.",
                          "Click Resume when you're ready. Engines that were off stay off, and "
                          "AI agents stay off until you turn them on.",
                          {"kind": "resume", "label": "Resume"}))
    phone = s.get("phonePaused")
    if phone:
        out.append(_alert("phone-paused", WARN, "Engines were paused from your phone",
                          f"Paused: {', '.join(phone)}.",
                          "Send /resume to your bot to turn them back on, or turn them on "
                          "in this app.",
                          _nav("strategies", "Open Strategies")))
    restriction = s.get("restriction")
    if restriction:
        out.append(_alert("exchange-paused", WARN, "Polymarket US has paused orders",
                          str(restriction.get("message") or "Orders are paused for now."),
                          "Nothing to do. Orders resume by themselves when the exchange is back."))
    if s.get("dailyRisk"):
        out.append(_alert("daily-stop", WARN, "Your daily limit stopped new trades",
                          f"{str(s['dailyRisk'])[:1].upper()}{str(s['dailyRisk'])[1:]}.",
                          "New trades resume tomorrow. To change the limit, open Settings.",
                          _nav("settings", "Open Settings")))
    if s.get("accountLock"):
        out.append(_alert("account-lock", ERROR, "Another copy of the app holds this account",
                          str(s["accountLock"])[:200],
                          "Close the other copy of the app. This one can't open trades until then."))
    unknown = s.get("unknownPositions") or []
    if unknown:
        shown = ", ".join(
            f"{float(u.get('qty') or 0):g} {str(u.get('side') or '').upper()} of "
            f"{str(u.get('ticker') or '')[:48]} (cost ${float(u.get('costUsd') or 0):.2f})"
            for u in unknown[:3])
        more = f", and {len(unknown) - 3} more" if len(unknown) > 3 else ""
        one = len(unknown) == 1
        out.append(_alert(
            "unknown-positions", ERROR,
            "Your account holds a position this app didn't place" if one
            else "Your account holds positions this app didn't place",
            f"{shown}{more}. The app can't look {'this market' if one else 'these markets'} "
            "up, so it isn't counted in your results here.",
            "Check your activity in the Polymarket US app. If you didn't place "
            f"{'it' if one else 'them'}, create a new API key and delete the old one.",
            _nav("api", "Open API Key page")))
    if s.get("balanceStale"):
        out.append(_alert("balance-stale", WARN, "Your balance may be out of date",
                          "The app can't read your positions' value from Polymarket US right now.",
                          "It keeps retrying. Don't rely on the balance or daily limits "
                          "until this clears."))
    return out


# Which health rows become alerts, and what they're called there. The row already
# holds the detail and the fix. Left out on purpose: `wallet` (_key_alerts words it
# for the API Key page), and any row with no fix.
_ROW_TITLES = {
    "mcp": "The AI agent server isn't working",
    "http": "The agent HTTP API isn't working",
    "autopilot": "Autopilot needs attention",
    "remote:discord": "Your Discord bot has a problem",
    "remote:telegram": "Your Telegram bot has a problem",
    "feed": "The live price feed is down",
    "spot": "The crypto price feed is down",
}
_ROW_PAGE = {
    "mcp": ("agents", "Open AI Agents"),
    "http": ("agents", "Open AI Agents"),
    "autopilot": ("agents", "Open AI Agents"),
}
# These connect in the background after a start, so a miss right then means nothing.
_SLOW_START_ROWS = ("remote:discord", "remote:telegram", "feed", "spot")


def _plain_detail(rid: str, detail: str) -> str:
    """The health panel shows the raw reason for people debugging. An alert is read
    by anyone, and "[winerror 10048]" tells them nothing."""
    if rid == "mcp" and "could not listen" in detail.lower():
        port = re.search(r"127\.0\.0\.1:(\d+)", detail)
        where = f"port {port.group(1)}" if port else "the port it needs"
        return f"Another program is using {where}, so AI agents can't connect."
    return detail


def _row_alerts(rows: list[dict], starting: bool) -> list[dict]:
    out = []
    for r in rows:
        rid, status = str(r.get("id") or ""), r.get("status")
        if status not in ("fail", "warn") or not r.get("fix"):
            continue
        # "No client has connected yet" is a setup step, not a fault.
        if rid == "mcp" and status == "warn":
            continue
        if starting and rid in _SLOW_START_ROWS:
            continue
        title = _ROW_TITLES.get(rid)
        if title is None and rid.startswith("ai:"):
            title = f"Your AI provider needs attention: {str(r.get('label') or '')}".strip()
        if title is None:
            continue
        action = r.get("action")
        if not action and rid in _ROW_PAGE:
            action = _nav(*_ROW_PAGE[rid])
        out.append(_alert(rid, ERROR if status == "fail" else WARN, title,
                          _plain_detail(rid, str(r.get("detail") or "")), str(r["fix"]), action))
    return out


def build(s: dict) -> list[dict]:
    """`s` is what service._h_alerts gathered; every key is optional. Errors first."""
    uptime = s.get("uptimeSec")
    starting = uptime is not None and float(uptime) < STARTUP_GRACE_SEC
    # Offline mode is the user's own choice, so a silent network isn't a fault.
    outages = {} if s.get("offline") else (s.get("outages") or {})
    alerts = _api_alerts(outages, bool(s.get("hasKey")))

    if s.get("regionRefused"):
        alerts.append(_alert(
            "region", ERROR, "Polymarket US refused your location",
            "It turned down a request because of where this computer is connecting from.",
            "Polymarket US decides where accounts can trade. Check that you're in an "
            "eligible state and that your account is in good standing. "
            "support@polymarket.us can help."))

    # A location refusal comes back as a 403 too, and that isn't the key's fault.
    alerts += _key_alerts(s, bool(outages) or bool(s.get("regionRefused")), starting)
    alerts += _trading_alerts(s)
    alerts += _row_alerts(s.get("healthRows") or [], starting)

    for c in s.get("endedClients") or []:
        name = str(c.get("client") or "Your AI client")
        alerts.append(_alert(
            f"client:{name}", WARN, f"{name} disconnected",
            "It closed its connection to this app, so it can't read markets or trade here.",
            f"If you didn't mean to close it, open {name} again and it reconnects by itself.",
            _nav("agents", "Open AI Agents")))

    for sc in s.get("stoppedScripts") or []:
        name = str(sc.get("name") or "A script")
        alerts.append(_alert(
            f"script:{sc.get('id')}", ERROR, f"Your script stopped: {name}",
            f"The app turned it off because of an error: {str(sc.get('reason') or 'unknown')[:160]}",
            "Fix the error in the script, then enable it again.",
            _nav("strategies", "Open Strategies")))

    for label in s.get("crashingLoops") or []:
        alerts.append(_alert(
            f"loop:{label}", ERROR, f"The {label} engine keeps crashing",
            "It has restarted several times in the last couple of minutes.",
            "Open Logs to see the error. If it continues, restart the app.",
            _nav("logs", "Open Logs")))

    alerts.sort(key=lambda a: 0 if a["severity"] == ERROR else 1)
    return alerts
