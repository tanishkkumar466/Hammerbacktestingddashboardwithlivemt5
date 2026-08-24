"""
Self-update engine for Hammer.

Pure Python — no Qt imports. QThread wrappers live in update_workers.py;
the dialog is update_window.py.

check_for_update()       -> blocking; call off the GUI thread
download_and_install()   -> blocking; progress/status callbacks for UI

Release packaging:
  - Source / script install: attach one .zip of the app .py tree
  - Frozen Windows EXE: attach HammerCandleBacktestDashboard.exe (or any .exe)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

from version import __version__ as CURRENT_VERSION


# ---------------------------------------------------------------------------
# Hammer GitHub Releases
# ---------------------------------------------------------------------------
GITHUB_OWNER = "tanishkkumar466"
GITHUB_REPO = "Hammerbacktestingddashboardwithlivemt5"


_TOKEN_FILE_NAMES = (
    ".hammer_github_token",
    ".github_token",
    # Windows Notepad often appends .txt or drops the leading dot:
    "hammer_github_token.txt",
    ".hammer_github_token.txt",
    "hammer_github_token",
)


def _normalize_github_token(raw: str) -> Optional[str]:
    token = (raw or "").strip().strip('"').strip("'")
    if token.startswith("\ufeff"):
        token = token.lstrip("\ufeff").strip()
    if "\x00" in token:
        return None
    return token or None


def _read_token_file(path: str) -> Optional[str]:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
                token = _normalize_github_token(f.read())
            if token:
                return token
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _token_search_roots() -> list[str]:
    """Folders where we look for (or save) the GitHub token file."""
    roots: list[str] = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(sys.executable))
    roots.append(os.path.dirname(os.path.abspath(__file__)))
    if os.getcwd():
        roots.append(os.getcwd())
    seen: set[str] = set()
    ordered: list[str] = []
    for base in roots:
        if not base:
            continue
        norm = os.path.normpath(base)
        if norm in seen:
            continue
        seen.add(norm)
        ordered.append(norm)
    return ordered


def _primary_token_path() -> str:
    return os.path.join(_token_search_roots()[0], ".hammer_github_token")


def token_lookup_hint() -> str:
    """Human-readable hint for Help / error dialogs."""
    roots = _token_search_roots()
    if not roots:
        return "Put a plain-text token file next to HammerCandleBacktestDashboard.exe"
    lines = [
        "Plain text file — one line only, starting with ghp_",
        "",
        "Accepted names (same folder as the .exe):",
        "  .hammer_github_token",
        "  hammer_github_token.txt",
        "",
        "This app looks in:",
    ]
    for base in roots:
        lines.append(f"  {base}")
    lines.append("")
    lines.append(f"Or use Help → GitHub Update Token… to save it for you.")
    return "\n".join(lines)


def save_github_token(token: str) -> str:
    """Write token next to the app. Returns the file path."""
    cleaned = _normalize_github_token(token)
    if not cleaned:
        raise ValueError("Token is empty.")
    if not cleaned.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")):
        raise ValueError(
            "That does not look like a GitHub token. It should start with ghp_ "
            "(classic) or github_pat_ (fine-grained)."
        )
    path = _primary_token_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(cleaned)
    global GITHUB_TOKEN
    GITHUB_TOKEN = cleaned
    return path


def _load_github_token() -> Optional[str]:
    """Token for private repos. Env wins; else file next to app / module (gitignored)."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = _normalize_github_token(os.environ.get(key) or "")
        if val:
            return val
    # Frozen exe: token lives beside the executable, not inside _MEIPASS bundle.
    for base in _token_search_roots():
        for name in _TOKEN_FILE_NAMES:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                token = _read_token_file(path)
                if token:
                    return token
    return None


GITHUB_TOKEN = _load_github_token()

API_LATEST_RELEASE = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)

# Never overwrite local runtime / secrets even if a release zip includes them
_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "data",
    "output",
    "plots",
    "presets",
    "build",
    "dist",
    ".pytest_cache",
    ".cursor",
    "_hammer_update_staging",
    "_hammer_update_extract",
}
_SKIP_FILE_NAMES = {
    "run_history.db",
    ".env",
    ".hammer_github_token",
    ".github_token",
    "hammer_github_token.txt",
    ".hammer_github_token.txt",
    "hammer_github_token",
    "credentials.json",
    "secrets.json",
    ".DS_Store",
    "_hammer_apply_update.bat",
    "_hammer_relaunch.bat",
    "_hammer_update.log",
}


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    notes: str
    download_url: str  # API asset URL for private repos; browser URL as fallback
    asset_name: str
    asset_size: int
    asset_kind: str  # "zip" | "exe"
    asset_id: Optional[int] = None


class UpdateError(Exception):
    pass


def _version_tuple(v: str):
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(remote_version: str, local_version: Optional[str] = None) -> bool:
    if local_version is None:
        local_version = CURRENT_VERSION
    return _version_tuple(remote_version) > _version_tuple(local_version)


def _request_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {
        "Accept": accept,
        "User-Agent": f"HammerDashboard/{CURRENT_VERSION}",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _api_request(url: str, *, not_found_message: Optional[str] = None):
    """GET JSON from GitHub. Returns dict or list. Raises UpdateError on failure."""
    # Reload token each call so a newly created token file works without restart
    global GITHUB_TOKEN
    GITHUB_TOKEN = _load_github_token()

    req = urllib.request.Request(url, headers=_request_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                not_found_message
                or (
                    f"GitHub returned 404 for:\n{url}\n"
                    "If this is a private repo, put a token in .hammer_github_token "
                    "(see Help → About)."
                )
            ) from e
        if e.code in (401, 403):
            raise UpdateError(
                f"GitHub auth error {e.code}. Check your token has 'repo' (private) "
                "or 'public_repo' scope."
            ) from e
        raise UpdateError(f"GitHub API error {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise UpdateError(f"Network error contacting GitHub: {e.reason}") from e


def _pick_release_asset(assets: list) -> Optional[dict]:
    """Prefer Windows delivery assets when frozen; source .zip when running from code."""
    if not assets:
        return None

    def score(asset: dict) -> int:
        name = str(asset.get("name", "")).lower()
        size = int(asset.get("size", 0) or 0)
        pts = 0
        if name.endswith(".exe"):
            pts += 200
            # Full Windows bundle ~420–450 MB (ray[default]); ~408 MB = missing ray extras
            if size and size < 50_000_000:
                pts -= 200
            elif size and size < 300_000_000:
                pts -= 80
            elif size >= 420_000_000:
                pts += 70
            elif size >= 400_000_000:
                pts += 50
            elif size >= 380_000_000:
                pts += 30
            elif size and size < 350_000_000:
                pts -= 80
        if name == "hammercandlebacktestdashboard.exe":
            pts += 60
        if "windows" in name:
            pts += 90
        if "hammercandle" in name:
            pts += 50
        elif "hammer" in name:
            pts += 30
        if name.endswith(".zip"):
            pts += 20
            # Source-only git archive (~300 KB) — never use for frozen Windows
            if size and size < 5_000_000:
                pts -= 150
        if getattr(sys, "frozen", False) and name.endswith(".zip"):
            if "windows" not in name and not name.endswith(".exe"):
                if name.startswith("hammer-") or name.endswith("src.zip"):
                    pts -= 120
        return pts

    ranked = sorted(assets, key=score, reverse=True)
    best = ranked[0]
    if getattr(sys, "frozen", False) and score(best) < 40:
        return None
    return best


def check_for_update() -> Optional[ReleaseInfo]:
    """Return ReleaseInfo if a newer release exists, else None. Blocking."""
    global GITHUB_TOKEN
    GITHUB_TOKEN = _load_github_token()

    repo_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    try:
        _api_request(
            repo_url,
            not_found_message=(
                f"Cannot find repo {GITHUB_OWNER}/{GITHUB_REPO}.\n"
                + (
                    "Your token may lack access, or the owner/repo name is wrong."
                    if GITHUB_TOKEN
                    else (
                        "Repo is likely private. Add a GitHub token:\n"
                        "Help → GitHub Update Token…\n\n"
                        + token_lookup_hint()
                    )
                )
            ),
        )
    except UpdateError:
        raise

    try:
        data = _api_request(
            API_LATEST_RELEASE,
            not_found_message=(
                "Connected to GitHub OK, but this repo has no Releases yet.\n\n"
                "Next: GitHub → Releases → Draft a new release → tag e.g. v1.0.1 → "
                "attach a .zip of the app → Publish release.\n\n"
                "Pushing updater.py is optional for this check — you need a published "
                "Release with a zip asset."
            ),
        )
    except UpdateError:
        raise

    if not isinstance(data, dict):
        raise UpdateError("Unexpected GitHub releases response.")

    tag = data.get("tag_name", "") or ""
    version = tag.lstrip("vV")
    notes = data.get("body", "") or ""

    asset = _pick_release_asset(data.get("assets") or [])
    if not asset:
        if getattr(sys, "frozen", False):
            raise UpdateError(
                "Latest release has no HammerCandleBacktestDashboard.exe yet.\n\n"
                "Wait for the Release Windows EXE GitHub Action to finish for this "
                "version (runs automatically on each v* tag), then try again."
            )
        raise UpdateError(
            "Latest release has no .zip or .exe asset attached. "
            "Attach a source .zip (or Windows .exe for frozen builds)."
        )

    name = asset.get("name", "") or "update.bin"
    kind = "exe" if name.lower().endswith(".exe") else "zip"
    # Private repos: browser_download_url returns 404 even with a token.
    # Always prefer the API asset URL + Accept: application/octet-stream.
    api_url = asset.get("url") or ""
    browser_url = asset.get("browser_download_url") or ""
    download_url = api_url or browser_url
    if not download_url:
        raise UpdateError("Release asset has no download URL.")

    info = ReleaseInfo(
        tag=tag,
        version=version,
        notes=notes,
        download_url=download_url,
        asset_name=name,
        asset_size=int(asset.get("size", 0) or 0),
        asset_kind=kind,
        asset_id=asset.get("id"),
    )

    if is_newer(info.version, CURRENT_VERSION):
        return info
    return None


def _download_file(
    url: str,
    dest_path: str,
    progress_cb: Callable[[int, int], None],
) -> None:
    global GITHUB_TOKEN
    GITHUB_TOKEN = _load_github_token()

    req = urllib.request.Request(
        url,
        headers=_request_headers("application/octet-stream"),
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as out_file:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            chunk_size = 65536
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                progress_cb(downloaded, total)
    except urllib.error.HTTPError as e:
        raise UpdateError(
            f"Download failed (HTTP {e.code}). "
            "For private repos the app must use the API asset URL with your token "
            f"— retry after restart. Detail: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise UpdateError(f"Download network error: {e.reason}") from e


def app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _find_source_root(extracted_dir: str) -> str:
    """Walk through single-child wrapping folders (GitHub zip layout)."""
    current = extracted_dir
    while True:
        entries = [e for e in os.listdir(current) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(current, entries[0])):
            current = os.path.join(current, entries[0])
        else:
            return current


def _should_skip_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _SKIP_DIR_NAMES:
            return True
    if parts and parts[-1] in _SKIP_FILE_NAMES:
        return True
    if parts and parts[-1].endswith(".db"):
        return True
    return False


def _copy_over_app(source_root: str, dest_root: str) -> None:
    for root, dirs, files in os.walk(source_root):
        # Prune skipped directories in-place
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        rel_dir = os.path.relpath(root, source_root)
        if rel_dir != "." and _should_skip_path(rel_dir):
            continue
        target_dir = dest_root if rel_dir == "." else os.path.join(dest_root, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for fname in files:
            rel = fname if rel_dir == "." else os.path.join(rel_dir, fname)
            if _should_skip_path(rel):
                continue
            shutil.copy2(os.path.join(root, fname), os.path.join(target_dir, fname))


def _find_hammer_exe_in_tree(root: str) -> Optional[str]:
    """Locate HammerCandleBacktestDashboard.exe (or similar) under an extracted update."""
    preferred: Optional[str] = None
    fallback: Optional[str] = None
    for dirpath, _, files in os.walk(root):
        for fname in files:
            if not fname.lower().endswith(".exe"):
                continue
            path = os.path.join(dirpath, fname)
            low = fname.lower()
            if low == "hammercandlebacktestdashboard.exe":
                return path
            if "hammer" in low:
                fallback = fallback or path
    return preferred or fallback


def _tree_has_source_only_layout(root: str) -> bool:
    """True when the zip is a Python source tree (cannot update a frozen one-file exe)."""
    if _find_hammer_exe_in_tree(root):
        return False
    return os.path.isfile(os.path.join(root, "main.py")) and os.path.isfile(
        os.path.join(root, "dashboard.py")
    )


def _is_onedir_payload(payload_root: str) -> bool:
    return os.path.isdir(os.path.join(payload_root, "_internal"))


# Windows: apply update after this process exits (exe swap or onedir folder copy)
_WINDOWS_UPDATE_BAT: list = [None]
_UPDATE_LOG_NAME = "_hammer_update.log"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_PYI_RELAUNCH_ENV = "PYINSTALLER_RESET_ENVIRONMENT"

# Private bootloader vars — if left set, a new onefile exe thinks it is a child
# of the previous instance / cmd.exe and dies with security validation failure.
_PYI_PRIVATE_ENV_VARS = (
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_ARCHIVE_FILE",
    "_PYI_LINUX_PROCESS_NAME",
    "_PYI_SPLASH_IPC",
)


def _strip_pyi_env(env: dict) -> dict:
    """Env safe for launching a fresh one-file Hammer instance (not for cmd helpers)."""
    cleaned = {k: v for k, v in env.items() if not str(k).startswith("_PYI_")}
    cleaned[_PYI_RELAUNCH_ENV] = "1"
    return cleaned


def _clean_windows_helper_env() -> dict:
    """
    Minimal Windows env for the update CMD window.

    Do NOT inherit the frozen app's full environment — leftover _PYI_* / Qt /
    Python vars are why post-update relaunch dies while double-click works.
    """
    keys = (
        "SystemRoot",
        "windir",
        "WINDIR",
        "SystemDrive",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "ComSpec",
        "PATHEXT",
        "PUBLIC",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
    )
    env: dict = {}
    for key in keys:
        val = os.environ.get(key)
        if val:
            env[key] = val
    sysroot = env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows"
    env["PATH"] = os.pathsep.join(
        [
            os.path.join(sysroot, "System32"),
            os.path.join(sysroot, "System32", "Wbem"),
            os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0"),
            sysroot,
        ]
    )
    return env


def update_log_path() -> str:
    return os.path.join(app_root(), _UPDATE_LOG_NAME)


def read_pending_update_message() -> Optional[str]:
    """
    If the last self-update bat failed or succeeded, return text for a startup dialog.
    Consumes (deletes) the log file after reading.
    """
    path = update_log_path()
    if not os.path.isfile(path):
        return None
    try:
        text = open(path, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not text:
        return None
    if "UPDATE_FAILED" in text:
        return (
            "The last Check for Updates could not replace the .exe.\n\n"
            "Details (_hammer_update.log):\n"
            f"{text}\n\n"
            "Fix: close all Hammer windows, download the latest "
            "HammerCandleBacktestDashboard.exe from GitHub Releases "
            "(~420–450 MB), replace the file manually, then double-click it."
        )
    if "UPDATE_OK" in text:
        return None
    return None


def _bat_echo_log(log_path: str, message: str) -> str:
    safe = message.replace('"', "'")
    return f'echo {safe}>>"{log_path}"'


def _windows_clear_pyi_env_lines() -> list[str]:
    """Strip PyInstaller private env vars before relaunching a one-file exe."""
    lines = [
        'for /f "tokens=1 delims==" %%V in (\'set _PYI_ 2^>nul\') do set "%%V="',
    ]
    for _var in _PYI_PRIVATE_ENV_VARS:
        lines.append(f'set "{_var}="')
    # Also clear MEIPASS leftovers from older bootloaders
    lines.append('set "_MEIPASS2="')
    lines.append(f'set "{_PYI_RELAUNCH_ENV}=1"')
    return lines


def _windows_relaunch_lines(root: str, exe_path: str, log_path: str) -> list[str]:
    """
    Relaunch after update the same way Explorer double-click does.

    Why local build works but Check-for-Updates does not
    ----------------------------------------------------
    Local / double-click: Explorer starts the exe with a clean environment.
    Check for Updates: we used to spawn a HIDDEN cmd (CREATE_NO_WINDOW) that
    still carried frozen-app env. Then `start` from that hidden process often
    never shows the GUI — download finishes, app never opens.

    Fix: wipe _PYI_*, then ShellExecute via rundll32 (same path as double-click),
    with `start` as fallback. Update runs in a VISIBLE console so progress/errors
    are visible.
    """
    lines = list(_windows_clear_pyi_env_lines())
    lines.append("echo.")
    lines.append("echo Starting Hammer (same as double-click)...")
    lines.append(_bat_echo_log(log_path, "Relaunching Hammer via ShellExecute..."))
    # FileProtocolHandler == Explorer "open" — not a child of the frozen process
    lines.append(f'rundll32 url.dll,FileProtocolHandler "{exe_path}"')
    lines.append("if errorlevel 1 (")
    lines.append(_bat_echo_log(log_path, "ShellExecute failed — trying start"))
    lines.append(f'  start "" /D "{root}" "{exe_path}"')
    lines.append("  if errorlevel 1 (")
    lines.append(_bat_echo_log(log_path, "UPDATE_FAILED relaunch"))
    lines.append("    echo UPDATE FAILED — double-click the .exe manually")
    lines.append("    pause")
    lines.append("    exit /b 1")
    lines.append("  )")
    lines.append(")")
    lines.append(_bat_echo_log(log_path, "UPDATE_OK relaunch started"))
    lines.append("echo Done. This window will close in 5 seconds.")
    lines.append("timeout /t 5 /nobreak >nul")
    return lines


def _windows_wait_for_exit_lines(*, pid: int, exe_path: str, log_path: str) -> list[str]:
    """Wait until Hammer fully exits (PID + process image name)."""
    exe_name = os.path.basename(exe_path)
    label_pid = f"wait_pid_{pid}"
    label_img = "wait_img"
    return [
        _bat_echo_log(log_path, f"Waiting for Hammer PID {pid} to exit..."),
        f":{label_pid}",
        f'tasklist /FI "PID eq {pid}" 2>nul | find /I "{pid}" >nul',
        "if %ERRORLEVEL%==0 (",
        "  timeout /t 1 /nobreak >nul",
        f"  goto {label_pid}",
        ")",
        _bat_echo_log(log_path, f"Waiting for {exe_name} processes to finish..."),
        f":{label_img}",
        f'tasklist /FI "IMAGENAME eq {exe_name}" 2>nul | find /I "{exe_name}" >nul',
        "if %ERRORLEVEL%==0 (",
        "  timeout /t 1 /nobreak >nul",
        f"  goto {label_img}",
        ")",
        # one-file parent bootloader may still hold the exe briefly
        "timeout /t 3 /nobreak >nul",
    ]


def _windows_replace_exe_lines(*, staged: str, current_exe: str, log_path: str) -> list[str]:
    """Replace running one-file exe with retries; log failures to update log."""
    return [
        _bat_echo_log(log_path, "Replacing executable..."),
        f'del /F /Q "{current_exe}.old" 2>nul',
        f'move /Y "{current_exe}" "{current_exe}.old"',
        "if errorlevel 1 (",
        _bat_echo_log(log_path, "UPDATE_FAILED could not move old exe (still locked?)"),
        "  exit /b 1",
        ")",
        "set COPY_TRIES=0",
        ":copy_retry",
        "set /a COPY_TRIES+=1",
        f'copy /Y "{staged}" "{current_exe}"',
        "if errorlevel 1 (",
        "  if !COPY_TRIES! lss 30 (",
        _bat_echo_log(log_path, "copy retry (exe still locked)"),
        "    timeout /t 2 /nobreak >nul",
        "    goto copy_retry",
        "  )",
        _bat_echo_log(log_path, "UPDATE_FAILED copy failed after 30 tries"),
        "  exit /b 1",
        ")",
        f'del /F /Q "{staged}" 2>nul',
        f'del /F /Q "{current_exe}.old" 2>nul',
        _bat_echo_log(log_path, "UPDATE_OK exe replaced"),
    ]


def _spawn_frozen_relaunch(exe_path: str, root: str) -> None:
    """Launch a fresh one-file exe instance (post-update or relaunch)."""
    env = _strip_pyi_env(os.environ.copy())
    kwargs: dict = {"cwd": root, "env": env, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW | _DETACHED_PROCESS
    subprocess.Popen([exe_path], **kwargs)


def _write_windows_update_bat(*, pid: int, exe_path: str, lines: list[str]) -> str:
    """Batch script that waits for Hammer to exit, then runs update commands."""
    bat = os.path.join(app_root(), "_hammer_apply_update.bat")
    log_path = update_log_path()
    try:
        if os.path.isfile(log_path):
            os.remove(log_path)
    except OSError:
        pass
    script = [
        "@echo off",
        "setlocal EnableExtensions EnableDelayedExpansion",
        f'echo Hammer update started >"{log_path}"',
    ]
    script.extend(_windows_wait_for_exit_lines(pid=pid, exe_path=exe_path, log_path=log_path))
    script.extend(lines)
    script.append('del /F /Q "%~f0" 2>nul')
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(script) + "\r\n")
    return bat


def _spawn_detached(cmd: list[str]) -> None:
    """
    Spawn the update CMD in a NEW VISIBLE console with a clean Windows env.

    CREATE_NO_WINDOW was the main bug: hidden cmd + inherited _PYI_* meant
    `start` never opened the GUI after download (local double-click still worked).
    """
    if os.name == "nt":
        env = _clean_windows_helper_env()
        kwargs: dict = {
            "env": env,
            "cwd": app_root(),
            "close_fds": True,
            "creationflags": _CREATE_NEW_CONSOLE,
        }
    else:
        kwargs = {"env": _strip_pyi_env(os.environ.copy()), "close_fds": True}
    subprocess.Popen(cmd, **kwargs)


def _schedule_windows_onedir_update(payload_root: str, status_cb: Callable[[str], None]) -> str:
    """Stage a PyInstaller onedir folder; copy over app after exit."""
    root = app_root()
    staging = os.path.join(root, "_hammer_update_staging")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    status_cb("Staging update folder...")
    shutil.copytree(payload_root, staging)

    exe_name = os.path.basename(sys.executable)
    target_exe = os.path.join(root, exe_name)
    log_path = update_log_path()
    bat = _write_windows_update_bat(
        pid=os.getpid(),
        exe_path=target_exe,
        lines=[
            _bat_echo_log(log_path, "Installing onedir update folder..."),
            f'xcopy /E /Y /I /Q "{staging}\\*" "{root}\\"',
            "if errorlevel 1 (",
            _bat_echo_log(log_path, "UPDATE_FAILED xcopy onedir staging"),
            "  exit /b 1",
            ")",
            *_windows_relaunch_lines(root, target_exe, log_path),
            f'rmdir /S /Q "{staging}" 2>nul',
        ],
    )
    _WINDOWS_UPDATE_BAT[0] = bat
    status_cb("Update staged — will apply after the app closes.")
    return root


def _install_exe(downloaded_exe: str, status_cb: Callable[[str], None]) -> str:
    """Replace the running frozen EXE (Windows one-file or onedir launcher)."""
    root = app_root()
    if not getattr(sys, "frozen", False):
        dest = os.path.join(root, os.path.basename(downloaded_exe))
        status_cb("Saving new executable...")
        shutil.copy2(downloaded_exe, dest)
        return root

    current_exe = os.path.abspath(sys.executable)
    dest_name = os.path.basename(current_exe)
    staged = os.path.join(root, dest_name + ".new")
    status_cb("Staging new executable...")
    shutil.copy2(downloaded_exe, staged)
    staged_size = os.path.getsize(staged)
    if staged_size < 350_000_000:
        raise UpdateError(
            f"Downloaded exe is only {staged_size / (1024 * 1024):.1f} MB — expected ~420–450 MB.\n\n"
            "The release may be incomplete or the wrong file was attached. "
            "Use HammerCandleBacktestDashboard.exe from GitHub Actions, not a source zip."
        )

    if os.name == "nt":
        log_path = update_log_path()
        bat = _write_windows_update_bat(
            pid=os.getpid(),
            exe_path=current_exe,
            lines=[
                *_windows_replace_exe_lines(
                    staged=staged, current_exe=current_exe, log_path=log_path
                ),
                *_windows_relaunch_lines(root, current_exe, log_path),
            ],
        )
        _WINDOWS_UPDATE_BAT[0] = bat
        status_cb("Executable staged — will replace after the app closes.")
    else:
        status_cb("Replacing executable...")
        shutil.move(staged, current_exe)
        try:
            os.chmod(current_exe, 0o755)
        except OSError:
            pass
    return root


# Back-compat alias used by relaunch path
_EXE_REPLACE_BAT = _WINDOWS_UPDATE_BAT


def relaunch_and_exit(root: Optional[str] = None) -> None:
    """Spawn a fresh Hammer process, then hard-exit this one."""
    root = root or app_root()
    python_exe = sys.executable
    main_script = os.path.join(root, "main.py")

    update_bat = _WINDOWS_UPDATE_BAT[0]
    if update_bat and os.path.isfile(update_bat):
        _spawn_detached(["cmd", "/c", update_bat])
        os._exit(0)

    if os.name == "nt":
        if getattr(sys, "frozen", False):
            _spawn_frozen_relaunch(python_exe, root)
        else:
            relauncher = os.path.join(root, "_hammer_relaunch.bat")
            with open(relauncher, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("@echo off\r\n")
                f.write("timeout /t 1 /nobreak >nul\r\n")
                f.write(f'start "" /D "{root}" "{python_exe}" "{main_script}"\r\n')
                f.write('del /F /Q "%~f0" 2>nul\r\n')
            _spawn_detached(["cmd", "/c", relauncher])
    else:
        if getattr(sys, "frozen", False):
            env = os.environ.copy()
            for key in list(env):
                if str(key).startswith("_PYI_"):
                    env.pop(key, None)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            subprocess.Popen([python_exe], cwd=root, env=env)
        else:
            subprocess.Popen([python_exe, main_script], cwd=root)

    os._exit(0)


def _install_from_zip(
    asset_path: str,
    status_cb: Callable[[str], None],
) -> str:
    """Install a release .zip — source tree (dev) or Windows onedir/exe payload (frozen)."""
    status_cb("Extracting update...")
    extract_parent = os.path.join(app_root(), "_hammer_update_extract")
    if os.path.isdir(extract_parent):
        shutil.rmtree(extract_parent, ignore_errors=True)
    os.makedirs(extract_parent, exist_ok=True)
    try:
        with zipfile.ZipFile(asset_path, "r") as zf:
            zf.extractall(extract_parent)
        source_root = _find_source_root(extract_parent)

        if getattr(sys, "frozen", False):
            if _tree_has_source_only_layout(source_root):
                raise UpdateError(
                    "This release zip is source code only and cannot update the "
                    "Windows .exe.\n\n"
                    "The maintainer must attach HammerCandleBacktestDashboard.exe or "
                    "HammerCandleBacktestDashboard-windows.zip (from GitHub Actions) "
                    "to the GitHub Release."
                )
            exe_in_tree = _find_hammer_exe_in_tree(source_root)
            if exe_in_tree:
                payload_root = os.path.dirname(exe_in_tree)
                if _is_onedir_payload(payload_root):
                    if os.name == "nt":
                        return _schedule_windows_onedir_update(payload_root, status_cb)
                    status_cb("Installing update...")
                    _copy_over_app(payload_root, app_root())
                    status_cb("Update installed.")
                    return app_root()
                return _install_exe(exe_in_tree, status_cb)
            raise UpdateError(
                "Could not find HammerCandleBacktestDashboard.exe inside the release zip."
            )

        status_cb("Installing update...")
        root = app_root()
        _copy_over_app(source_root, root)
        status_cb("Update installed.")
        return root
    finally:
        shutil.rmtree(extract_parent, ignore_errors=True)


def download_and_install(
    release: ReleaseInfo,
    progress_cb: Callable[[int, int], None],
    status_cb: Callable[[str], None],
) -> str:
    """
    Blocking: download → install into app folder.
    Returns app_root for the caller to relaunch.
    """
    status_cb(f"Downloading {release.asset_name}...")
    tmp_dir = tempfile.mkdtemp(prefix="hammer_update_")
    try:
        asset_path = os.path.join(tmp_dir, release.asset_name)
        _download_file(release.download_url, asset_path, progress_cb)

        if release.asset_kind == "exe" or release.asset_name.lower().endswith(".exe"):
            return _install_exe(asset_path, status_cb)

        return _install_from_zip(asset_path, status_cb)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
