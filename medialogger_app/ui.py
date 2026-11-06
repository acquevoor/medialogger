"""The window. SQLite stays on the UI thread; workers only fetch and parse, then hand plain rows back."""
import hashlib
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from . import imports, sources, store

log = logging.getLogger("medialogger")
ACTIVE = {"game": "Playing", "anime": "Watching", "manga": "Reading", "music": "Listening", "film": "Watching", "novel": "Reading", "vn": "Reading"}
DONE = {"game": "Played", "anime": "Watched", "manga": "Read", "music": "Listened", "film": "Watched", "novel": "Read", "vn": "Read"}
SITE_NAME = {"game": "Steam + Wikidata", "anime": "AniList", "manga": "AniList", "music": "MusicBrainz", "film": "Wikidata", "novel": "RanobeDB", "vn": "VNDB"}
MANUAL_SHARED = ("film", "music")  # hand-added ones share keys with exports, so a later import lands on the same row


def status_label(kind, status):
    return {"active": ACTIVE[kind], "done": DONE[kind], "planned": "Planned", "paused": "Paused", "dropped": "Dropped"}[status]


# ---------- worker threads -> UI thread ----------

class Bridge(QObject):
    done = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(lambda cb, res, err: cb(res, err))


POOL = ThreadPoolExecutor(4)
bridge = None  # set by main once the QApplication exists


def run(fn, cb):
    """Run fn() in a worker, then cb(result, error) on the UI thread."""
    def job():
        try:
            res, err = fn(), None
        except Exception as e:
            res, err = None, e
            log.warning("background task failed: %s: %s", type(e).__name__, e)
        bridge.done.emit(cb, res, err)
    POOL.submit(job)


def show_error(parent, title, err):
    # plain text: errors quote files and site replies, and QMessageBox would render them as HTML
    box = QMessageBox(QMessageBox.Warning, title, str(err), QMessageBox.Ok, parent)
    box.setTextFormat(Qt.PlainText)
    box.exec()


def confirm(parent, title, text, yes="OK"):
    box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Cancel, parent)
    box.setTextFormat(Qt.PlainText)
    ok = box.addButton(yes, QMessageBox.AcceptRole)
    box.exec()
    return box.clickedButton() is ok


def open_link(url):
    if url.startswith("https://"):
        QDesktopServices.openUrl(QUrl(url))


def cover_file(url):
    return store.cache_dir() / "covers" / hashlib.sha1(url.encode()).hexdigest()


def load_cover(url):
    path = cover_file(url)
    if not path.exists():
        data = sources.fetch_cover(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path


# ---------- dialogs ----------

class LogDialog(QDialog):
    """Add or edit one entry. `row` carries everything the form doesn't show (site, key, url, cover)."""

    def __init__(self, parent, row):
        super().__init__(parent)
        self.row, kind = row, row["kind"]
        self.setWindowTitle(f"Log: {KIND_ONE[kind]}")
        self.resize(460, 480)
        form = QFormLayout(self)
        self.title_ = QLineEdit(row.get("title", ""))
        self.creator = QLineEdit(row.get("creator", ""))
        self.year = QSpinBox(minimum=999, maximum=3000, specialValueText="—", value=row.get("year") or 999)
        self.status = QComboBox()
        for s in store.STATUSES:
            self.status.addItem(status_label(kind, s), s)
        self.status.setCurrentIndex(store.STATUSES.index(row.get("status", "planned")))
        self.rating = QDoubleSpinBox(minimum=0, maximum=10, singleStep=0.5, decimals=1, specialValueText="—", value=row.get("rating") or 0)
        self.progress = QSpinBox(minimum=0, maximum=10_000_000, specialValueText="—", value=row.get("progress") or 0)
        self.started = QLineEdit(row.get("started", ""), placeholderText="YYYY-MM-DD")
        self.finished = QLineEdit(row.get("finished", ""), placeholderText="YYYY-MM-DD")
        self.notes = QPlainTextEdit(row.get("notes", ""))
        for label, w in (("Title", self.title_), ("Creator", self.creator), ("Year", self.year), ("Status", self.status), ("Rating (0–10)", self.rating),
                         ("Progress", self.progress), ("Started", self.started), ("Finished", self.finished), ("Notes", self.notes)):
            form.addRow(label, w)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self):
        row = dict(self.row)
        row.update(title=self.title_.text().strip(), creator=self.creator.text().strip(), year=self.year.value() if self.year.value() >= 1000 else None,
                   status=self.status.currentData(), rating=self.rating.value() or None, progress=self.progress.value() or None,
                   started=self.started.text().strip(), finished=self.finished.text().strip(), notes=self.notes.toPlainText())
        if not row.get("key"):  # a hand-made entry
            kind = row["kind"]
            row["site"] = store.SITE_OF[kind] if kind in MANUAL_SHARED else "manual"
            row["key"] = store.make_key(kind, row["title"], row["year"], row["creator"])
        try:
            self.saved = store.clean(row)
        except ValueError as e:
            show_error(self, "Can't save", e)
            return
        self.accept()


class ResultsDialog(QDialog):
    def __init__(self, parent, db, kind, rows):
        super().__init__(parent)
        self.setWindowTitle(f"{store.KINDS[kind]}: {len(rows)} found")
        self.resize(640, 460)
        self.rows, self.chosen = rows, None
        box = QVBoxLayout(self)
        self.list = QListWidget()
        for r in rows:
            mark = " ✓ logged" if store.find(db, r["site"], r["key"]) else ""
            text = f"{r['title']}" + (f" ({r['year']})" if r.get("year") else "") + (f" — {r['creator']}" if r.get("creator") else "") + mark
            self.list.addItem(QListWidgetItem(text))
        self.list.itemActivated.connect(self.pick)
        self.list.itemDoubleClicked.connect(self.pick)
        box.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Log selected", QDialogButtonBox.AcceptRole).clicked.connect(self.pick)
        buttons.addButton("Enter by hand…", QDialogButtonBox.ActionRole).clicked.connect(self.manual)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.list.setCurrentRow(0)

    def pick(self, *_):
        i = self.list.currentRow()
        if i >= 0:
            self.chosen = self.rows[i]
            self.accept()

    def manual(self):
        self.chosen = {}
        self.accept()


class SyncDialog(QDialog):
    """Pull by username or read a site export. Everything shows a preview before it touches the log."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Sync from sites")
        self.resize(540, 280)
        box = QVBoxLayout(self)
        form = QFormLayout()
        self.names = {}
        for site, label in (("anilist", "AniList user"), ("vndb", "VNDB user"), ("letterboxd", "Letterboxd user (recent watches)")):
            edit = QLineEdit(store.get_setting(main.db, site))
            pull = QPushButton("Pull")
            pull.clicked.connect(lambda _=False, s=site: self.pull(s))
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(pull)
            self.names[site] = edit
            form.addRow(label, row)
        box.addLayout(form)
        lb = QPushButton("Import Letterboxd export (.zip)…")
        lb.clicked.connect(lambda: self.file("Letterboxd export (*.zip)", imports.parse_letterboxd))
        rym = QPushButton("Import RateYourMusic export (.csv)…")
        rym.clicked.connect(lambda: self.file("RateYourMusic export (*.csv *.txt)", imports.parse_rym))
        box.addWidget(lb)
        box.addWidget(rym)
        note = QLabel("Backloggd and RanobeDB have no pull: log those from search.\nRows you saved by hand are never overwritten, and nothing is ever deleted.")
        note.setTextFormat(Qt.PlainText)
        box.addWidget(note)
        self.state = QLabel(textFormat=Qt.PlainText)
        box.addWidget(self.state)

    def busy(self, text):
        self.state.setText(text)
        for w in self.findChildren(QPushButton):
            w.setEnabled(not text)

    def pull(self, site):
        user = self.names[site].text().strip()
        if not user:
            return
        store.set_setting(self.main.db, site, user)
        self.busy(f"Pulling {user}…")
        run(lambda: sources.PULLS[site](user), self.preview)

    def file(self, pattern, parse):
        path, _ = QFileDialog.getOpenFileName(self, "Import export file", str(Path.home()), pattern)
        if path:
            self.busy("Reading…")
            run(lambda: parse(path), self.preview)

    def preview(self, rows, err):
        self.busy("")
        if err:
            show_error(self, "Sync failed", err)
            return
        try:
            d = store.diff(self.main.db, rows)
        except ValueError as e:
            show_error(self, "Sync failed", e)
            return
        if not d["new"] and not d["update"]:
            self.state.setText("Nothing new: " + store.summary(d))
        elif confirm(self, "Apply sync?", store.summary(d), "Apply"):
            store.apply(self.main.db, d)
            self.state.setText("Applied: " + store.summary(d))
            self.main.refresh()


# ---------- the tabs ----------

KIND_ONE = {"game": "game", "anime": "anime", "manga": "manga", "music": "album", "film": "film", "novel": "light novel", "vn": "visual novel"}
COLUMNS = ("Title", "Creator", "Year", "Status", "Rating", "Finished")


class Tab(QWidget):
    def __init__(self, main, kind):
        super().__init__()
        self.main, self.kind = main, kind
        self.rows, self.current, self.search_id = [], None, 0
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        if kind:
            self.search = QLineEdit(placeholderText=f"Search {SITE_NAME[kind]}, press Enter")
            self.search.returnPressed.connect(self.do_search)
            add = QPushButton("Add by hand…")
            add.clicked.connect(lambda: self.log_row({"kind": kind}))
            top.addWidget(self.search, 2)
            top.addWidget(add)
        self.filter = QLineEdit(placeholderText="Filter this log")
        self.filter.textChanged.connect(self.fill)
        self.status = QComboBox()
        self.status.addItem("All statuses", "")
        for s in store.STATUSES:
            self.status.addItem(status_label(kind, s) if kind else s.capitalize(), s)
        self.status.currentIndexChanged.connect(self.fill)
        top.addWidget(self.filter, 1)
        top.addWidget(self.status)
        box.addLayout(top)

        cols = COLUMNS if kind else ("Type", *COLUMNS)
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.title_col = cols.index("Title")
        self.table.horizontalHeader().setSectionResizeMode(self.title_col, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.selected)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())

        pane = QWidget()
        side = QVBoxLayout(pane)
        self.cover = QLabel(alignment=Qt.AlignCenter, minimumHeight=240)
        self.heading = QLabel(textFormat=Qt.PlainText, wordWrap=True)
        self.heading.setStyleSheet("font-weight: bold")
        self.meta = QLabel(textFormat=Qt.PlainText, wordWrap=True)
        self.notes = QPlainTextEdit(readOnly=True)
        side.addWidget(self.cover)
        side.addWidget(self.heading)
        side.addWidget(self.meta)
        side.addWidget(self.notes, 1)
        row = QHBoxLayout()
        self.edit_btn, self.link_btn, self.del_btn = QPushButton("Edit…"), QPushButton("Open site"), QPushButton("Delete")
        self.edit_btn.clicked.connect(self.edit)
        self.link_btn.clicked.connect(lambda: open_link(self.current["url"]))
        self.del_btn.clicked.connect(self.delete)
        for b in (self.edit_btn, self.link_btn, self.del_btn):
            row.addWidget(b)
        side.addLayout(row)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(pane)
        split.setSizes([760, 300])
        box.addWidget(split, 1)
        self.show_entry(None)

    def reload(self):
        self.rows = store.entries(self.main.db, self.kind)
        self.fill()

    def visible(self):
        needle, status = self.filter.text().casefold(), self.status.currentData()
        return [e for e in self.rows if (not status or e["status"] == status)
                and needle in f"{e['title']} {e['creator']} {e['notes']}".casefold()]

    def fill(self):
        """ponytail: rebuilds the whole table, fine into the tens of thousands; a model/view pair if it ever lags."""
        keep = self.current["id"] if self.current else None
        rows = self.visible()
        t = self.table
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.clearContents()
        t.setRowCount(len(rows))
        for y, e in enumerate(rows):
            cells = (e["title"], e["creator"], e["year"], status_label(e["kind"], e["status"]), e["rating"], e["finished"])
            if not self.kind:
                cells = (KIND_ONE[e["kind"]], *cells)
            for x, v in enumerate(cells):
                item = QTableWidgetItem()
                item.setData(Qt.DisplayRole, v if v is not None else "")  # numbers stay numbers, so they sort as numbers
                t.setItem(y, x, item)
            t.item(y, self.title_col).setData(Qt.UserRole, e["id"])
        t.setSortingEnabled(True)
        t.blockSignals(False)
        for y in range(t.rowCount()):
            if t.item(y, self.title_col).data(Qt.UserRole) == keep:
                t.selectRow(y)
                break
        else:
            self.show_entry(None)
        self.main.show_stats()

    def stats(self):
        rows = self.visible()
        rated = [e["rating"] for e in rows if e["rating"] is not None]
        mean = f", mean rating {sum(rated) / len(rated):.1f} of {len(rated)} rated" if rated else ""
        return f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'}{mean}"

    def selected(self):
        items = self.table.selectedItems()
        if items:
            entry_id = self.table.item(items[0].row(), self.title_col).data(Qt.UserRole)
            self.show_entry(next((e for e in self.rows if e["id"] == entry_id), None))
        else:
            self.show_entry(None)

    def show_entry(self, e):
        self.current = e
        for b in (self.edit_btn, self.del_btn):
            b.setEnabled(bool(e))
        self.link_btn.setEnabled(bool(e and e["url"]))
        self.cover.clear()
        if not e:
            self.heading.clear(), self.meta.clear(), self.notes.clear()
            return
        self.heading.setText(e["title"])
        bits = [e["creator"], str(e["year"] or ""), status_label(e["kind"], e["status"]), f"{e['rating']:g}/10" if e["rating"] is not None else "",
                f"progress {e['progress']}" if e["progress"] else "", f"from {e['started']}" if e["started"] else "", f"finished {e['finished']}" if e["finished"] else ""]
        self.meta.setText(" · ".join(b for b in bits if b))
        self.notes.setPlainText(e["notes"])
        if e["cover"]:
            url = e["cover"]
            run(lambda: load_cover(url), lambda path, err: self.got_cover(e["id"], path, err))

    def got_cover(self, entry_id, path, err):
        if not err and self.current and self.current["id"] == entry_id:
            self.cover.setPixmap(QPixmap(str(path)).scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def do_search(self):
        q = self.search.text().strip()
        if not q:
            return
        self.search_id += 1
        ticket = self.search_id
        self.main.statusBar().showMessage(f"Searching {SITE_NAME[self.kind]}…")
        kind = self.kind
        run(lambda: sources.search(kind, q), lambda rows, err: self.results(ticket, rows, err))

    def results(self, ticket, rows, err):
        if ticket != self.search_id:
            return  # a newer search replaced this one
        self.main.show_stats()
        if err:
            show_error(self, "Search failed", err)
            return
        dlg = ResultsDialog(self, self.main.db, self.kind, rows)
        if dlg.exec() and dlg.chosen is not None:
            r = dlg.chosen
            old = store.find(self.main.db, r["site"], r["key"]) if r else None
            self.log_row(old or {**r, "kind": self.kind})

    def log_row(self, row):
        dlg = LogDialog(self, row)
        if dlg.exec():
            store.save_entry(self.main.db, dlg.saved)
            self.reload()

    def edit(self):
        if self.current:
            self.log_row(self.current)

    def delete(self):
        if self.current and confirm(self, "Delete entry?", f"Remove “{self.current['title']}” from this log?", "Delete"):
            store.delete_entry(self.main.db, self.current["id"])
            self.current = None
            self.reload()


# ---------- main window ----------

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Logger")
        self.resize(1100, 720)
        self.conf = store.load_conf()
        names = store.list_profiles()
        self.profile = self.conf.get("profile") if self.conf.get("profile") in names else (names[0] if names else "default")
        self.db = store.open_db(self.profile)

        bar = QToolBar(movable=False)
        self.addToolBar(bar)
        self.combo = QComboBox(minimumWidth=170)
        self.combo.activated.connect(lambda: self.switch(self.combo.currentText()))
        bar.addWidget(QLabel(" Profile: "))
        bar.addWidget(self.combo)
        for text, fn in (("New", self.new_profile), ("Export…", self.export), ("Import…", self.import_)):
            act = QAction(text, self)
            act.triggered.connect(fn)
            bar.addAction(act)
        bar.addSeparator()
        sync = QAction("Sync from sites…", self)
        sync.triggered.connect(lambda: SyncDialog(self).exec())
        bar.addAction(sync)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        for kind, label in ((None, "All"), *store.KINDS.items()):
            self.tabs.addTab(Tab(self, kind), label)
        self.tabs.currentChanged.connect(self.refresh)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.tabs.currentWidget().filter.setFocus())
        self.load_profiles()
        self.refresh()

    def load_profiles(self):
        self.combo.clear()
        self.combo.addItems(store.list_profiles())
        self.combo.setCurrentText(self.profile)

    def refresh(self, *_):
        self.tabs.currentWidget().reload()

    def show_stats(self):
        tab = self.tabs.currentWidget()
        if tab is not None:
            self.statusBar().showMessage(f"{self.profile}: {tab.stats()}")

    def switch(self, name):
        self.db.close()
        self.profile, self.db = name, store.open_db(name)
        self.conf["profile"] = name
        store.save_conf(self.conf)
        self.load_profiles()
        for i in range(self.tabs.count()):
            self.tabs.widget(i).current = None
        self.refresh()

    def new_profile(self):
        name, ok = QInputDialog.getText(self, "New profile", "Name:")
        if ok and name.strip():
            name = store.safe_name(name)
            if name in store.list_profiles():
                show_error(self, "New profile", f"A profile named {name} already exists.")
            else:
                self.switch(name)

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", str(Path.home() / f"{self.profile}.medialogger.json"), "Profile (*.json)")
        if path:
            try:
                store.export_profile(self.db, self.profile, path)
                self.statusBar().showMessage(f"Exported {self.profile} to {path}")
            except OSError as e:
                show_error(self, "Export failed", e)

    def import_(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", str(Path.home()), "Profile (*.json)")
        if not path:
            return
        try:
            doc = store.read_profile(path)
        except (ValueError, OSError) as e:
            show_error(self, "Import failed", e)
            return
        box = QMessageBox(QMessageBox.Question, "Import profile", f"{doc['name'][:100]}: {len(doc['entries'])} entries.\n\nNeither choice removes anything; "
                          f"merging keeps the newest version of each entry.", QMessageBox.Cancel, self)
        box.setTextFormat(Qt.PlainText)
        new = box.addButton("New profile", QMessageBox.AcceptRole)
        merge = box.addButton(f"Merge into {self.profile}", QMessageBox.AcceptRole)
        box.setDefaultButton(new)
        box.exec()
        if box.clickedButton() is new:
            self.switch(store.import_new(doc))
        elif box.clickedButton() is merge:
            added, replaced, kept = store.merge_profile(self.db, doc)
            self.refresh()
            self.statusBar().showMessage(f"Merged: +{added} new, {replaced} replaced by newer, {kept} kept")

    def closeEvent(self, ev):
        POOL.shutdown(wait=False, cancel_futures=True)
        self.db.close()
        super().closeEvent(ev)


def main():
    global bridge
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("medialogger")
    app.setDesktopFileName("medialogger")
    bridge = Bridge()
    win = Main()
    win.show()
    sys.exit(app.exec())
