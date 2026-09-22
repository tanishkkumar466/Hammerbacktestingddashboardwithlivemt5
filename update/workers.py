"""
QThread workers for Hammer's self-updater.

Runs updater.py off the GUI thread and reports via Qt Signals.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import update.updater as updater


class CheckUpdateWorker(QThread):
    """Runs updater.check_for_update() off the main thread."""

    found = Signal(object)  # ReleaseInfo or None
    failed = Signal(str)

    def run(self):
        try:
            result = updater.check_for_update()
            self.found.emit(result)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class DownloadInstallWorker(QThread):
    """Runs updater.download_and_install() off the main thread."""

    progress = Signal(int, int)  # downloaded, total
    status = Signal(str)
    finished_ok = Signal(str)  # app_root
    failed = Signal(str)

    def __init__(self, release: updater.ReleaseInfo, parent=None):
        super().__init__(parent)
        self.release = release
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self):
        try:
            def _progress(done: int, total: int) -> None:
                if self._cancel:
                    raise updater.UpdateError("Update cancelled.")
                self.progress.emit(done, total)

            def _status(text: str) -> None:
                if self._cancel:
                    raise updater.UpdateError("Update cancelled.")
                self.status.emit(text)

            app_root = updater.download_and_install(
                self.release,
                progress_cb=_progress,
                status_cb=_status,
            )
            if self._cancel:
                updater.cleanup_broken_update_files()
                self.failed.emit("Update cancelled.")
                return
            self.finished_ok.emit(app_root)
        except Exception as e:  # noqa: BLE001
            try:
                updater.cleanup_broken_update_files()
            except Exception:
                pass
            if self._cancel:
                self.failed.emit("Update cancelled.")
            else:
                self.failed.emit(str(e))
