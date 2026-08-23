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


def is_newer(remote_version: str, local_version: str = CURRENT_VERSION) -> bool:
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
    """Prefer EXE when frozen; otherwise prefer a .zip of source."""
    if not assets:
        return None
    exes = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]

    if getattr(sys, "frozen", False):
        # Prefer branded exe name if present
        for a in exes:
            if "hammer" in str(a.get("name", "")).lower():
                return a
        if exes:
            return exes[0]
        if zips:
            return zips[0]
        return None

    if zips:
        return zips[0]
    if exes:
        return exes[0]
    return None


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

    if is_newer(info.version):
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


def _install_exe(downloaded_exe: str, status_cb: Callable[[str], None]) -> str:
    """Replace the running frozen EXE (Windows). Returns app_root."""
    root = app_root()
    if not getattr(sys, "frozen", False):
        # Script mode receiving an exe: drop it next to the app for the user
        dest = os.path.join(root, os.path.basename(downloaded_exe))
        status_cb("Saving new executable...")
        shutil.copy2(downloaded_exe, dest)
        return root

    current_exe = sys.executable
    dest_name = os.path.basename(current_exe)
    staged = os.path.join(root, dest_name + ".new")
    status_cb("Staging new executable...")
    shutil.copy2(downloaded_exe, staged)

    # On Windows, replace after this process exits (file lock).
    if os.name == "nt":
        bat = os.path.join(tempfile.gettempdir(), "hammer_replace_exe.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            f.write("timeout /t 2 /nobreak > nul\r\n")
            f.write(f'copy /Y "{staged}" "{current_exe}"\r\n')
            f.write(f'del /F /Q "{staged}"\r\n')
            f.write(f'start "" "{current_exe}"\r\n')
            f.write('del "%~f0"\r\n')
        # Relaunch is handled by the bat; mark for exit-only path
        status_cb("Executable staged — will replace on relaunch.")
        # Stash bat path for relaunch_and_exit
        _EXE_REPLACE_BAT[0] = bat
    else:
        status_cb("Replacing executable...")
        shutil.move(staged, current_exe)
        try:
            os.chmod(current_exe, 0o755)
        except OSError:
            pass
    return root


# Shared between install and relaunch when a Windows exe swap is pending
_EXE_REPLACE_BAT: list = [None]


def relaunch_and_exit(root: Optional[str] = None) -> None:
    """Spawn a fresh Hammer process, then hard-exit this one."""
    root = root or app_root()
    python_exe = sys.executable
    main_script = os.path.join(root, "main.py")

    replace_bat = _EXE_REPLACE_BAT[0]
    if replace_bat and os.path.isfile(replace_bat):
        subprocess.Popen(
            ["cmd", "/c", replace_bat],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        os._exit(0)

    if os.name == "nt":
        relauncher = os.path.join(tempfile.gettempdir(), "hammer_relaunch.bat")
        with open(relauncher, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            f.write("timeout /t 1 /nobreak > nul\r\n")
            if getattr(sys, "frozen", False):
                f.write(f'start "" "{python_exe}"\r\n')
            else:
                f.write(f'start "" "{python_exe}" "{main_script}"\r\n')
            f.write('del "%~f0"\r\n')
        subprocess.Popen(
            ["cmd", "/c", relauncher],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    else:
        if getattr(sys, "frozen", False):
            subprocess.Popen([python_exe])
        else:
            subprocess.Popen([python_exe, main_script])

    os._exit(0)


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

        status_cb("Extracting update...")
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(asset_path, "r") as zf:
            zf.extractall(extract_dir)
        source_root = _find_source_root(extract_dir)

        status_cb("Installing update...")
        root = app_root()
        _copy_over_app(source_root, root)
        status_cb("Update installed.")
        return root
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
