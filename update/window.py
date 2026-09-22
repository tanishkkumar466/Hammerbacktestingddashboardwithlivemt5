"""
"Check for Updates" dialog for Hammer (Safari-style).

Slow work runs in update.workers.QThread subclasses; this file only
reacts to their signals on the GUI thread.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

import update.updater as updater
from update.workers import CheckUpdateWorker, DownloadInstallWorker
from version import __version__ as CURRENT_VERSION


class UpdateWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Software Update")
        self.setFixedSize(420, 300)

        self._release = None
        self._check_worker = None
        self._install_worker = None
        self._auto_retries = 0
        self._closing = False

        self._build_ui()
        self._start_check()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(6)

        self.icon_label = QLabel("⟳")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setStyleSheet("color: #3b82f6; font-size: 30px;")
        layout.addWidget(self.icon_label)

        self.title_label = QLabel("Checking for updates...")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        layout.addWidget(self.title_label)

        self.subtitle_label = QLabel(f"Current version: {CURRENT_VERSION}")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setStyleSheet("color: #666666; font-size: 11px;")
        layout.addSpacing(4)
        layout.addWidget(self.subtitle_label)
        layout.addSpacing(8)

        self.notes_text = QTextEdit()
        self.notes_text.setReadOnly(True)
        self.notes_text.setFixedHeight(90)
        self.notes_text.setStyleSheet(
            "background: white; border: 1px solid #d1d5db; font-size: 11px;"
        )
        self.notes_text.hide()
        layout.addWidget(self.notes_text)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #777777; font-size: 10px;")
        layout.addWidget(self.status_label)

        layout.addStretch()

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.secondary_btn = QPushButton("Close")
        self.secondary_btn.clicked.connect(self.close)
        button_row.addWidget(self.secondary_btn)

        self.primary_btn = QPushButton("Download && Install")
        self.primary_btn.setEnabled(False)
        self.primary_btn.setDefault(True)
        self.primary_btn.clicked.connect(self._on_download_clicked)
        button_row.addWidget(self.primary_btn)

        layout.addLayout(button_row)

    def _start_check(self):
        self._check_worker = CheckUpdateWorker()
        self._check_worker.found.connect(self._on_check_result)
        self._check_worker.failed.connect(self._on_check_error)
        self._check_worker.start()

    def _on_check_result(self, release):
        if self._closing:
            return
        if release is None:
            self.icon_label.setText("✓")
            self.icon_label.setStyleSheet("color: #22c55e; font-size: 30px;")
            self.title_label.setText("You're up to date")
            self.subtitle_label.setText(
                f"Version {CURRENT_VERSION} is the latest version."
            )
            self.primary_btn.setEnabled(False)
        else:
            self._release = release
            self.icon_label.setText("⬆")
            self.icon_label.setStyleSheet("color: #3b82f6; font-size: 30px;")
            self.title_label.setText(f"Version {release.version} is available")
            self.subtitle_label.setText(
                f"You have {CURRENT_VERSION} — new: {release.version}"
            )
            if release.notes.strip():
                self.notes_text.setPlainText(release.notes.strip())
                self.notes_text.show()
            self.primary_btn.setEnabled(True)

    def _on_check_error(self, message: str):
        if self._closing:
            return
        self.icon_label.setText("⚠")
        self.icon_label.setStyleSheet("color: #ef4444; font-size: 30px;")
        self.title_label.setText("Couldn't check for updates")
        first_line = (message or "").strip().split("\n", 1)[0]
        self.subtitle_label.setText(first_line[:120] or "Unknown error")
        self.notes_text.setPlainText(message.strip())
        self.notes_text.show()
        self.primary_btn.setEnabled(False)

    def _on_download_clicked(self):
        if not self._release:
            return
        self._auto_retries = 0
        self.primary_btn.setEnabled(False)
        self.secondary_btn.setEnabled(False)
        self.notes_text.hide()
        self.progress.setValue(0)
        self.progress.show()
        self.status_label.setText("Starting download…")
        self._start_install_worker()

    def _start_install_worker(self):
        self._install_worker = DownloadInstallWorker(self._release)
        self._install_worker.progress.connect(self._on_progress)
        self._install_worker.status.connect(self._on_status)
        self._install_worker.finished_ok.connect(self._on_install_done)
        self._install_worker.failed.connect(self._on_install_error)
        self._install_worker.start()

    def _on_progress(self, downloaded: int, total: int):
        if self._closing:
            return
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(downloaded)
            pct = int(downloaded / total * 100)
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self.status_label.setText(
                f"{pct}%  ({mb_done:.1f} MB of {mb_total:.1f} MB)"
            )
        else:
            self.progress.setRange(0, 0)
            self.status_label.setText(
                f"{downloaded / (1024 * 1024):.1f} MB downloaded"
            )

    def _on_status(self, text: str):
        if self._closing:
            return
        self.status_label.setText(text)

    def _on_install_done(self, app_root: str):
        if self._closing:
            return
        self._auto_retries = 0
        self.icon_label.setText("✓")
        self.icon_label.setStyleSheet("color: #22c55e; font-size: 30px;")
        self.title_label.setText("Update ready")
        self.status_label.setText("Restarting Hammer…")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        QTimer.singleShot(500, lambda: updater.relaunch_and_exit(app_root))

    def _on_install_error(self, message: str):
        if self._closing:
            return
        msg = (message or "").strip()
        if msg.lower().startswith("update cancelled"):
            self.title_label.setText("Update cancelled")
            self.status_label.setText("")
            self.secondary_btn.setEnabled(True)
            self.primary_btn.setEnabled(bool(self._release))
            self.primary_btn.setText("Download && Install")
            return

        # One quiet auto-retry after engine retries (interrupted transfers).
        if (
            self._release
            and self._auto_retries < 1
            and updater._looks_like_av_interference(Exception(msg))
        ):
            self._auto_retries += 1
            self.status_label.setText("Interrupted — retrying automatically…")
            self.progress.setValue(0)
            QTimer.singleShot(800, self._start_install_worker)
            return

        self.icon_label.setText("⚠")
        self.icon_label.setStyleSheet("color: #ef4444; font-size: 30px;")
        self.title_label.setText("Couldn't finish update")
        self.status_label.setText("Your data is safe. Tap Try Again when ready.")
        # Keep technical detail available but collapsed-feeling in notes
        short = msg.split("\n", 1)[0][:160]
        self.notes_text.setPlainText(msg if len(msg) < 800 else short + "\n\n" + msg[:800])
        self.notes_text.show()
        self.secondary_btn.setEnabled(True)
        self.primary_btn.setEnabled(True)
        self.primary_btn.setText("Try Again")
        QMessageBox.information(
            self,
            "Software Update",
            "The update didn't finish.\n\n"
            "Tap Try Again — Hammer will clean up and download again.\n"
            "Your data folder is never touched.",
        )

    def closeEvent(self, event):
        self._closing = True
        if self._install_worker and self._install_worker.isRunning():
            try:
                self._install_worker.request_cancel()
            except Exception:
                pass
            self._install_worker.setParent(None)
            self._install_worker.finished.connect(self._install_worker.deleteLater)
        if self._check_worker and self._check_worker.isRunning():
            self._check_worker.setParent(None)
            self._check_worker.finished.connect(self._check_worker.deleteLater)
        try:
            updater.cleanup_broken_update_files()
        except Exception:
            pass
        super().closeEvent(event)


def open_update_window(parent=None):
    dlg = UpdateWindow(parent)
    dlg.exec()
