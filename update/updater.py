"""
Self-update engine for Hammer.

Pure Python — no Qt imports. QThread wrappers live in update.workers;
the dialog is update.window.

check_for_update()       -> blocking; call off the GUI thread
download_and_install()   -> blocking; progress/status callbacks for UI

Release packaging (stub launcher):
  - Prefer Hammer-stub-package.zip = stub exe + app/HammerRuntime.exe
    (not Hammer-windows.zip — 1.0.27/1.0.28 exact-match that name and then
    crash in their broken zip installer; new name lets them use the .exe bridge)
  - Legacy: large HammerCandleBacktestDashboard.exe / HammerRuntime.exe
    still accepted (stages into app/ or one-file swap for old installs)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

from version import __version__ as CURRENT_VERSION
from update.paths import (
    MIN_RUNTIME_BYTES,
    RUNTIME_EXE_NAME,
    STUB_EXE_NAME,
    app_dir as hammer_app_dir,
    install_root as hammer_install_root,
    is_running_as_legacy_onefile,
    is_running_as_runtime,
    is_stub_layout,
    pending_runtime_bin_path,
    pending_runtime_path,
    runtime_exe_path,
    stub_exe_path,
)


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
    try:
        roots.append(hammer_install_root())
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(sys.executable))
    # Repo / install root (update/ is one level down when running from source)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    "_hammer_update_cache",
}

# Neutral extension while bytes are on disk — AV is far more aggressive on *.exe
# written under %TEMP% than on a non-executable package next to the install.
_DOWNLOAD_SUFFIX = ".hammerdl"
_DOWNLOAD_ATTEMPTS = 3
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


# Zip names preferred by fixed clients (1.0.30+).
# Do NOT make Hammer-windows.zip the only package on "latest": 1.0.27/1.0.28
# exact-match it then crash in their broken zip installer. New name lets those
# builds fall through to the .exe bridge path (_install_exe), which works.
STUB_PACKAGE_ZIP_NAMES = (
    "hammer-stub-package.zip",
    "hammer-windows.zip",  # legacy; avoid uploading while broken clients exist
    "hammercandlebacktestdashboard-windows.zip",
)


def _pick_release_asset(assets: list) -> Optional[dict]:
    """Prefer stub zip when frozen; source .zip when running from code."""
    if not assets:
        return None

    frozen = bool(getattr(sys, "frozen", False))

    if frozen:
        # 1) Official stub+runtime package (new name first)
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            size = int(asset.get("size", 0) or 0)
            if name in STUB_PACKAGE_ZIP_NAMES and size >= MIN_RUNTIME_BYTES:
                return asset
        # 2) Legacy exact one-file name (bridge for older clients / incomplete zips)
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            size = int(asset.get("size", 0) or 0)
            if name == "hammercandlebacktestdashboard.exe" and size >= MIN_RUNTIME_BYTES:
                return asset
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            size = int(asset.get("size", 0) or 0)
            if name == "hammerruntime.exe" and size >= MIN_RUNTIME_BYTES:
                return asset

    def score(asset: dict) -> int:
        name = str(asset.get("name", "")).lower()
        size = int(asset.get("size", 0) or 0)
        pts = 0
        if name.endswith(".exe"):
            pts += 200
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
            elif size and size < MIN_RUNTIME_BYTES:
                pts -= 80
            if not frozen:
                pts -= 250
        if name == "hammercandlebacktestdashboard.exe":
            pts += 60
        if name == "hammerruntime.exe":
            pts += 55
        if "windows" in name:
            pts += 90
        if "hammercandle" in name:
            pts += 50
        elif "hammer" in name:
            pts += 30
        if name.endswith(".zip"):
            pts += 20
            if size and size < 5_000_000:
                pts -= 150
            if frozen and (
                "stub-package" in name or "windows" in name
            ) and size >= MIN_RUNTIME_BYTES:
                pts += 250
            if not frozen and "hammer" in name:
                pts += 200
        if frozen and name.endswith(".zip"):
            if "stub-package" not in name and "windows" not in name:
                if name.startswith("hammer-") or name.endswith("src.zip"):
                    pts -= 120
        return pts

    ranked = sorted(assets, key=score, reverse=True)
    best = ranked[0]
    if frozen and score(best) < 40:
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
                "Latest release has no Hammer-stub-package.zip / HammerRuntime.exe yet.\n\n"
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


def _update_cache_dir() -> str:
    path = os.path.join(app_root(), "_hammer_update_cache")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_download_name(asset_name: str) -> str:
    """Keep the real name readable, but never end in .exe while downloading."""
    base = os.path.basename(asset_name or "update").strip() or "update"
    # Strip trailing .exe so Windows Defender does not treat the download as a new PE drop.
    low = base.lower()
    if low.endswith(".exe"):
        base = base[: -len(".exe")] + ".bin"
    if not base.lower().endswith(_DOWNLOAD_SUFFIX):
        base = base + _DOWNLOAD_SUFFIX
    return base


def _looks_like_av_interference(exc: BaseException) -> bool:
    """Heuristic: security software often deletes / locks the file mid-write."""
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return True
    msg = str(exc).lower()
    needles = (
        "permission denied",
        "access is denied",
        "being used by another process",
        "cannot find the file",
        "no such file",
        "winerror 5",
        "winerror 32",
        "winerror 33",
        "quarantine",
        "virus",
        "threat",
        "incomplete or corrupted",
        "disappeared",
        "removed while",
    )
    return any(n in msg for n in needles)


def cleanup_broken_update_files() -> None:
    """Remove truncated pending / leftover cache so the next Try Again is clean."""
    root = app_root()
    candidates = [
        pending_runtime_path(root),
        pending_runtime_bin_path(root),
        pending_runtime_path(root) + ".writing",
        pending_runtime_bin_path(root) + ".writing",
    ]
    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if size < MIN_RUNTIME_BYTES:
                os.remove(path)
        except OSError:
            pass
    cache = os.path.join(root, "_hammer_update_cache")
    if os.path.isdir(cache):
        try:
            for name in os.listdir(cache):
                low = name.lower()
                if low.endswith((".part", _DOWNLOAD_SUFFIX, ".tmp")):
                    try:
                        os.remove(os.path.join(cache, name))
                    except OSError:
                        pass
        except OSError:
            pass


def _copy_file_with_progress(
    src: str,
    dst: str,
    status_cb: Callable[[str], None],
    *,
    label: str = "Preparing update",
) -> None:
    total = os.path.getsize(src)
    copied = 0
    chunk = 1024 * 1024
    last_pct = -1
    with open(src, "rb") as inf, open(dst, "wb") as out:
        while True:
            buf = inf.read(chunk)
            if not buf:
                break
            out.write(buf)
            copied += len(buf)
            if total > 0:
                pct = int(copied * 100 / total)
                if pct != last_pct and (pct % 5 == 0 or pct >= 99):
                    last_pct = pct
                    status_cb(
                        f"{label}… {copied / (1024 * 1024):.0f} / "
                        f"{total / (1024 * 1024):.0f} MB"
                    )
    out_size = os.path.getsize(dst)
    if out_size != total:
        raise UpdateError(
            f"Copy incomplete ({out_size} of {total} bytes). Tap Try Again."
        )


def _download_file(
    url: str,
    dest_path: str,
    progress_cb: Callable[[int, int], None],
    *,
    expected_size: Optional[int] = None,
) -> None:
    """
    Stream a release asset to dest_path.

    Writes via a sibling .part file, then renames. If security software deletes
    or locks the file mid-stream, raises UpdateError with an AV-friendly message
    so the caller can auto-retry.
    """
    global GITHUB_TOKEN
    GITHUB_TOKEN = _load_github_token()

    part_path = dest_path + ".part"
    for stale in (dest_path, part_path):
        try:
            if os.path.isfile(stale):
                os.remove(stale)
        except OSError:
            pass

    req = urllib.request.Request(
        url,
        headers=_request_headers("application/octet-stream"),
    )
    downloaded = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            if expected_size and total and abs(total - expected_size) > 1024:
                raise UpdateError(
                    f"Download Content-Length ({total} bytes) does not match "
                    f"GitHub asset size ({expected_size} bytes). Aborting."
                )
            chunk_size = 65536
            with open(part_path, "wb") as out_file:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    try:
                        out_file.write(chunk)
                        out_file.flush()
                    except OSError as e:
                        raise UpdateError(
                            "Download interrupted while writing the package "
                            f"(file removed or locked mid-transfer).\n\n{e}"
                        ) from e
                    downloaded += len(chunk)
                    progress_cb(downloaded, total or expected_size or 0)
                    # Detect AV deleting the open file on some Windows configs
                    try:
                        if not os.path.isfile(part_path):
                            raise UpdateError(
                                "Download file disappeared while writing "
                                "(security software likely quarantined it)."
                            )
                    except UpdateError:
                        raise
                    except OSError:
                        pass
    except urllib.error.HTTPError as e:
        raise UpdateError(
            f"Download failed (HTTP {e.code}). "
            "For private repos the app must use the API asset URL with your token "
            f"— retry after restart. Detail: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise UpdateError(f"Download network error: {e.reason}") from e
    except UpdateError:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise UpdateError(
            f"Could not save update package ({e}).\n"
            "Tap Try Again — Hammer will re-download automatically."
        ) from e

    if not os.path.isfile(part_path):
        raise UpdateError(
            "Download finished but the package file is missing "
            "(security software likely removed it)."
        )

    try:
        got = os.path.getsize(part_path)
    except OSError as e:
        raise UpdateError(f"Could not read downloaded package: {e}") from e

    if expected_size and expected_size > 0 and got != expected_size:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise UpdateError(
            f"Download incomplete or corrupted.\n"
            f"Got {got} bytes, GitHub asset is {expected_size} bytes.\n"
            "Tap Try Again — Hammer will re-download automatically."
        )

    # Bare .exe assets must still be huge; zip packages are also large when frozen.
    if got < 50_000_000 and (
        dest_path.lower().endswith(".exe")
        or dest_path.lower().endswith(".bin" + _DOWNLOAD_SUFFIX)
        or dest_path.lower().endswith(".bin.hammerdl")
    ):
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise UpdateError(
            f"Downloaded file is only {got / (1024 * 1024):.1f} MB — that is not the "
            "Windows package (likely source zip). Need Hammer-stub-package.zip or "
            "HammerRuntime.exe."
        )

    try:
        os.replace(part_path, dest_path)
    except OSError as e:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise UpdateError(
            f"Could not finalize download package ({e}).\n"
            "Tap Try Again — Hammer will re-download automatically."
        ) from e

    _unblock_windows_download(dest_path)


def _unblock_windows_download(path: str) -> None:
    """
    Clear Mark-of-the-Web so SmartScreen does not silently block the new exe.
    Browser / GitHub downloads set Zone.Identifier; local PyInstaller builds do not.
    """
    if os.name != "nt":
        return
    zone = path + ":Zone.Identifier"
    try:
        if os.path.exists(zone):
            os.remove(zone)
    except OSError:
        pass
    # Also try PowerShell Unblock-File (handles some edge cases)
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f'Unblock-File -LiteralPath "{path}"',
            ],
            check=False,
            capture_output=True,
            creationflags=creationflags,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def app_root() -> str:
    return hammer_install_root()


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


# Filled by install helpers with path to _hammer_apply_update.bat (Windows).
_WINDOWS_UPDATE_BAT: list = [None]
# When set to a stub exe path, relaunch_and_exit uses a silent detached launch
# (no visible CMD) — preferred for stub-layout pending updates.
_SILENT_STUB_RELAUNCH: list = [None]


def _find_runtime_exe_in_tree(root: str) -> Optional[str]:
    """Prefer app/HammerRuntime.exe inside an extracted package."""
    direct = os.path.join(root, "app", RUNTIME_EXE_NAME)
    if os.path.isfile(direct):
        return direct
    found: Optional[str] = None
    for dirpath, _, files in os.walk(root):
        for fname in files:
            if fname.lower() == RUNTIME_EXE_NAME.lower():
                return os.path.join(dirpath, fname)
            if fname.lower() == "hammercandlebacktestdashboard.exe":
                path = os.path.join(dirpath, fname)
                try:
                    if os.path.getsize(path) >= MIN_RUNTIME_BYTES:
                        found = found or path
                except OSError:
                    pass
    return found


def _find_stub_exe_in_tree(root: str) -> Optional[str]:
    """Small launcher named HammerCandleBacktestDashboard.exe (not the huge runtime)."""
    direct = os.path.join(root, STUB_EXE_NAME)
    if os.path.isfile(direct):
        try:
            if os.path.getsize(direct) < MIN_RUNTIME_BYTES:
                return direct
        except OSError:
            return direct
    for dirpath, _, files in os.walk(root):
        for fname in files:
            if fname.lower() != STUB_EXE_NAME.lower():
                continue
            path = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(path) < MIN_RUNTIME_BYTES:
                    return path
            except OSError:
                return path
    return None


def _validate_runtime_file(path: str) -> None:
    size = os.path.getsize(path)
    if size < MIN_RUNTIME_BYTES:
        raise UpdateError(
            f"Runtime is only {size / (1024 * 1024):.1f} MB — expected ~420–450 MB.\n\n"
            "The release may be incomplete or the wrong file was attached."
        )


def _schedule_relaunch_stub(root: str, status_cb: Callable[[str], None], note: str) -> str:
    """
    After this process exits, start the stub (which applies pending + launches runtime).

    Stub-layout updates use a silent detached relaunch — no visible CMD window.
    """
    stub = stub_exe_path(root)
    if not os.path.isfile(stub):
        stub = os.path.abspath(sys.executable)

    # Preferred professional path: silent relaunch (stub applies .pending.bin).
    # No visible CMD — legacy one-file replace still uses the bat helpers.
    if os.path.isfile(stub):
        _WINDOWS_UPDATE_BAT[0] = None
        _SILENT_STUB_RELAUNCH[0] = stub
        status_cb("Update ready — restarting…")
        return root

    status_cb("Update staged.")
    return root


def _stage_runtime_pending(downloaded_runtime: str, status_cb: Callable[[str], None]) -> str:
    """
    Stage new runtime as HammerRuntime.pending.bin (non-.exe name).

    The stub applies it on next start. Keeping a non-.exe extension while the
    file sits on disk reduces security-software false positives mid-update.
    """
    root = app_root()
    os.makedirs(hammer_app_dir(root), exist_ok=True)
    writing = pending_runtime_bin_path(root)
    # Clear legacy *.exe.pending if present so stub prefers the bin name
    legacy = pending_runtime_path(root)

    for path in (writing, writing + ".writing", legacy):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    tmp = writing + ".writing"
    try:
        # If the download already landed on the pending path, just validate.
        if os.path.abspath(downloaded_runtime) == os.path.abspath(writing):
            _unblock_windows_download(writing)
            _validate_runtime_file(writing)
        elif os.path.abspath(downloaded_runtime) == os.path.abspath(tmp):
            _validate_runtime_file(tmp)
            os.replace(tmp, writing)
            _unblock_windows_download(writing)
            _validate_runtime_file(writing)
        else:
            status_cb("Preparing update…")
            _copy_file_with_progress(
                downloaded_runtime, tmp, status_cb, label="Preparing update"
            )
            _unblock_windows_download(tmp)
            _validate_runtime_file(tmp)
            os.replace(tmp, writing)
            _unblock_windows_download(writing)
            _validate_runtime_file(writing)
    except OSError as e:
        for path in (tmp, writing):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        raise UpdateError(
            f"Could not stage the new runtime ({e}).\n"
            "Tap Try Again — Hammer will re-download automatically."
        ) from e
    except UpdateError:
        for path in (tmp, writing):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        raise

    return _schedule_relaunch_stub(root, status_cb, "Pending runtime ready")


def _install_stub_package(
    next_stub: Optional[str],
    next_runtime: str,
    status_cb: Callable[[str], None],
) -> str:
    """
    Install stub launcher + runtime into install root.

    Never overwrites the running runtime in place — uses .pending when needed.
    First migration from legacy one-file: write app/runtime, then rename-swap stub.
    """
    root = app_root()
    os.makedirs(hammer_app_dir(root), exist_ok=True)
    _validate_runtime_file(next_runtime)

    # Always refresh stub when provided (stub is not the running UI process when
    # we are inside HammerRuntime; when legacy one-file, we swap after).
    if next_stub and os.path.isfile(next_stub) and not is_running_as_legacy_onefile():
        dest_stub = stub_exe_path(root)
        status_cb("Updating launcher...")
        shutil.copy2(next_stub, dest_stub + ".new")
        _unblock_windows_download(dest_stub + ".new")
        try:
            if os.path.isfile(dest_stub):
                os.replace(dest_stub, dest_stub + ".old")
            os.replace(dest_stub + ".new", dest_stub)
            try:
                os.remove(dest_stub + ".old")
            except OSError:
                pass
        except OSError:
            # Keep .new for the bat to finish
            pass

    if is_running_as_runtime() or is_stub_layout(root):
        return _stage_runtime_pending(next_runtime, status_cb)

    # Legacy one-file at install root: place runtime (path free), then swap stub in.
    status_cb("Installing runtime into app/...")
    dest_runtime = runtime_exe_path(root)
    shutil.copy2(next_runtime, dest_runtime)
    _unblock_windows_download(dest_runtime)
    _validate_runtime_file(dest_runtime)

    if next_stub and os.path.isfile(next_stub) and os.name == "nt":
        status_cb("Installing launcher (one-time migration)...")
        current_exe = os.path.abspath(sys.executable)
        staged_stub = os.path.join(root, STUB_EXE_NAME + ".new")
        shutil.copy2(next_stub, staged_stub)
        _unblock_windows_download(staged_stub)
        log_path = update_log_path()
        swapped = _swap_exe_while_running(staged_stub, current_exe, min_bytes=50_000)
        if swapped:
            bat = _write_windows_update_bat(
                pid=os.getpid(),
                exe_path=current_exe,
                lines=[
                    _bat_echo_log(log_path, "Stub installed; runtime in app/"),
                    f'del /F /Q "{current_exe}.old" 2>nul',
                    _bat_echo_log(log_path, "UPDATE_OK stub migration"),
                    *_windows_relaunch_lines(root, current_exe, log_path),
                ],
            )
        else:
            bat = _write_windows_update_bat(
                pid=os.getpid(),
                exe_path=current_exe,
                lines=[
                    *_windows_replace_exe_lines(
                        staged=staged_stub, current_exe=current_exe, log_path=log_path
                    ),
                    *_windows_relaunch_lines(root, current_exe, log_path),
                ],
            )
        _WINDOWS_UPDATE_BAT[0] = bat
        status_cb("Migration staged — closing to finish...")
        return root

    # Non-Windows or no stub in package: just save runtime path note
    status_cb("Runtime installed under app/.")
    return root


# Windows: apply update after this process exits (exe swap or onedir folder copy)
# _WINDOWS_UPDATE_BAT declared earlier (near stub install helpers)
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
            "Open Help → Check for Updates… and tap Try Again — Hammer will "
            "clean up and re-download automatically. Your data/ folder is safe."
        )
    if "UPDATE_OK" in text:
        return None
    return None


def _bat_echo_log(log_path: str, message: str) -> str:
    safe = message.replace('"', "'")
    return f'echo {safe}>>"{log_path}"'


def _bat_sleep_seconds(seconds: int) -> str:
    """
    Delay without `timeout` — timeout.exe often hangs forever when stdin is
    not a real console (common after CREATE_NEW_CONSOLE / detached spawn).
    """
    # ping -n N waits ~N-1 seconds
    n = max(2, int(seconds) + 1)
    return f"ping -n {n} 127.0.0.1 >nul"


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

    Prefer PowerShell Start-Process (reliable WorkingDirectory + no inherited
    PyInstaller env). Fall back to `start`. Avoid rundll32 FileProtocolHandler —
    it often returns success without actually launching a large one-file exe.
    """
    lines = list(_windows_clear_pyi_env_lines())
    lines.append("echo.")
    lines.append("echo Starting Hammer...")
    lines.append(_bat_echo_log(log_path, "Unblocking new exe (Mark of the Web)..."))
    ps_exe = exe_path.replace("'", "''")
    ps_root = root.replace("'", "''")
    lines.append(
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"try {{ Unblock-File -LiteralPath '{ps_exe}' }} catch {{ }}\""
    )
    lines.append(_bat_echo_log(log_path, "Relaunching Hammer via Start-Process..."))
    lines.append(f'if not defined HAMMER_LAUNCH set "HAMMER_LAUNCH={exe_path}"')
    lines.append(
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"Start-Process -FilePath ([string]$env:HAMMER_LAUNCH) -WorkingDirectory '{ps_root}'\""
    )
    lines.append("if errorlevel 1 (")
    lines.append(_bat_echo_log(log_path, "Start-Process failed — trying start"))
    lines.append(f'  start "" /D "{root}" "{exe_path}"')
    lines.append("  if errorlevel 1 (")
    lines.append(_bat_echo_log(log_path, "UPDATE_FAILED relaunch"))
    lines.append("    echo UPDATE FAILED — double-click the .exe manually")
    lines.append("    pause")
    lines.append("    exit /b 1")
    lines.append("  )")
    lines.append(")")
    lines.append(_bat_echo_log(log_path, "UPDATE_OK relaunch started"))
    lines.append("echo Done. This window will close in a few seconds.")
    lines.append(_bat_sleep_seconds(4))
    return lines


def _swap_exe_while_running(
    staged: str,
    current_exe: str,
    *,
    min_bytes: Optional[int] = MIN_RUNTIME_BYTES,
) -> bool:
    """
    Windows: rename the running one-file exe, then copy the new file into
    the original name.

    A running .exe can be RENAMED even when it cannot be deleted or
    overwritten. Waiting until after exit (old bat `move`) is what failed
    with "exe still locked" — Defender / bootloader still holds the path.

    min_bytes: size floor for the file written onto current_exe. Use a small
    value when installing the stub launcher (migration).
    """
    if os.name != "nt":
        return False
    old = current_exe + ".old"
    try:
        if os.path.isfile(old):
            try:
                os.remove(old)
            except OSError:
                old = current_exe + f".old.{os.getpid()}"
        os.replace(current_exe, old)
    except OSError:
        return False
    try:
        shutil.copy2(staged, current_exe)
        _unblock_windows_download(current_exe)
        if min_bytes and os.path.getsize(current_exe) < min_bytes:
            raise OSError("copied exe too small")
        try:
            os.remove(staged)
        except OSError:
            pass
        return True
    except OSError:
        try:
            if os.path.isfile(current_exe):
                os.remove(current_exe)
        except OSError:
            pass
        try:
            os.replace(old, current_exe)
        except OSError:
            pass
        return False


def _windows_wait_for_exit_lines(*, pid: int, exe_path: str, log_path: str) -> list[str]:
    """
    Wait until this Hammer PID exits. Do not taskkill by image name —
    that re-locks the .exe while we try to replace it.
    """
    return [
        _bat_echo_log(log_path, f"Waiting for Hammer PID {pid} to exit (max ~90s)..."),
        "set WAIT_N=0",
        ":wait_pid",
        f'tasklist /FI "PID eq {pid}" 2>nul | findstr /I /C:"{pid}" >nul',
        "if errorlevel 1 goto wait_pid_done",
        "set /a WAIT_N+=1",
        "if !WAIT_N! GEQ 90 (",
        _bat_echo_log(log_path, f"PID {pid} still alive after 90s — force continuing"),
        "  goto wait_pid_done",
        ")",
        _bat_sleep_seconds(1),
        "goto wait_pid",
        ":wait_pid_done",
        _bat_echo_log(log_path, "PID gone"),
        # Do NOT taskkill /IM the exe — that re-locks the file during move.
        _bat_echo_log(log_path, "Ready to finish update"),
        _bat_sleep_seconds(3),
    ]


def _windows_replace_exe_lines(*, staged: str, current_exe: str, log_path: str) -> list[str]:
    """
    Fallback if rename-while-running failed.

    Copy the new file to a sidecar first (never blocked by the old lock),
    try a short rename of the old exe, then either copy onto the original
    name or launch the sidecar.
    """
    sidecar = (
        current_exe[:-4] + "-updated.exe"
        if current_exe.lower().endswith(".exe")
        else current_exe + "-updated.exe"
    )
    return [
        _bat_echo_log(log_path, "Fallback replace via sidecar copy"),
        f'copy /Y "{staged}" "{sidecar}"',
        "if errorlevel 1 (",
        _bat_echo_log(log_path, "UPDATE_FAILED could not copy sidecar"),
        "  pause",
        "  exit /b 1",
        ")",
        f'powershell -NoProfile -ExecutionPolicy Bypass -Command "try {{ Unblock-File -LiteralPath \'{sidecar.replace(chr(39), chr(39)+chr(39))}\' }} catch {{ }}"',
        f'set "HAMMER_LAUNCH={sidecar}"',
        f'del /F /Q "{current_exe}.old" 2>nul',
        "set MOVE_TRIES=0",
        ":move_retry",
        "set /a MOVE_TRIES+=1",
        f'move /Y "{current_exe}" "{current_exe}.old"',
        "if errorlevel 1 (",
        "  if !MOVE_TRIES! lss 8 (",
        _bat_echo_log(log_path, "move retry (exe still locked)"),
        "    " + _bat_sleep_seconds(3),
        "    goto move_retry",
        "  )",
        _bat_echo_log(log_path, "Old exe stayed locked — will launch -updated.exe"),
        "  goto replace_done",
        ")",
        f'copy /Y "{sidecar}" "{current_exe}"',
        "if not errorlevel 1 (",
        f'  set "HAMMER_LAUNCH={current_exe}"',
        f'  del /F /Q "{sidecar}" 2>nul',
        f'  del /F /Q "{current_exe}.old" 2>nul',
        ")",
        ":replace_done",
        f'del /F /Q "{staged}" 2>nul',
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
        "title Hammer — applying update",
        f'echo Hammer update started >"{log_path}"',
        _bat_echo_log(log_path, "Apply script started"),
        "echo Applying Hammer update — do not close this window.",
        "echo.",
    ]
    script.extend(_windows_wait_for_exit_lines(pid=pid, exe_path=exe_path, log_path=log_path))
    script.extend(lines)
    # Delete bat after relaunch scheduling (not mid-script)
    script.append('del /F /Q "%~f0" 2>nul')
    with open(bat, "w", encoding="ascii", newline="\r\n", errors="replace") as f:
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
        comspec = env.get("ComSpec") or os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
        # Always invoke via full ComSpec path; wrap bat with "call" so errors propagate
        if len(cmd) >= 3 and str(cmd[0]).lower() in ("cmd", "cmd.exe") and cmd[1].lower() == "/c":
            bat = cmd[2]
            argv = [comspec, "/d", "/c", "call", bat]
        else:
            argv = cmd
        kwargs: dict = {
            "env": env,
            "cwd": app_root(),
            "close_fds": True,
            "creationflags": _CREATE_NEW_CONSOLE,
        }
        subprocess.Popen(argv, **kwargs)
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
    """Install a downloaded runtime .exe into stub layout (or legacy one-file swap)."""
    root = app_root()
    if not getattr(sys, "frozen", False):
        dest = os.path.join(root, os.path.basename(downloaded_exe))
        status_cb("Saving new executable...")
        shutil.copy2(downloaded_exe, dest)
        return root

    _validate_runtime_file(downloaded_exe)

    # Preferred path: never replace the running UI binary in place
    if is_running_as_runtime() or is_stub_layout(root):
        return _stage_runtime_pending(downloaded_exe, status_cb)

    # Legacy single-file install — keep old rename/sidecar path once
    current_exe = os.path.abspath(sys.executable)
    dest_name = os.path.basename(current_exe)
    staged = os.path.join(root, dest_name + ".new")
    status_cb("Staging new executable...")
    shutil.copy2(downloaded_exe, staged)
    _unblock_windows_download(staged)
    _validate_runtime_file(staged)

    if os.name == "nt":
        log_path = update_log_path()
        swapped = _swap_exe_while_running(staged, current_exe)
        if swapped:
            status_cb("New exe installed (old file renamed). Closing to finish...")
            bat = _write_windows_update_bat(
                pid=os.getpid(),
                exe_path=current_exe,
                lines=[
                    _bat_echo_log(log_path, "New exe already in place"),
                    f'del /F /Q "{current_exe}.old" 2>nul',
                    f'del /F /Q "{current_exe}.old.*" 2>nul',
                    _bat_echo_log(log_path, "UPDATE_OK exe replaced"),
                    *_windows_relaunch_lines(root, current_exe, log_path),
                ],
            )
        else:
            status_cb("Could not rename running exe — will copy sidecar after close.")
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

    # Stub-layout pending update: silent detached relaunch (no CMD flash).
    silent_stub = _SILENT_STUB_RELAUNCH[0]
    if silent_stub and os.path.isfile(silent_stub):
        _SILENT_STUB_RELAUNCH[0] = None
        time.sleep(0.35)
        _spawn_frozen_relaunch(silent_stub, root)
        time.sleep(0.2)
        os._exit(0)

    update_bat = _WINDOWS_UPDATE_BAT[0]
    if update_bat and os.path.isfile(update_bat):
        _spawn_detached(["cmd", "/c", update_bat])
        # Give the helper a moment to start before we kill this process
        time.sleep(0.6)
        os._exit(0)

    # Prefer stub so pending runtime is applied before UI start
    launch_exe = python_exe
    stub = stub_exe_path(root)
    if getattr(sys, "frozen", False) and os.path.isfile(stub):
        launch_exe = stub

    if os.name == "nt":
        if getattr(sys, "frozen", False):
            _spawn_frozen_relaunch(launch_exe, root)
        else:
            relauncher = os.path.join(root, "_hammer_relaunch.bat")
            with open(relauncher, "w", encoding="ascii", newline="\r\n", errors="replace") as f:
                f.write("@echo off\r\n")
                f.write(f"{_bat_sleep_seconds(1)}\r\n")
                f.write(f'start "" /D "{root}" "{python_exe}" "{main_script}"\r\n')
                f.write('del /F /Q "%~f0" 2>nul\r\n')
            _spawn_detached(["cmd", "/c", relauncher])
            time.sleep(0.5)
    else:
        if getattr(sys, "frozen", False):
            env = os.environ.copy()
            for key in list(env):
                if str(key).startswith("_PYI_"):
                    env.pop(key, None)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            subprocess.Popen([launch_exe], cwd=root, env=env)
        else:
            subprocess.Popen([python_exe, main_script], cwd=root)

    os._exit(0)


def _zip_find_stub_runtime_members(
    zf: zipfile.ZipFile,
) -> tuple[Optional[zipfile.ZipInfo], Optional[zipfile.ZipInfo]]:
    """Locate small stub + large runtime members inside a release zip."""
    runtime_info: Optional[zipfile.ZipInfo] = None
    stub_info: Optional[zipfile.ZipInfo] = None
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base == RUNTIME_EXE_NAME.lower() and info.file_size >= MIN_RUNTIME_BYTES:
            runtime_info = info
        elif (
            base == STUB_EXE_NAME.lower()
            and info.file_size > 0
            and info.file_size < MIN_RUNTIME_BYTES
        ):
            # Prefer the smaller launcher if multiple matches exist
            if stub_info is None or info.file_size < stub_info.file_size:
                stub_info = info
    return stub_info, runtime_info


def _stream_zip_member_to_file(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dest: str,
    status_cb: Callable[[str], None],
    *,
    label: str,
) -> None:
    total = int(info.file_size or 0)
    copied = 0
    last_pct = -1
    chunk = 1024 * 1024
    with zf.open(info, "r") as src, open(dest, "wb") as out:
        while True:
            buf = src.read(chunk)
            if not buf:
                break
            out.write(buf)
            copied += len(buf)
            if total > 0:
                pct = int(copied * 100 / total)
                if pct != last_pct and (pct % 5 == 0 or pct >= 99):
                    last_pct = pct
                    status_cb(
                        f"{label}… {copied / (1024 * 1024):.0f} / "
                        f"{total / (1024 * 1024):.0f} MB"
                    )
    got = os.path.getsize(dest)
    if total and got != total:
        raise UpdateError(
            f"Extract incomplete ({got} of {total} bytes). Tap Try Again."
        )


def _install_stub_package_from_zip_fast(
    asset_path: str,
    status_cb: Callable[[str], None],
) -> Optional[str]:
    """
    Fast path for stub-layout installs: stream only runtime (+ stub) from the zip
    into place — no full extract tree / double copy of ~430 MB.
    Returns install root on success, or None to fall back to full extract.
    """
    if not (is_running_as_runtime() or is_stub_layout(app_root())):
        return None

    root = app_root()
    try:
        with zipfile.ZipFile(asset_path, "r") as zf:
            stub_info, runtime_info = _zip_find_stub_runtime_members(zf)
            if runtime_info is None:
                return None

            os.makedirs(hammer_app_dir(root), exist_ok=True)
            writing = pending_runtime_bin_path(root)
            tmp = writing + ".writing"
            for path in (writing, tmp, pending_runtime_path(root)):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass

            status_cb("Installing update…")
            _stream_zip_member_to_file(
                zf, runtime_info, tmp, status_cb, label="Installing update"
            )
            _unblock_windows_download(tmp)
            _validate_runtime_file(tmp)
            os.replace(tmp, writing)
            _unblock_windows_download(writing)
            _validate_runtime_file(writing)

            if stub_info is not None and not is_running_as_legacy_onefile():
                status_cb("Updating launcher…")
                stub_tmp = stub_exe_path(root) + ".new"
                _stream_zip_member_to_file(
                    zf, stub_info, stub_tmp, status_cb, label="Updating launcher"
                )
                _unblock_windows_download(stub_tmp)
                dest_stub = stub_exe_path(root)
                try:
                    if os.path.isfile(dest_stub):
                        os.replace(dest_stub, dest_stub + ".old")
                    os.replace(stub_tmp, dest_stub)
                    try:
                        os.remove(dest_stub + ".old")
                    except OSError:
                        pass
                except OSError:
                    pass

            return _schedule_relaunch_stub(root, status_cb, "Pending runtime ready (fast zip)")
    except UpdateError:
        raise
    except Exception:
        # Corrupt zip / unexpected layout — fall back to full extract
        return None


def _install_from_zip(
    asset_path: str,
    status_cb: Callable[[str], None],
) -> str:
    """Install a release .zip — source tree (dev) or Windows onedir/exe payload (frozen)."""
    if getattr(sys, "frozen", False):
        fast = _install_stub_package_from_zip_fast(asset_path, status_cb)
        if fast is not None:
            return fast

    status_cb("Extracting update…")
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
                    "The maintainer must attach Hammer-stub-package.zip "
                    "(stub + runtime) from GitHub Actions to the GitHub Release."
                )
            runtime_in_tree = _find_runtime_exe_in_tree(source_root)
            stub_in_tree = _find_stub_exe_in_tree(source_root)
            if runtime_in_tree:
                try:
                    return _install_stub_package(
                        next_stub=stub_in_tree,
                        next_runtime=runtime_in_tree,
                        status_cb=status_cb,
                    )
                except TypeError as exc:
                    # Safety net if an old broken binary somehow runs new assets
                    raise UpdateError(
                        "Zip install failed on this build (known 1.0.27/1.0.28 bug).\n\n"
                        "Close Hammer, download HammerRuntime.exe from the GitHub "
                        "Release, replace app\\HammerRuntime.exe, then reopen the "
                        f"launcher.\n\n({exc})"
                    ) from exc
            exe_in_tree = _find_hammer_exe_in_tree(source_root)
            if exe_in_tree:
                payload_root = os.path.dirname(exe_in_tree)
                if _is_onedir_payload(payload_root):
                    if os.name == "nt":
                        return _schedule_windows_onedir_update(payload_root, status_cb)
                    status_cb("Installing update…")
                    _copy_over_app(payload_root, app_root())
                    status_cb("Update installed.")
                    return app_root()
                return _install_exe(exe_in_tree, status_cb)
            raise UpdateError(
                "Could not find HammerRuntime.exe (or a full Hammer .exe) inside "
                "the release zip."
            )

        status_cb("Installing update…")
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

    Downloads into <install>/_hammer_update_cache as a non-.exe package and
    auto-retries when security software deletes/locks the file mid-transfer.
    """
    last_err: Optional[BaseException] = None
    cache_dir = _update_cache_dir()
    asset_path = os.path.join(cache_dir, _safe_download_name(release.asset_name))

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        cleanup_broken_update_files()
        try:
            if attempt == 1:
                status_cb(f"Downloading {release.asset_name}...")
            else:
                status_cb(
                    f"Security software interrupted the transfer — "
                    f"retrying automatically ({attempt}/{_DOWNLOAD_ATTEMPTS})..."
                )
                time.sleep(1.2 * attempt)

            _download_file(
                release.download_url,
                asset_path,
                progress_cb,
                expected_size=release.asset_size or None,
            )
            _unblock_windows_download(asset_path)

            # Prefer treating by kind; .hammerdl names keep the original zip/exe identity
            # via release.asset_kind / original asset_name.
            name_low = (release.asset_name or "").lower()
            if release.asset_kind == "exe" or name_low.endswith(".exe"):
                root = _install_exe(asset_path, status_cb)
            else:
                root = _install_from_zip(asset_path, status_cb)

            # Success — drop the bulky cache package
            try:
                if os.path.isfile(asset_path):
                    os.remove(asset_path)
            except OSError:
                pass
            return root
        except UpdateError as e:
            last_err = e
            for path in (asset_path, asset_path + ".part"):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            if attempt >= _DOWNLOAD_ATTEMPTS or not _looks_like_av_interference(e):
                break
        except OSError as e:
            last_err = e
            if attempt >= _DOWNLOAD_ATTEMPTS or not _looks_like_av_interference(e):
                break

    detail = str(last_err) if last_err else "unknown error"
    raise UpdateError(
        "Update could not finish after automatic retries.\n\n"
        f"{detail}\n\n"
        "Tap Try Again in this window — Hammer will clean up and re-download. "
        "Your data/ folder is never touched."
    ) from last_err
