# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks import collect_all

datas = []
hiddenimports = ['alerts', 'history_download', 'db', 'scanner', 'trader', 'polymarket_api', 'polymarket_auth', 'us_api', 'index_feed', 'categorize', 'config', 'crypto15m', 'crypto15m_trader', 'crypto15m_record', 'backtest', 'indicators', 'clob_ws', 'rules', 'spot_ws', 'replay', 'parlay_generator', 'ws_ssl', 'script_sandbox', 'script_engine', 'script_backtest', 'script_docs', 'script_audit', 'statistics', 'terminal', 'terminal_trade', 'terminal_paper', 'kalshi', 'cross_venue', 'remote', 'bot_secrets', 'bot_telegram', 'bot_discord', 'ai_analysis', 'ai_secrets', 'ai_providers', 'autopilot', 'mcp_server', 'mcp_tools', 'mcp_stdio', 'mcp_agents', 'mcp_rails', 'forecast_ledger', 'logscrub', 'health', 'user_ws', 'script_backtest_child']
hiddenimports += collect_submodules('cryptography')
hiddenimports += collect_submodules('httpx')
# Real-time CLOB market-data WebSocket feed (clob_ws.py).
hiddenimports += collect_submodules('websockets')
# certifi CA bundle: ws_ssl.py verifies every wss:// handshake against it
# (frozen macOS Python has no usable OpenSSL default verify paths). httpx
# already pulls certifi in, but collect it explicitly so the .pem can never
# silently drop out of the bundle.
hiddenimports += ['certifi']
datas += collect_data_files('certifi')

binaries = []

# AI market analysis SDKs. Lazy-imported inside ai_analysis (the feature ships
# off and keyless, so the cost of importing them should not be paid at boot) —
# which means PyInstaller's static analysis sees nothing at all and would ship
# a backend that reports "the anthropic package is not installed" on a machine
# where it is. Collect each in full.
#
# httpx2 is listed explicitly because anthropic 1.x is built on it rather than
# on httpx: the bundle carries BOTH stacks, ours and the SDK's. openai still
# uses httpx, which is already collected above.
#
# All optional at runtime — a collect miss degrades to the panel's honest
# "package not installed" reason and can never break the build or the signer.
for _pkg in ('anthropic', 'openai', 'httpx2', 'jiter', 'distro'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

# OS keychain for at-rest credential encryption on macOS/Linux (polymarket_auth
# stores a Fernet master key there). keyring resolves its backends via package
# metadata + entry points, so collect it in full and copy its metadata; the
# Linux Secret Service backend also needs secretstorage + jeepney (absent on the
# Windows build box — the except just skips them there). All optional at runtime:
# a missing/unusable backend falls back to plaintext, so a collect miss here can
# never break the build or the signer.
for _pkg in ('keyring', 'secretstorage', 'jeepney'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass
try:
    datas += copy_metadata('keyring')
except Exception:
    pass


a = Analysis(
    ['service.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow (~13MB) is pulled in transitively but nothing on the runtime path
    # imports it. Drops it from the installer; the `--selftest` build gate aborts
    # the build if this exclude is ever wrong.
    excludes=['PIL'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='krypt-polybot-us-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: UPX-packed unsigned exes are a common AV/EDR false-positive
    # heuristic and get quarantined on user machines. Ship unpacked.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # see EXE(): avoid AV false-positive/quarantine of the unsigned exe
    upx_exclude=[],
    name='krypt-polybot-us-backend',
)
