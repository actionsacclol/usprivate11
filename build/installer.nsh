; Krypt PolyBot US NSIS installer add-ons.
; Loaded by electron-builder (see package.json -> build.nsis.include).
; This file is included into the script HEADER, before electron-builder's own
; installer.nsi / allowOnlyOneInstallerInstance.nsh, for both the installer
; and the uninstaller build (BUILD_UNINSTALLER). Written against
; app-builder-lib 24.13.3 templates (templates/nsis/).
;
; No Windows Defender exclusion here (removed in 3.5, audit H11). The old
; customInstall ran `Add-MpPreference -ExclusionPath $InstDir` unquoted, so a
; path with a space ("...\Programs\Krypt PolyBot US") never worked, and "fixing"
; it would turn off antivirus scanning of a user-writable folder for good.
; Code-signing the app and the backend is the right fix for false positives.

; ---------------------------------------------------------------------------
; Stop this user's backend relays before files are replaced or removed.
;
; Why: MCP clients (Claude Desktop, Cursor, Codex, ...) start
; `resources\python\krypt-polybot-us-backend.exe --mcp --agent <id>` straight
; from the install folder as a stdio relay. They keep that exe and its
; _internal\ files (zipimport keeps base_library.zip open) loaded even when
; Krypt PolyBot US itself is closed. electron-builder's own running-app check
; only looks for "Krypt PolyBot US.exe", so the old uninstaller that an update
; runs fails on the locked files, says "Krypt PolyBot US cannot be closed" for
; an app that is closed, and Retry can leave a mixed old/new install.
;
; Scope: only processes with this exact image name owned by the user running
; the setup (taskkill /FI "USERNAME eq <user>"). If %USERNAME% can't be read
; we skip the kill, so an empty filter can never widen it to other users.
; taskkill's exit code is ignored: "no tasks running" is the common case.
; No /T: with a USERNAME filter it walks every process's tree and took ~16 s
; per call even with nothing to kill (measured 2026-10-12). Not needed either:
; the backend's own children (backtest workers) are the same image name, so
; /IM already matches them.
; Two passes because a client may restart a relay the moment it dies; the
; sleeps let Windows release the file handles. The clients then need a
; restart to pick up the new version (in-app "New in this version" copy).
; ---------------------------------------------------------------------------
!define /ifndef KRYPT_BACKEND_EXE "krypt-polybot-us-backend.exe"

!macro kryptStopBackendRelays
  Push $0
  Push $1
  ReadEnvStr $1 USERNAME
  ${If} $1 != ""
    DetailPrint `Closing Krypt PolyBot US connections used by AI clients...`
    nsExec::Exec `"$SYSDIR\taskkill.exe" /F /IM "${KRYPT_BACKEND_EXE}" /FI "USERNAME eq $1"`
    Pop $0
    Sleep 1500
    nsExec::Exec `"$SYSDIR\taskkill.exe" /F /IM "${KRYPT_BACKEND_EXE}" /FI "USERNAME eq $1"`
    Pop $0
    Sleep 500
  ${EndIf}
  Pop $1
  Pop $0
!macroend

; Defining customCheckAppRunning replaces electron-builder's default check, and
; allowOnlyOneInstallerInstance.nsh then skips the include + `Var pid` the
; default check needs (it tests `!ifmacrondef customCheckAppRunning`, and this
; file is processed first). Provide both here so the default check still runs.
!include "getProcessInfo.nsh"
Var pid

; Runs in the install section before the old version's uninstaller is invoked
; (installSection.nsh), and in our own uninstaller (un.onInit when silent,
; un.install otherwise). Default check first: if the user cancels the
; "Krypt PolyBot US is running" prompt, setup quits and the relays are untouched.
!macro customCheckAppRunning
  !ifmacrondef _CHECK_APP_RUNNING
    !error "electron-builder no longer defines _CHECK_APP_RUNNING; re-check build/installer.nsh against templates/nsis/include/allowOnlyOneInstallerInstance.nsh"
  !endif
  !insertmacro _CHECK_APP_RUNNING
  !insertmacro kryptStopBackendRelays
!macroend

; electron-builder skips CHECK_APP_RUNNING in the elevated inner instance of an
; "all users" install (installSection.nsh: `${ifNot} ${UAC_IsInnerInstance}`),
; which is the instance that runs the old uninstaller. Stop the relays there
; too. When the same account elevated, %USERNAME% is still that user.
!macro customInit
  ${If} ${UAC_IsInnerInstance}
    !insertmacro kryptStopBackendRelays
  ${EndIf}
!macroend

!macro customUnInstall
  ; Don't delete user data on uninstall — they keep their settings,
  ; profiles, credentials, and DB if they reinstall later. The user
  ; can clear via "Delete saved keys" in the API page first.
!macroend
