# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report them privately via one of:

- GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Security" tab → "Report a vulnerability"), or
- email **hi@krypt.cc**.

Please include reproduction steps and the affected version. We'll acknowledge as soon as we can.

## Handling of credentials

Krypt PolyBot US stores your **Polymarket US API key** (Key ID and Ed25519 Secret Key) **locally** in
the app's user-data directory (`%APPDATA%/Krypt PolyBot US/credentials/` on Windows). It is used only
to sign requests to Polymarket US and is **never transmitted to any server operated by this project**.
Anyone with the Secret Key can trade your account.

> [!IMPORTANT]
> On **Windows**, credentials are encrypted at rest with the OS keystore (**DPAPI**,
> `CryptProtectData`) — tied to your user account, with no key stored on disk; existing plaintext
> files are upgraded automatically on first read. On non-Windows dev builds they fall back to
> plaintext (the app ships on Windows). Either way, protect the machine you run this on.

If you believe your key may have been exposed, revoke it at <https://polymarket.us/developer> and
create a new one.

## User strategy scripts (the Scripts tab)

Krypt PolyBot US runs **user-authored Python** in the same process that holds the decrypted API key,
with the same privileges as the trading engine. There is **no language sandbox**.

> [!WARNING]
> **A strategy script is a program you are running on your machine.** It can do anything your user
> account can do: read and write your files, open network connections, and reach the API key held
> in this process. Treat a script from someone else — or from an AI — exactly as you would treat any
> executable you downloaded from them, because that is what it is. Read it first.

This is a deliberate change from earlier versions, which validated scripts against an AST whitelist
(no imports, no classes, curated builtins only). That whitelist blocked most useful Python while
never being a real security boundary — CPython sandboxes are escapable in principle, and the docs
said so. Rather than offer a guarantee it could not keep, the app now **reviews instead of
restricting**.

**The risk audit.** Every script is statically scanned (`python/script_audit.py`) on save, on
validate, and before it can be armed. It reports, with line numbers:

- **network** — sockets, HTTP clients, websockets, mail, DNS, `webbrowser`
- **credentials** — the app's own credential modules (`polymarket_auth`, `polymarket_api`, `trader`, …),
  the OS keyring, and credential-shaped identifiers/strings (`private_key`, `secret_key`, `.env`, …)
- **exfiltration** — a synthesized critical finding when a script does *both* of the above, which is
  the shape of a key-stealing script
- **dynamic** — `eval`/`exec`/`compile`, `__import__`, `importlib`, `pickle`/`marshal`, base64 and
  hex decoding, `getattr` with a computed name, and long encoded literals
- **process / filesystem** — `subprocess`, `os.system`, `ctypes`, file writes and deletions
- **env / introspection** — `os.environ`, `sys.modules`, frame walking

Obfuscation is reported as **critical in its own right**. A static scan cannot see through it, so
"this script is hiding what it does" is the only honest thing the tool can say about such code.

> [!IMPORTANT]
> The audit is a smoke detector, not a lock. Clean findings mean *nothing known-dangerous was
> recognized* — never *safe*. Nothing in the app blocks a script from running; the audit informs the
> user, and arming a flagged script requires passing a confirmation that names the findings.

**What is still enforced.** The **money rails** are not advisory and are unchanged: max entry price,
max contracts per order, max open positions per script, a per-script daily-loss breaker, and one
entry attempt per market window. A script cannot place an order the rails would refuse, and where a
cap and the exchange's own minimum contradict each other the entry is refused rather than rounded up
past the cap. Liveness is enforced too: every hook runs in a joined worker thread with a hard
wall-clock timeout, and a hook that overruns is abandoned and its script auto-disabled.

**What we consider a reportable vulnerability here:**

- A way to bypass the **money rails**, or to place an order from a script the rails should have
  refused.
- A path that enables a script or arms it for real orders without the user's explicit in-app
  confirmation.
- A script that hangs or crashes the engine loop rather than being auto-disabled.
- A **false-clean audit**: source that performs network, credential or dynamic-execution access via
  a route `script_audit.py` reports as clean. (Obfuscated code that the audit correctly flags as
  obfuscated is working as designed, not a bug.)

**What is not:** anything a script does once the user has run it — scripts are unsandboxed by
design, and that is stated in the app before a script can be enabled or armed. Nor is strategy
performance or losses.

## Scope

This project ships a desktop app (Electron) plus a local Python backend. Reports of concern include:
credential handling, the IPC boundary, request signing, the user-script money rails and risk audit
(see above), and any path that could place unintended orders. The bundled trading strategies are out
of scope — strategy performance is not a security issue (see the [Disclaimer](./DISCLAIMER.md)).
