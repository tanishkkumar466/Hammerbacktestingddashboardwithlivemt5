#!/usr/bin/env python3
"""
Fast local/CI sanity checks for the stub-update pipeline (no PyInstaller).

Fails early on import cycles (stub must not pull Qt), asset picking, and
install-root layout helpers.
"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

# Windows CI consoles are often cp1252 — keep prints ASCII-safe and force UTF-8 when possible
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(msg: str) -> None:
    raise SystemExit(f"PIPELINE CHECK FAILED: {msg}")


def check_stub_imports_are_light() -> None:
    # Import paths the way the stub does - must not load PySide6
    before = {k for k in sys.modules if k.startswith("PySide6") or k == "PySide6"}
    import update.paths  # noqa: F401
    import update.stub  # noqa: F401

    after = {k for k in sys.modules if k.startswith("PySide6") or k == "PySide6"}
    leaked = after - before
    if leaked:
        _fail(f"update.stub/paths pulled Qt into memory: {sorted(leaked)}")
    print("OK stub imports stay Qt-free")


def check_install_root() -> None:
    from update.paths import install_root

    root = Path(install_root())
    if root.resolve() != ROOT.resolve():
        _fail(f"install_root()={root} expected repo root {ROOT}")
    print("OK install_root -> repo root")


def check_asset_pick() -> None:
    import update.updater as updater

    updater.sys.frozen = True  # type: ignore[attr-defined]
    picked = updater._pick_release_asset(
        [
            {"name": "HammerCandleBacktestDashboard.exe", "size": 430_000_000},
            {"name": "Hammer-stub-package.zip", "size": 435_000_000},
            {"name": "Source code.zip", "size": 100_000},
        ]
    )
    if not picked or picked["name"] != "Hammer-stub-package.zip":
        _fail(f"expected Hammer-stub-package.zip, got {picked}")
    print("OK frozen asset pick prefers Hammer-stub-package.zip")

    # Recovery path used for 1.0.27/1.0.28: no legacy windows zip name → .exe bridge
    picked_exe = updater._pick_release_asset(
        [
            {"name": "HammerCandleBacktestDashboard.exe", "size": 430_000_000},
            {"name": "HammerRuntime.exe", "size": 430_000_000},
        ]
    )
    if not picked_exe or picked_exe["name"] != "HammerCandleBacktestDashboard.exe":
        _fail(f"expected bridge exe when stub zip absent, got {picked_exe}")
    print("OK frozen asset pick falls back to bridge exe")


def check_zip_layout_detection() -> None:
    import update.updater as updater

    # Keep the size-gate helpers but avoid writing a 350MB temp file.
    # Stub sample must stay under this floor; runtime is found by filename.
    original = updater.MIN_RUNTIME_BYTES
    updater.MIN_RUNTIME_BYTES = 50_000
    try:
        td = Path(tempfile.mkdtemp())
        pkg = td / "pkg"
        (pkg / "app").mkdir(parents=True)
        (pkg / "HammerCandleBacktestDashboard.exe").write_bytes(b"STUB" * 200)  # ~800 bytes
        rt = pkg / "app" / "HammerRuntime.exe"
        rt.write_bytes(b"R" * 60_000)
        zpath = td / "Hammer-stub-package.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for p in pkg.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(pkg).as_posix())
        extract = td / "extract"
        extract.mkdir()
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(extract)
        root = updater._find_source_root(str(extract))
        runtime = updater._find_runtime_exe_in_tree(root)
        stub = updater._find_stub_exe_in_tree(root)
        if not runtime or not stub:
            _fail(f"zip layout detect failed runtime={runtime} stub={stub}")
        # stub finder uses MIN_RUNTIME_BYTES to reject huge stubs
        if os.path.getsize(stub) >= updater.MIN_RUNTIME_BYTES:
            _fail("stub classified incorrectly as runtime-sized")
        print("OK zip stub+runtime detection")
    finally:
        updater.MIN_RUNTIME_BYTES = original


def check_embedded_stub_finder() -> None:
    import update.migrate as migrate

    td = Path(tempfile.mkdtemp())
    embed = td / "_hammer_embedded_stub"
    embed.mkdir()
    stub = embed / "HammerCandleBacktestDashboard.exe"
    stub.write_bytes(b"stub" * 1000)
    migrate.sys.frozen = True  # type: ignore[attr-defined]
    migrate.sys._MEIPASS = str(td)  # type: ignore[attr-defined]
    found = migrate.find_embedded_stub()
    if found != str(stub):
        _fail(f"embedded stub find failed: {found}")
    print("OK embedded stub finder")


def check_workflow_scripts_exist() -> None:
    for rel in (
        "HammerStub.spec",
        "HammerCandleBacktestDashboard.spec",
        "scripts/ci_package_stub_windows.ps1",
        "scripts/ci_smoke_stub_windows.ps1",
        ".github/workflows/release-windows-exe.yml",
        ".github/workflows/build-windows-exe.yml",
        "update/stub.py",
        "update/migrate.py",
        "update/paths.py",
        "update/updater.py",
        "update/window.py",
        "update/workers.py",
    ):
        if not (ROOT / rel).is_file():
            _fail(f"missing required file: {rel}")
    # Spec must embed stub from stash
    spec = (ROOT / "HammerCandleBacktestDashboard.spec").read_text(encoding="utf-8")
    if "_hammer_embedded_stub" not in spec or "_stash" not in spec:
        _fail("main spec missing stub-embed wiring")
    stub_spec = (ROOT / "HammerStub.spec").read_text(encoding="utf-8")
    if "update/stub.py" not in stub_spec:
        _fail("HammerStub.spec must analyze update/stub.py")
    print("OK workflow + spec files present")


def main() -> int:
    os.chdir(ROOT)
    check_workflow_scripts_exist()
    check_install_root()
    check_stub_imports_are_light()
    check_asset_pick()
    check_zip_layout_detection()
    check_embedded_stub_finder()
    print("ALL PIPELINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
