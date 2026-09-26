"""
History window (menu bar → History): every Live event across accounts / slots,
with the entry + SL (+ buffer) + TP breakdown for the selected trade.

Layout mirrors Results → History: ℹ help, one filter row (Search / Account /
Pattern / Show / Columns… / count), sortable table, Refresh / Export to Excel row.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import live_history

MAX_ROWS = 5000
REFRESH_MS = 3000
SETTINGS_COLUMNS_KEY = "live_history/visible_columns"

SHOW_FILTERS = (
    ("All events", None),
    ("Trades (signal / order / exit)", "trade"),
    ("Problems (errors, blocks, SL moved)", "problem"),
    ("Errors & safety blocks", "error"),
    ("Skipped setups", "skip"),
)

# (row key, header, visible by default)
COLUMN_DEFS: Tuple[Tuple[str, str, bool], ...] = (
    ("logged_at", "Time", True),
    ("account", "Account", True),
    ("slot", "Slot", True),
    ("preset", "Preset", True),
    ("pattern", "Pattern", True),
    ("pattern_variant", "Variant", False),
    ("symbol", "Symbol", False),
    ("timeframe", "TF", True),
    ("event", "Event", True),
    ("direction", "Side", True),
    ("strategy_entry", "Strategy entry", False),
    ("entry_price", "Entry", True),
    ("sl_anchor", "Candle low/high", True),
    ("sl_buffer_amount", "SL buffer", True),
    ("stop_loss", "SL", True),
    ("_sl_check", "SL check", True),
    ("target", "TP", True),
    ("rr_multiple", "RR", False),
    ("volume", "Lots", True),
    ("order_mode", "Order type", False),
    ("dry_run", "Dry run", False),
    ("magic", "Magic", False),
    ("mt5_order_id", "Ticket", False),
    ("exit_price", "Exit", False),
    ("profit", "Profit", True),
    ("close_reason", "Close reason", False),
    ("signal_bar_time", "Signal candle", False),
    ("mt5_message", "MT5 message", False),
    ("reason", "Reason / error", True),
)
HEADER_BY_KEY = {k: h for k, h, _v in COLUMN_DEFS}
DEFAULT_VISIBLE = [k for k, _h, v in COLUMN_DEFS if v]
NUMERIC_KEYS = {
    "strategy_entry", "entry_price", "sl_anchor", "sl_buffer_amount", "stop_loss", "target",
    "rr_multiple", "volume", "exit_price", "profit",
}

PROBLEM_BG = QColor("#FDECEA")
SKIP_FG = QColor("#80868B")
WIN_FG = QColor("#137333")
LOSS_FG = QColor("#C5221F")


def row_has_problem(row: Dict[str, str]) -> bool:
    if live_history.event_category(row.get("event", "")) == "error":
        return True
    if live_history.sl_check_text(row).startswith("MOVED"):
        return True
    try:
        return float(row.get("sl_buffer_amount") or 0) < -1e-9
    except (TypeError, ValueError):
        return False


def _cell_text(row: Dict[str, str], key: str) -> str:
    if key == "_sl_check":
        return live_history.sl_check_text(row)
    raw = row.get(key, "") or ""
    if key in NUMERIC_KEYS and key not in ("volume", "rr_multiple"):
        try:
            return f"{float(raw):.2f}"
        except (TypeError, ValueError):
            return raw
    if key == "logged_at":
        return raw.replace("T", " ")
    if key == "reason":
        return raw or (row.get("mt5_message", "") or "")
    return raw


def _row_key(row: Dict[str, str]) -> Tuple[str, ...]:
    return (
        row.get("_journal", ""),
        row.get("logged_at", ""),
        row.get("event", ""),
        row.get("signal_bar_time", ""),
    )


class _SortItem(QTableWidgetItem):
    """Numeric-aware sorting without changing the displayed text."""

    def __init__(self, text: str, sort_value=None):
        super().__init__(text)
        self._sort_value = sort_value
        self.setFlags(self.flags() & ~Qt.ItemIsEditable)

    def __lt__(self, other):
        a = self._sort_value
        b = getattr(other, "_sort_value", None)
        if isinstance(a, float) and isinstance(b, float):
            return a < b
        if isinstance(a, float) != isinstance(b, float):
            return isinstance(a, float)
        return self.text() < other.text()


def _info_icon(tip: str, label: str) -> QWidget:
    """Same ℹ control as the dashboard (objectName infoHintButton)."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lbl = QLabel(label)
    lbl.setObjectName("sectionHint")
    lay.addWidget(lbl)
    btn = QToolButton()
    btn.setObjectName("infoHintButton")
    btn.setText("ℹ")
    btn.setAutoRaise(True)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setToolTip(tip)
    btn.setFixedSize(22, 22)
    btn.setToolTipDuration(60000)
    btn.clicked.connect(
        lambda _c=False, b=btn: QToolTip.showText(b.mapToGlobal(b.rect().bottomLeft()), b.toolTip(), b)
    )
    lay.addWidget(btn)
    lay.addStretch(1)
    return row


def _secondary(text: str, tip: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("secondaryButton")
    if tip:
        btn.setToolTip(tip)
    return btn


class LiveHistoryDialog(QDialog):
    def __init__(
        self,
        parent,
        app_root: str,
        *,
        open_logs: Optional[Callable[[], None]] = None,
        open_folder: Optional[Callable[[str], None]] = None,
        export_dir: str = "",
        settings=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("History — Live trades")
        self.setWindowFlag(Qt.Window, True)
        self.setModal(False)
        self.resize(1320, 820)
        self._store = live_history.HistoryStore(app_root)
        self._open_folder = open_folder
        self._export_dir = export_dir or app_root
        self._settings = settings
        self._all_rows: List[Dict[str, str]] = []
        self._shown: List[Dict[str, str]] = []
        self._loaded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        root.addWidget(_info_icon(
            "Every Live event for every account and slot is listed here automatically:\n"
            "signals, dry runs, orders, exits, errors, safety blocks and skipped setups.\n\n"
            "Select a row to see how entry, SL (candle low/high ± buffer) and TP were built\n"
            "from that slot's pattern and preset, and what was sent to MT5.\n"
            "SL check = OK when MT5 got exactly the strategy SL.\n\n"
            "Show → Problems lists only errors, blocks and moved SLs.\n"
            "Export to Excel saves what is listed (Trades / Errors / All events sheets).\n"
            "Double-click a row to open that slot's log folder.",
            "How Live History works",
        ))

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(QLabel("Search"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by symbol, preset, reason, ticket…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(220)
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(QLabel("Account"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(140)
        filter_row.addWidget(self.account_combo)
        filter_row.addWidget(QLabel("Pattern"))
        self.pattern_combo = QComboBox()
        self.pattern_combo.setMinimumWidth(160)
        filter_row.addWidget(self.pattern_combo)
        filter_row.addWidget(QLabel("Show"))
        self.show_combo = QComboBox()
        for label, _cat in SHOW_FILTERS:
            self.show_combo.addItem(label)
        filter_row.addWidget(self.show_combo)
        columns_btn = _secondary("Columns…", "Choose which columns appear in the Live History table.")
        columns_btn.clicked.connect(self._on_columns_clicked)
        filter_row.addWidget(columns_btn)
        self.count_label = QLabel("")
        self.count_label.setObjectName("sectionHint")
        filter_row.addWidget(self.count_label)
        root.addLayout(filter_row)

        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        if sys.platform == "darwin":
            self.table.setAttribute(Qt.WA_MacShowFocusRect, False)
            self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemDoubleClicked.connect(self._on_double_click)

        detail_host = QWidget()
        detail_box = QVBoxLayout(detail_host)
        detail_box.setContentsMargins(0, 0, 0, 0)
        detail_box.setSpacing(4)
        detail_head = QHBoxLayout()
        detail_title = QLabel("Trade detail")
        detail_title.setObjectName("sectionHint")
        detail_head.addWidget(detail_title)
        detail_head.addStretch(1)
        copy_btn = _secondary("Copy detail", "Copy this breakdown to share it.")
        copy_btn.clicked.connect(self._copy_detail)
        detail_head.addWidget(copy_btn)
        detail_box.addLayout(detail_head)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.detail.setFont(mono)
        self.detail.setPlaceholderText("Select a row to see entry / SL + buffer / TP for that trade.")
        detail_box.addWidget(self.detail, 1)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(self.table)
        split.addWidget(detail_host)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        button_row = QHBoxLayout()
        refresh_btn = _secondary("Refresh", "Re-read all Live journals now.")
        refresh_btn.clicked.connect(lambda: self.reload(force=True))
        export_btn = _secondary(
            "Export to Excel",
            "Save the listed rows as .xlsx (Trades / Errors / All events) to share or review.",
        )
        export_btn.clicked.connect(self.export_to_excel)
        button_row.addWidget(refresh_btn)
        button_row.addWidget(export_btn)
        if open_logs is not None:
            logs_btn = _secondary("Open logs folder", "Folder with every account/slot live_trades.csv.")
            logs_btn.clicked.connect(open_logs)
            button_row.addWidget(logs_btn)
        button_row.addStretch()
        self.auto_cb = QCheckBox("Auto-refresh")
        self.auto_cb.setChecked(True)
        self.auto_cb.setToolTip("Re-read the Live journals every few seconds while this window is open.")
        button_row.addWidget(self.auto_cb)
        root.addLayout(button_row)

        self.account_combo.currentIndexChanged.connect(lambda *_: self._apply_filters())
        self.pattern_combo.currentIndexChanged.connect(lambda *_: self._apply_filters())
        self.show_combo.currentIndexChanged.connect(lambda *_: self._apply_filters())
        self.search_edit.textChanged.connect(lambda *_: self._apply_filters())

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

        self.reload()

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------
    def _visible_keys(self) -> List[str]:
        keys: List[str] = []
        if self._settings is not None:
            raw = self._settings.value(SETTINGS_COLUMNS_KEY, "")
            if isinstance(raw, str) and raw:
                keys = [k for k in raw.split(",") if k in HEADER_BY_KEY]
            elif isinstance(raw, (list, tuple)):
                keys = [str(k) for k in raw if str(k) in HEADER_BY_KEY]
        keys = keys or list(DEFAULT_VISIBLE)
        return [k for k, _h, _v in COLUMN_DEFS if k in keys]

    def _save_visible_keys(self, keys: List[str]) -> None:
        if self._settings is not None:
            self._settings.setValue(SETTINGS_COLUMNS_KEY, ",".join(keys))

    def _on_columns_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Live History columns")
        dlg.setMinimumWidth(360)
        layout = QVBoxLayout(dlg)
        tip = QLabel("Check columns to show in History → Live trades. Time stays on.")
        tip.setWordWrap(True)
        tip.setObjectName("sectionHint")
        layout.addWidget(tip)
        list_w = QListWidget()
        visible = set(self._visible_keys())
        for key, header, _default in COLUMN_DEFS:
            item = QListWidgetItem(header)
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if key in visible else Qt.Unchecked)
            if key == "logged_at":
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked)
            list_w.addItem(item)
        layout.addWidget(list_w, 1)
        btn_row = QHBoxLayout()
        reset_btn = _secondary("Reset defaults")

        def _reset():
            for i in range(list_w.count()):
                it = list_w.item(i)
                k = it.data(Qt.UserRole)
                it.setCheckState(Qt.Checked if k in DEFAULT_VISIBLE else Qt.Unchecked)

        reset_btn.clicked.connect(_reset)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        keys = [
            str(list_w.item(i).data(Qt.UserRole))
            for i in range(list_w.count())
            if list_w.item(i).checkState() == Qt.Checked
        ]
        self._save_visible_keys(keys)
        self._fill_table()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _on_timer(self) -> None:
        if self.isVisible() and self.auto_cb.isChecked():
            self.reload()

    def reload(self, force: bool = False) -> None:
        rows = self._store.load()
        unchanged = (
            not force
            and self._loaded
            and len(rows) == len(self._all_rows)
            and [_row_key(r) for r in rows[:1]] == [_row_key(r) for r in self._all_rows[:1]]
        )
        if unchanged:
            return
        self._loaded = True
        self._all_rows = rows
        self._refill_combo(self.account_combo, "All accounts", sorted({r.get("account", "") for r in rows} - {""}))
        self._refill_combo(self.pattern_combo, "All patterns", sorted({r.get("pattern", "") for r in rows} - {""}))
        self._apply_filters()

    @staticmethod
    def _refill_combo(combo: QComboBox, all_label: str, values: List[str]) -> None:
        current = combo.currentText()
        items = [all_label] + values
        if [combo.itemText(i) for i in range(combo.count())] == items:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        idx = combo.findText(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def filters_text(self) -> str:
        bits = []
        if self.account_combo.currentIndex() > 0:
            bits.append(f"account={self.account_combo.currentText()}")
        if self.pattern_combo.currentIndex() > 0:
            bits.append(f"pattern={self.pattern_combo.currentText()}")
        if self.show_combo.currentIndex() > 0:
            bits.append(f"show={self.show_combo.currentText()}")
        if self.search_edit.text().strip():
            bits.append(f"search='{self.search_edit.text().strip()}'")
        return ", ".join(bits)

    def _filtered(self) -> List[Dict[str, str]]:
        acc = self.account_combo.currentText() if self.account_combo.currentIndex() > 0 else ""
        pat = self.pattern_combo.currentText() if self.pattern_combo.currentIndex() > 0 else ""
        cat = SHOW_FILTERS[max(0, self.show_combo.currentIndex())][1]
        needle = self.search_edit.text().strip().lower()
        out = []
        for r in self._all_rows:
            if acc and r.get("account", "") != acc:
                continue
            if pat and r.get("pattern", "") != pat:
                continue
            if cat == "problem":
                if not row_has_problem(r):
                    continue
            elif cat and live_history.event_category(r.get("event", "")) != cat:
                continue
            if needle and needle not in " ".join(
                str(v) for k, v in r.items() if not k.startswith("_")
            ).lower():
                continue
            out.append(r)
            if len(out) >= MAX_ROWS:
                break
        return out

    def _apply_filters(self) -> None:
        cur = self._current_row()
        selected_key = _row_key(cur) if cur is not None else None
        self._shown = self._filtered()
        self._fill_table()
        self._update_count()
        if selected_key is not None:
            for i in range(self.table.rowCount()):
                r = self._row_at(i)
                if r is not None and _row_key(r) == selected_key:
                    self.table.selectRow(i)
                    return
        if not self._shown:
            self.detail.clear()

    def _fill_table(self) -> None:
        keys = self._visible_keys()
        table = self.table
        was_sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        table.clearContents()
        table.setColumnCount(len(keys))
        table.setHorizontalHeaderLabels([HEADER_BY_KEY[k] for k in keys])
        table.setRowCount(len(self._shown))
        for i, row in enumerate(self._shown):
            cat = live_history.event_category(row.get("event", ""))
            problem = row_has_problem(row)
            for j, key in enumerate(keys):
                text = _cell_text(row, key)
                sort_value = None
                if key in NUMERIC_KEYS:
                    try:
                        sort_value = float(row.get(key) or "")
                    except (TypeError, ValueError):
                        sort_value = None
                item = _SortItem(text, sort_value)
                if j == 0:
                    item.setData(Qt.UserRole, i)
                if problem:
                    item.setBackground(QBrush(PROBLEM_BG))
                elif cat == "skip":
                    item.setForeground(QBrush(SKIP_FG))
                if key == "event" and cat == "error":
                    item.setForeground(QBrush(LOSS_FG))
                elif key == "event" and (row.get("event") or "").upper() == "ORDER":
                    item.setForeground(QBrush(WIN_FG))
                elif key == "direction":
                    side = (row.get("direction") or "").upper()
                    if side == "BUY":
                        item.setForeground(QBrush(WIN_FG))
                    elif side == "SELL":
                        item.setForeground(QBrush(LOSS_FG))
                elif key == "_sl_check" and text.startswith("MOVED"):
                    item.setForeground(QBrush(LOSS_FG))
                elif key == "profit" and sort_value is not None:
                    item.setForeground(QBrush(WIN_FG if sort_value >= 0 else LOSS_FG))
                table.setItem(i, j, item)
        table.blockSignals(False)
        table.setUpdatesEnabled(True)
        table.resizeColumnsToContents()
        table.setSortingEnabled(was_sorting)

    def _update_count(self) -> None:
        rows = self._shown
        exits = [r for r in rows if (r.get("event") or "").upper() == "EXIT"]
        pnl = 0.0
        for r in exits:
            try:
                pnl += float(r.get("profit") or 0)
            except (TypeError, ValueError):
                pass
        orders = sum(1 for r in rows if (r.get("event") or "").upper() == "ORDER")
        problems = sum(1 for r in rows if row_has_problem(r))
        more = f" (newest {MAX_ROWS})" if len(rows) >= MAX_ROWS else ""
        self.count_label.setText(
            f"{len(rows)} of {len(self._all_rows)} events{more} · {orders} orders · "
            f"{len(exits)} exits P/L {pnl:+.2f} · {problems} problems"
        )

    def _row_at(self, table_row: int) -> Optional[Dict[str, str]]:
        item = self.table.item(table_row, 0)
        if item is None:
            return None
        idx = item.data(Qt.UserRole)
        if isinstance(idx, int) and 0 <= idx < len(self._shown):
            return self._shown[idx]
        return None

    def _current_row(self) -> Optional[Dict[str, str]]:
        model = self.table.selectionModel()
        sel = model.selectedRows() if model is not None else []
        if not sel:
            return None
        return self._row_at(sel[0].row())

    def _on_selection(self) -> None:
        row = self._current_row()
        if row is None:
            return
        timeline = live_history.related_rows(self._all_rows, row)
        self.detail.setPlainText(live_history.format_trade_detail(row, timeline))

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        row = self._row_at(item.row())
        if row is None or self._open_folder is None:
            return
        folder = os.path.dirname(row.get("_journal", "") or "")
        if folder and os.path.isdir(folder):
            self._open_folder(folder)

    def _copy_detail(self) -> None:
        QGuiApplication.clipboard().setText(self.detail.toPlainText())

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_to_excel(self) -> None:
        if not self._shown:
            QMessageBox.information(self, "Nothing to export", "No Live events match the current filters.")
            return
        os.makedirs(self._export_dir, exist_ok=True)
        default = os.path.join(
            self._export_dir, f"live_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        path, _flt = QFileDialog.getSaveFileName(self, "Export Live history to Excel", default, "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            counts = live_history.export_history_xlsx(path, self._shown, filters_text=self.filters_text())
        except ImportError:
            QMessageBox.critical(
                self, "Missing Package",
                "Exporting to Excel needs the 'openpyxl' package.\n\nInstall it with:\npip install openpyxl",
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export Live history:\n{e}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Export Complete")
        box.setText(
            f"Live history saved to:\n{path}\n\n"
            f"Trades: {counts.get('Trades', 0)} · Errors: {counts.get('Errors', 0)} · "
            f"All events: {counts.get('All_Events', 0)}"
        )
        open_btn = box.addButton("Open folder", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is open_btn and self._open_folder is not None:
            self._open_folder(os.path.dirname(path))
