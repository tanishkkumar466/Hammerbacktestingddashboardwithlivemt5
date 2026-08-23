"""
QThread workers for Hammer's self-updater.

Runs updater.py off the GUI thread and reports via Qt Signals.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import updater


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

    def run(self):
        try:
            app_root = updater.download_and_install(
                self.release,
                progress_cb=lambda done, total: self.progress.emit(done, total),
                status_cb=lambda text: self.status.emit(text),
            )
            self.finished_ok.emit(app_root)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
