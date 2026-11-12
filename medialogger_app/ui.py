"""The window. SQLite stays on the UI thread; workers only fetch and parse, then hand plain rows back."""
import hashlib
import json
import logging
import os
import platform
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QMenu, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from . import imports, scan, sources, store

log = logging.getLogger("medialogger")
ACTIVE = {"game": "Playing", "anime": "Watching", "manga": "Reading", "music": "Listening", "film": "Watching", "novel": "Reading", "vn": "Reading", "character": "Following"}
DONE = {"game": "Played", "anime": "Watched", "manga": "Read", "music": "Listened", "film": "Watched", "novel": "Read", "vn": "Read", "character": "Favorite"}
SITE_NAME = {"game": "Steam + Wikidata", "anime": "AniList", "manga": "AniList", "music": "MusicBrainz", "film": "Wikidata", "novel": "RanobeDB", "vn": "VNDB", "character": "AniList + VNDB + Wikidata"}
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


def open_local(path):
    if path:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


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
        self.adult = QCheckBox("blur the cover", checked=store.adult(row))
        for label, w in (("Title", self.title_), ("Creator", self.creator), ("Year", self.year), ("Status", self.status), ("Rating (0–10)", self.rating),
                         ("Progress", self.progress), ("Started", self.started), ("Finished", self.finished), ("Adult", self.adult), ("Notes", self.notes)):
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
        # ticked: adult. Unticked: -1 only where something would still call it adult (a site flag, the title), so a
        # later title change can still blur an entry you never decided on
        row["nsfw"] = 1 if self.adult.isChecked() else -1 if store.adult({**row, "nsfw": 0}) or self.row.get("nsfw") == 1 else 0
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


TILE_W, TILE_H = 150, 225  # 2:3, the shape of a poster or box
COVER_ROLE, LINES_ROLE, BLUR_ROLE = Qt.UserRole + 1, Qt.UserRole + 2, Qt.UserRole + 3


def blur(pix):
    """Shrink to a few pixels and back: nothing recognisable survives, the colours do."""
    small = pix.scaled(max(1, pix.width() // 16), max(1, pix.height() // 16), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(pix.width(), pix.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def badge(p, box, text="18+"):
    """A dark pill in the middle of a cover."""
    f = QFont(p.font())
    f.setBold(True)
    p.setFont(f)
    w = QFontMetrics(f).horizontalAdvance(text) + 16
    pill = QRect(box.center().x() - w // 2, box.center().y() - 12, w, 24)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 170))
    p.drawRoundedRect(pill, 12, 12)
    p.setPen(QColor("white"))
    p.drawText(pill, Qt.AlignCenter, text)


class Tile(QStyledItemDelegate):
    """One search result: the cover (or a "no cover" card) and a caption that always has room for what tells same-titled
    things apart: the title on up to two lines, then year · creator, then the detail (platforms, format, release type)."""
    PAD, CAPTION = 8, 74
    reveal = False  # the dialog's "Show adult covers"

    def sizeHint(self, option, index):
        return QSize(TILE_W + 2 * self.PAD, TILE_H + self.CAPTION + 2 * self.PAD)

    def paint(self, p, option, index):
        p.save()
        r, pal = option.rect.adjusted(2, 2, -2, -2), option.palette
        if option.state & QStyle.State_Selected:
            p.fillRect(r, pal.highlight())
        fg = pal.highlightedText().color() if option.state & QStyle.State_Selected else pal.text().color()
        box = QRect(r.left() + (r.width() - TILE_W) // 2, r.top() + self.PAD, TILE_W, TILE_H)
        hide = index.data(BLUR_ROLE) is not None and not self.reveal
        pix = index.data(BLUR_ROLE) if hide and index.data(BLUR_ROLE) else index.data(COVER_ROLE)
        if pix:
            p.drawPixmap(box.left() + (TILE_W - pix.width()) // 2, box.top() + (TILE_H - pix.height()) // 2, pix)
        else:
            p.fillRect(box, pal.alternateBase())
            p.setPen(pal.placeholderText().color())
            p.drawText(box, Qt.AlignCenter, "no cover" if index.data(Qt.UserRole) == "none" else "…")
        if hide:
            p.save()
            badge(p, box)
            p.restore()
        if index.data(KIND_ROLE):  # the home grid: a strip in the kind's color
            p.fillRect(QRect(box.left(), box.top(), TILE_W, 5), QColor(index.data(KIND_ROLE)))
        title, meta, detail = index.data(LINES_ROLE)
        y, w = box.bottom() + 6, r.width() - 2 * self.PAD
        bold = QFont(option.font)
        bold.setBold(True)
        fm = QFontMetrics(bold)
        p.setFont(bold)
        p.setPen(fg)
        two = fm.elidedText(title, Qt.ElideRight, w * 2 - fm.averageCharWidth() * 3)  # wraps onto two lines at most
        p.drawText(QRect(r.left() + self.PAD, y, w, fm.height() * 2), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, two)
        y += fm.height() * 2
        fm = QFontMetrics(option.font)
        p.setFont(option.font)
        for line, dim in ((meta, False), (detail, True)):
            if dim:
                c = QColor(fg)
                c.setAlphaF(0.65)
                p.setPen(c)
            p.drawText(QRect(r.left() + self.PAD, y, w, fm.height()), Qt.AlignHCenter, fm.elidedText(line, Qt.ElideRight, w))
            y += fm.height()
        p.restore()


class ResultsDialog(QDialog):
    """Search results as a cover grid, like imagesearcher's: the cover, year, creator and the detail line (platforms,
    format, release type) are what tell same-titled things apart."""

    def __init__(self, parent, db, heading, rows):
        super().__init__(parent)
        self.setWindowTitle(f"{heading}: {len(rows)} found")
        mixed = len({r["kind"] for r in rows}) > 1
        self.resize(1040, 760)
        self.rows, self.chosen, self.alive = rows, None, True
        box = QVBoxLayout(self)
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setItemDelegate(Tile(self.grid))
        for r in rows:
            logged = store.find(db, r["site"], r["key"])
            title = ("✓ " if logged else "") + r["title"]
            meta = " · ".join(str(x) for x in (r.get("year"), r.get("creator")) if x)
            item = QListWidgetItem()
            item.setData(LINES_ROLE, (title, meta, r.get("detail", "")))
            item.setData(Qt.UserRole, "wait" if r.get("cover") else "none")
            if mixed:
                item.setData(KIND_ROLE, KIND_COLOR[r["kind"]])
            if store.adult(r):
                item.setData(BLUR_ROLE, False)  # blurred version comes with the cover; False: nothing to show yet
            item.setToolTip("\n".join(x for x in (r["title"], store.KINDS[r["kind"]], meta, r.get("detail", "")) if x) + ("\n\nAlready in your log" if logged else ""))
            self.grid.addItem(item)
            if r.get("cover"):
                url = r["cover"]
                run(lambda url=url: load_cover(url), lambda path, err, item=item: self.got_cover(item, path, err))
        self.grid.itemActivated.connect(self.pick)
        box.addWidget(self.grid)
        hint = QLabel("Double-click a cover to log it.", textFormat=Qt.PlainText)
        self.show_adult = QCheckBox(f"Show adult covers ({sum(map(store.adult, rows))})")
        self.show_adult.setVisible(any(map(store.adult, rows)))
        self.show_adult.toggled.connect(self.reveal)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Log selected", QDialogButtonBox.AcceptRole).clicked.connect(self.pick)
        buttons.addButton("Enter by hand…", QDialogButtonBox.ActionRole).clicked.connect(self.manual)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(hint, 1)
        row.addWidget(self.show_adult)
        row.addWidget(buttons)
        box.addLayout(row)
        self.grid.setCurrentRow(0)

    def got_cover(self, item, path, err):
        if not self.alive:  # covers keep arriving after the dialog closes
            return
        pix = None if err else QPixmap(str(path))
        if pix and not pix.isNull():
            pix = pix.scaled(TILE_W, TILE_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if item.data(BLUR_ROLE) is not None:
                item.setData(BLUR_ROLE, blur(pix))  # before the cover itself: never a frame of it unblurred
            item.setData(COVER_ROLE, pix)
        else:
            item.setData(Qt.UserRole, "none")  # a 404 or an unreadable file: say so instead of loading forever

    def reveal(self, on):
        self.grid.itemDelegate().reveal = on
        self.grid.viewport().update()

    def done(self, code):
        self.alive = False
        super().done(code)

    def pick(self, *_):
        i = self.grid.currentRow()
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


class ScanDialog(QDialog):
    """What you already have: Steam (exact ids and playtime), qBittorrent's torrents, or any folder (one work per
    top-level entry). Torrents and folders are guesses from file types and names: fix the title or kind, Find matches
    looks each up on its site, then Add checked goes through the same preview as a sync."""
    COLS = ("", "Found", "Title", "Kind", "Match")

    def __init__(self, main):
        super().__init__(main)
        self.main, self.rows, self.alive, self.matching = main, [], True, False
        self.setWindowTitle("Scan for media")
        self.resize(1100, 680)
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        for text, fn in (("Steam library", lambda: self.scan("Steam", scan.scan_steam)), ("qBittorrent", lambda: self.scan("qBittorrent", scan.scan_qbittorrent)),
                         ("Choose a folder…", self.pick_folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch(1)
        box.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        head = self.table.horizontalHeader()
        for col, mode in ((0, QHeaderView.ResizeToContents), (1, QHeaderView.Stretch), (2, QHeaderView.Stretch), (3, QHeaderView.ResizeToContents), (4, QHeaderView.Stretch)):
            head.setSectionResizeMode(col, mode)
        self.table.cellChanged.connect(self.edited)
        box.addWidget(self.table, 1)
        self.state = QLabel("Pick what to scan. Names go to the sites only when you press Find matches.", textFormat=Qt.PlainText)
        box.addWidget(self.state)
        bottom = QHBoxLayout()
        self.match_btn, self.stop_btn, self.add_btn = QPushButton("Find matches"), QPushButton("Stop"), QPushButton("Add checked…")
        self.match_btn.clicked.connect(self.find_matches)
        self.stop_btn.clicked.connect(lambda: setattr(self, "matching", False))
        self.add_btn.clicked.connect(self.add)
        self.status = QComboBox()
        for st in store.STATUSES:
            self.status.addItem(st.capitalize(), st)
        bottom.addWidget(self.match_btn)
        bottom.addWidget(self.stop_btn)
        bottom.addStretch(1)
        bottom.addWidget(QLabel("Add as (Steam keeps its playtime status):"))
        bottom.addWidget(self.status)
        bottom.addWidget(self.add_btn)
        box.addLayout(bottom)
        self.buttons()

    def buttons(self):
        for b in self.findChildren(QPushButton):
            b.setEnabled(not self.matching)
        self.stop_btn.setEnabled(self.matching)
        self.match_btn.setEnabled(not self.matching and any(not r.get("match") for r in self.rows))
        self.add_btn.setEnabled(not self.matching and bool(self.rows))

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder to scan", str(Path.home()))
        if folder:
            self.scan(folder, lambda: scan.scan_folder(folder))

    def scan(self, what, fn):
        self.state.setText(f"Scanning {what}…")
        run(fn, lambda rows, err: self.scanned(what, rows, err))

    def scanned(self, what, rows, err):
        if not self.alive:
            return
        if err:
            show_error(self, "Scan failed", err)
            self.state.setText("")
            return
        self.rows = rows
        self.fill()
        self.state.setText(f"{what}: {len(rows)} found." + ("" if not rows or rows[0].get("steam") else " Check the titles, then Find matches."))
        self.buttons()

    def match_text(self, r):
        m = r.get("match")
        if r.get("steam"):
            return f"Steam · {m['detail']}"
        if not m:
            return "no match yet" if "match" not in r else "nothing found: added as you typed it"
        return ("" if r.get("exact") else "closest, check it: ") + m["title"] + (f" ({m['year']})" if m.get("year") else "") + (f" · {m['creator']}" if m.get("creator") else "")

    def fill(self):
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.rows))
        for y, r in enumerate(self.rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if r.get("steam") or r.get("exact") else Qt.Unchecked)
            t.setItem(y, 0, check)
            found = QTableWidgetItem(r["found"] + (f"  ×{r['count']}" if r.get("count", 1) > 1 else ""))
            found.setFlags(Qt.ItemIsEnabled)
            found.setToolTip(r["found"])
            t.setItem(y, 1, found)
            title = QTableWidgetItem(r["title"])
            if r.get("steam"):
                title.setFlags(Qt.ItemIsEnabled)
            t.setItem(y, 2, title)
            kind = QComboBox()
            for k, label in store.KINDS.items():
                kind.addItem(label, k)
            kind.setCurrentIndex(list(store.KINDS).index(r["kind"]))
            kind.setEnabled(not r.get("steam"))
            kind.currentIndexChanged.connect(lambda _, y=y, w=kind: self.rekind(y, w.currentData()))
            t.setCellWidget(y, 3, kind)
            match = QTableWidgetItem(self.match_text(r))
            match.setFlags(Qt.ItemIsEnabled)
            t.setItem(y, 4, match)
        t.blockSignals(False)

    def unmatch(self, y):
        r = self.rows[y]
        r.pop("match", None)
        r.pop("exact", None)
        self.table.blockSignals(True)
        self.table.item(y, 4).setText(self.match_text(r))
        self.table.item(y, 0).setCheckState(Qt.Unchecked)
        self.table.blockSignals(False)
        self.buttons()

    def edited(self, y, x):
        if x == 2 and not self.rows[y].get("steam"):
            self.rows[y]["title"] = self.table.item(y, 2).text().strip()
            self.unmatch(y)

    def rekind(self, y, kind):
        self.rows[y]["kind"] = kind
        self.unmatch(y)

    def find_matches(self):
        todo = [y for y, r in enumerate(self.rows) if "match" not in r]
        self.matching = True
        self.buttons()
        self.next_match(todo, 0)

    def next_match(self, todo, done):
        if not self.alive:
            return
        if not self.matching or done == len(todo):
            self.matching = False
            self.state.setText(f"Matched {done} of {len(todo)}. Exact matches are checked; check the closest ones yourself.")
            self.buttons()
            return
        y = todo[done]
        r = dict(self.rows[y])
        self.state.setText(f"Matching {done + 1} of {len(todo)}: {r['title']}")
        run(lambda: scan.find_match(r), lambda res, err: self.matched(todo, done, y, r, res, err))

    def matched(self, todo, done, y, asked, res, err):
        if not self.alive:
            return
        r = self.rows[y]
        if (r["title"], r["kind"]) == (asked["title"], asked["kind"]):  # not edited while the search ran
            if err:
                r.pop("match", None)  # a failed search stays "no match yet", so the next Find matches retries it
                r["exact"] = False
            else:
                r["match"], r["exact"] = res
            self.table.blockSignals(True)
            self.table.item(y, 4).setText(self.match_text(r) if not err else f"search failed: {err}")
            if r["exact"]:
                self.table.item(y, 0).setCheckState(Qt.Checked)
            self.table.blockSignals(False)
        self.next_match(todo, done + 1)

    def picked(self):
        """Checked rows -> log rows: the site's own entry when matched, else the title as typed."""
        out, status = [], self.status.currentData()
        for y, r in enumerate(self.rows):
            if self.table.item(y, 0).checkState() != Qt.Checked:
                continue
            m = r.get("match")
            if m:
                out.append({**m, "status": m.get("status", status) if r.get("steam") else status, "path": r.get("path", "")})
            elif r["title"]:
                kind = r["kind"]
                out.append({"kind": kind, "site": store.SITE_OF[kind] if kind in MANUAL_SHARED else "manual", "key": store.make_key(kind, r["title"], r.get("year"), r.get("creator", "")),
                            "title": r["title"], "year": r.get("year"), "creator": r.get("creator", ""), "status": status, "path": r.get("path", "")})
        return out

    def add(self):
        rows = self.picked()
        if not rows:
            self.state.setText("Nothing checked.")
            return
        try:
            d = store.diff(self.main.db, rows)
        except ValueError as e:
            show_error(self, "Can't add", e)
            return
        located = [r for r in rows if r["path"]]
        if not d["new"] and not d["update"]:
            self.save_paths(located)
            self.state.setText(f"Nothing new: {store.summary(d)}. Locations saved for {len(located)}.")
            self.main.refresh()
        elif confirm(self, "Add to your log?", store.summary(d), "Add"):
            store.apply(self.main.db, d)
            self.save_paths(located)
            self.state.setText("Added: " + store.summary(d))
            self.main.refresh()

    def save_paths(self, rows):
        for r in rows:
            store.set_path(self.main.db, r["site"], r["key"], r["path"])

    def done(self, code):
        self.alive = self.matching = False
        super().done(code)


# ---------- the home page ----------

KIND_ONE = {"game": "game", "anime": "anime", "manga": "manga", "music": "album", "film": "film", "novel": "light novel", "vn": "visual novel", "character": "character"}
KIND_COLOR = {"game": "#2e9e5b", "anime": "#3b7dd8", "manga": "#8e5bd0", "music": "#e08a1e", "film": "#d64545", "novel": "#1f9e9e", "vn": "#d44d9a", "character": "#b8860b"}
COLUMNS = ("Type", "Title", "Creator", "Year", "Status", "Rating", "Finished")
ID_ROLE, KIND_ROLE = Qt.UserRole + 4, Qt.UserRole + 5


def chip_style(color):
    return (f"QPushButton {{ border: 2px solid {color}; border-radius: 13px; padding: 4px 12px; }}"
            f"QPushButton:checked {{ background: {color}; color: white; font-weight: bold; }}")


class Home(QWidget):
    """Every kind on one page: the colored chips pick which kinds show, the List/Grid buttons how."""

    def __init__(self, main):
        super().__init__()
        self.main, conf = main, main.conf
        self.rows, self.current, self.search_id = [], None, 0
        self.revealed, self.cover_pix = set(), None  # entries whose adult cover you clicked open, this session
        self.pix, self.tiles = {}, {}  # cover url -> scaled pixmap (None: failed); entry id -> grid item
        box = QVBoxLayout(self)

        chips = QHBoxLayout()
        self.chips = {}
        shown = set(store.KINDS) - set(conf.get("hidden") or ())
        for kind, label in store.KINDS.items():
            b = QPushButton(label, checkable=True, checked=kind in shown)
            b.setStyleSheet(chip_style(KIND_COLOR[kind]))
            b.toggled.connect(self.kinds_changed)
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _, k=kind: self.only(k))
            b.setToolTip(f"Show or hide {label.lower()}. Right-click: only {label.lower()}.")
            self.chips[kind] = b
            chips.addWidget(b)
        every = QPushButton("All")
        every.clicked.connect(lambda: self.only(None))
        chips.addWidget(every)
        chips.addStretch(1)
        self.list_btn, self.grid_btn = QPushButton("☰ List", checkable=True), QPushButton("▦ Grid", checkable=True)
        views = QButtonGroup(self)
        for b in (self.list_btn, self.grid_btn):
            views.addButton(b)
            chips.addWidget(b)
        (self.grid_btn if conf.get("view") == "grid" else self.list_btn).setChecked(True)
        views.buttonToggled.connect(lambda b, on: on and self.view_changed())
        box.addLayout(chips)

        top = QHBoxLayout()
        self.search_kind = QComboBox()
        self.search_kind.addItem("Shown kinds", None)  # every toggled kind's site at once
        for kind, label in store.KINDS.items():
            self.search_kind.addItem(label, kind)
        self.search_kind.currentIndexChanged.connect(self.search_kind_changed)
        self.search = QLineEdit()
        self.search.returnPressed.connect(self.do_search)
        add = QPushButton("Add by hand…")
        add.clicked.connect(self.add_by_hand)
        self.filter = QLineEdit(placeholderText="Filter your log")
        self.filter.textChanged.connect(self.fill)
        self.status = QComboBox()
        self.status.addItem("All statuses", "")
        for s in store.STATUSES:
            self.status.addItem(s.capitalize(), s)
        self.status.currentIndexChanged.connect(self.fill)
        self.show_adult = QCheckBox("Show adult covers")
        self.show_adult.toggled.connect(self.reveal_all)
        top.addWidget(self.search_kind)
        top.addWidget(self.search, 2)
        top.addWidget(add)
        top.addWidget(self.filter, 1)
        top.addWidget(self.status)
        top.addWidget(self.show_adult)
        box.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.title_col = COLUMNS.index("Title")
        self.table.horizontalHeader().setSectionResizeMode(self.title_col, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.selected)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setItemDelegate(Tile(self.grid))
        self.grid.itemSelectionChanged.connect(self.selected)
        self.grid.itemActivated.connect(lambda *_: self.edit())
        self.grid.itemClicked.connect(lambda *_: self.actions())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda *_: self.actions())
        self.views = QStackedWidget()
        self.views.addWidget(self.table)
        self.views.addWidget(self.grid)

        pane = QWidget()
        side = QVBoxLayout(pane)
        self.cover = QLabel(alignment=Qt.AlignCenter, minimumHeight=240)
        self.cover.mousePressEvent = lambda _: self.reveal_cover()
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
        split.addWidget(self.views)
        split.addWidget(pane)
        split.setSizes([800, 300])
        box.addWidget(split, 1)
        self.views.setCurrentIndex(self.grid_btn.isChecked())
        self.match_search_kind()
        self.search_kind_changed()
        self.show_entry(None)

    # ----- what is shown -----

    def kinds(self):
        return {k for k, b in self.chips.items() if b.isChecked()}

    def only(self, kind):
        for k, b in self.chips.items():
            b.blockSignals(True)
            b.setChecked(kind in (None, k))
            b.blockSignals(False)
        self.kinds_changed()

    def kinds_changed(self, *_):
        self.main.conf["hidden"] = sorted(set(store.KINDS) - self.kinds())
        store.save_conf(self.main.conf)
        self.match_search_kind()
        self.search_kind_changed()
        self.fill()

    def match_search_kind(self):
        kinds = self.kinds()
        self.search_kind.setCurrentIndex(self.search_kind.findData(kinds.pop()) if len(kinds) == 1 else 0)

    def search_kinds(self):
        kind = self.search_kind.currentData()
        return [kind] if kind else [k for k in store.KINDS if k in self.kinds()]

    def search_kind_changed(self, *_):
        kind = self.search_kind.currentData()
        if kind:
            self.search.setPlaceholderText(f"Search {SITE_NAME[kind]} for {store.KINDS[kind].lower()}, press Enter")
        else:
            sites = dict.fromkeys(SITE_NAME[k] for k in self.search_kinds())
            self.search.setPlaceholderText(f"Search {', '.join(sites) or 'nothing: no kind is shown'}, press Enter")

    def view_changed(self):
        grid = self.grid_btn.isChecked()
        self.main.conf["view"] = "grid" if grid else "list"
        store.save_conf(self.main.conf)
        self.views.setCurrentIndex(grid)
        self.fill()

    def reload(self):
        self.rows = store.entries(self.main.db)
        counts = {k: 0 for k in store.KINDS}
        for e in self.rows:
            counts[e["kind"]] += 1
        for k, b in self.chips.items():
            b.setText(f"{store.KINDS[k]}  {counts[k]}")
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(b.text()) + 40)  # the stylesheet padding isn't in the size hint
        self.fill()

    def visible(self):
        kinds, needle, status = self.kinds(), self.filter.text().casefold(), self.status.currentData()
        return [e for e in self.rows if e["kind"] in kinds and (not status or e["status"] == status)
                and needle in f"{e['title']} {e['creator']} {e['notes']}".casefold()]

    def fill(self, *_):
        """ponytail: rebuilds the whole view, fine into the tens of thousands; a model/view pair if it ever lags."""
        (self.fill_grid if self.grid_btn.isChecked() else self.fill_table)(self.visible(), self.current["id"] if self.current else None)
        self.main.show_stats()

    def fill_table(self, rows, keep):
        t = self.table
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.clearContents()
        t.setRowCount(len(rows))
        for y, e in enumerate(rows):
            cells = ("● " + KIND_ONE[e["kind"]], e["title"], e["creator"], e["year"], status_label(e["kind"], e["status"]), e["rating"], e["finished"])
            for x, v in enumerate(cells):
                item = QTableWidgetItem()
                item.setData(Qt.DisplayRole, v if v is not None else "")  # numbers stay numbers, so they sort as numbers
                t.setItem(y, x, item)
            t.item(y, 0).setForeground(QColor(KIND_COLOR[e["kind"]]))
            t.item(y, self.title_col).setData(ID_ROLE, e["id"])
        t.setSortingEnabled(True)
        t.blockSignals(False)
        for y in range(t.rowCount()):
            if t.item(y, self.title_col).data(ID_ROLE) == keep:
                t.selectRow(y)
                break
        else:
            self.show_entry(None)

    def fill_grid(self, rows, keep):
        """ponytail: every cover in the filtered log is fetched and kept in memory (~135 KB each); fetch only the
        scrolled-to tiles if logs of thousands of covers get slow or heavy."""
        g = self.grid
        g.blockSignals(True)
        g.clear()
        self.tiles = {}
        for e in rows:
            item = QListWidgetItem()
            rating = f"{e['rating']:g}/10" if e["rating"] is not None else ""
            item.setData(LINES_ROLE, (e["title"], " · ".join(str(x) for x in (e["year"], e["creator"]) if x),
                                      " · ".join(x for x in (status_label(e["kind"], e["status"]), rating) if x)))
            item.setData(ID_ROLE, e["id"])
            item.setData(KIND_ROLE, KIND_COLOR[e["kind"]])
            item.setToolTip(f"{e['title']}\n{store.KINDS[e['kind']]}")
            g.addItem(item)
            self.tiles[e["id"]] = item
            self.paint_tile(item, e)
            url = e["cover"]
            if url and url not in self.pix:
                self.pix[url] = False  # asked for
                run(lambda url=url: load_cover(url), lambda path, err, url=url: self.got_tile(url, path, err))
            if e["id"] == keep:
                g.setCurrentItem(item)
        g.blockSignals(False)
        if not g.currentItem():
            self.show_entry(None)

    def paint_tile(self, item, e):
        pix = self.pix.get(e["cover"]) if e["cover"] else None
        item.setData(Qt.UserRole, "none" if not e["cover"] or pix is None else "wait")
        hide = store.adult(e) and e["id"] not in self.revealed and not self.show_adult.isChecked()
        item.setData(BLUR_ROLE, (blur(pix) if pix else False) if hide else None)
        item.setData(COVER_ROLE, pix or None)

    def got_tile(self, url, path, err):
        pix = None if err else QPixmap(str(path))
        self.pix[url] = pix.scaled(TILE_W, TILE_H, Qt.KeepAspectRatio, Qt.SmoothTransformation) if pix and not pix.isNull() else None
        for e in self.rows:
            if e["cover"] == url and e["id"] in self.tiles:
                self.paint_tile(self.tiles[e["id"]], e)

    def stats(self):
        rows = self.visible()
        rated = [e["rating"] for e in rows if e["rating"] is not None]
        mean = f", mean rating {sum(rated) / len(rated):.1f} of {len(rated)} rated" if rated else ""
        return f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'}{mean}"

    # ----- the selected entry -----

    def selected(self):
        if self.grid_btn.isChecked():
            item = self.grid.currentItem() if self.grid.selectedItems() else None
            entry_id = item.data(ID_ROLE) if item else None
        else:
            items = self.table.selectedItems()
            entry_id = self.table.item(items[0].row(), self.title_col).data(ID_ROLE) if items else None
        self.show_entry(next((e for e in self.rows if e["id"] == entry_id), None))

    def show_entry(self, e):
        self.current = e
        for b in (self.edit_btn, self.del_btn):
            b.setEnabled(bool(e))
        self.link_btn.setEnabled(bool(e and e["url"]))
        self.cover.clear()
        self.cover_pix = None
        if not e:
            self.heading.clear(), self.meta.clear(), self.notes.clear()
            return
        self.heading.setText(e["title"])
        bits = [store.KINDS[e["kind"]], e["creator"], str(e["year"] or ""), status_label(e["kind"], e["status"]),
                f"{e['rating']:g}/10" if e["rating"] is not None else "", f"progress {e['progress']}" if e["progress"] else "",
                f"from {e['started']}" if e["started"] else "", f"finished {e['finished']}" if e["finished"] else ""]
        path = store.get_path(self.main.db, e["site"], e["key"])
        self.meta.setText(" · ".join(b for b in bits if b) + (f"\n{path}" if path else ""))
        self.notes.setPlainText(e["notes"])
        if e["cover"]:
            url = e["cover"]
            run(lambda: load_cover(url), lambda path, err: self.got_cover(e["id"], path, err))

    def got_cover(self, entry_id, path, err):
        if not err and self.current and self.current["id"] == entry_id:
            self.cover_pix = QPixmap(str(path)).scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.paint_cover()

    def paint_cover(self):
        e, pix = self.current, self.cover_pix
        if not e or not pix or pix.isNull():
            return
        if store.adult(e) and e["id"] not in self.revealed and not self.show_adult.isChecked():
            pix = blur(pix)
            p = QPainter(pix)
            badge(p, pix.rect(), "18+ · click to show")
            p.end()
            self.cover.setToolTip("Adult cover: click to show it")
        else:
            self.cover.setToolTip("")
        self.cover.setPixmap(pix)

    def reveal_cover(self):
        e = self.current
        if e:
            self.revealed ^= {e["id"]}  # click again to blur it back
            self.paint_cover()
            if e["id"] in self.tiles:
                self.paint_tile(self.tiles[e["id"]], e)

    def reveal_all(self, *_):
        self.paint_cover()
        for e in self.rows:
            if e["id"] in self.tiles:
                self.paint_tile(self.tiles[e["id"]], e)

    # ----- search and edit -----

    def do_search(self):
        q, kinds = self.search.text().strip(), self.search_kinds()
        if not q or not kinds:
            return
        self.search_id += 1
        ticket = self.search_id
        self.main.statusBar().showMessage(f"Searching {', '.join(dict.fromkeys(SITE_NAME[k] for k in kinds))}…")
        run(lambda: sources.search_many(kinds, q), lambda res, err: self.results(ticket, kinds, res, err))

    def results(self, ticket, kinds, res, err):
        if ticket != self.search_id:
            return  # a newer search replaced this one
        self.main.show_stats()
        if err:
            show_error(self, "Search failed", err)
            return
        rows, errors = res
        if errors:  # the others still answered: say which didn't, and show what came back
            self.main.statusBar().showMessage("No answer from " + ", ".join(f"{SITE_NAME[k]} ({e})" for k, e in errors.items()))
        heading = store.KINDS[kinds[0]] if len(kinds) == 1 else "Search"
        dlg = ResultsDialog(self, self.main.db, heading, rows)
        picked = dlg.exec() and dlg.chosen is not None
        dlg.deleteLater()
        if picked:
            r = dlg.chosen
            if not r:  # "Enter by hand…"
                self.add_by_hand(kinds)
                return
            self.log_row(store.find(self.main.db, r["site"], r["key"]) or r)

    def add_by_hand(self, kinds=None):
        kinds = kinds or self.search_kinds() or list(store.KINDS)
        kind = kinds[0]
        if len(kinds) > 1:
            labels = [store.KINDS[k] for k in kinds]
            label, ok = QInputDialog.getItem(self, "Add by hand", "Kind:", labels, 0, False)
            if not ok:
                return
            kind = kinds[labels.index(label)]
        self.log_row({"kind": kind})

    def log_row(self, row):
        dlg = LogDialog(self, row)
        if dlg.exec():
            store.save_entry(self.main.db, dlg.saved)
            self.reload()

    def edit(self):
        if self.current:
            self.log_row(self.current)

    def actions(self):
        menu = self.action_menu()
        if menu:
            menu.exec(QCursor.pos())

    def action_menu(self):
        """The click menu: open what you have of it on disk, or say where it is."""
        e = self.current
        if not e:
            return None
        path = store.get_path(self.main.db, e["site"], e["key"])
        here = bool(path) and os.path.exists(path)
        steam = e["key"].removeprefix("steam:") if e["key"].startswith("steam:") else ""
        menu = QMenu(self)
        target = scan.main_file(path, e["kind"]) if here else None
        if steam and not target:
            menu.addAction("Play on Steam", lambda: QDesktopServices.openUrl(QUrl(f"steam://rungameid/{steam}")))
        else:
            menu.addAction("Open file", lambda: open_local(target)).setEnabled(bool(target))
        menu.addAction("Open folder", lambda: open_local(path if os.path.isdir(path) else os.path.dirname(path))).setEnabled(here)
        menu.addAction("Open screenshots folder", lambda: self.screenshots(path)).setEnabled(here)
        menu.addSeparator()
        where = menu.addMenu("Location" if path else "Set location")
        where.addAction("Choose folder…", lambda: self.locate(QFileDialog.getExistingDirectory(self, "Where is it?", path or str(Path.home()))))
        where.addAction("Choose file…", lambda: self.locate(QFileDialog.getOpenFileName(self, "Where is it?", path or str(Path.home()))[0]))
        if path:
            where.addAction("Forget location", lambda: self.locate(None))
        menu.addSeparator()
        menu.addAction("Edit…", self.edit)
        menu.addAction("Open site", lambda: open_link(e["url"])).setEnabled(bool(e["url"]))
        return menu

    def locate(self, path):
        if path is None or path:  # None: forget it. "": the file dialog was cancelled
            store.set_path(self.main.db, self.current["site"], self.current["key"], path or "")
            self.show_entry(self.current)

    def screenshots(self, path):
        shots = scan.screenshots_dir(path)
        try:
            os.makedirs(shots, exist_ok=True)
        except OSError as err:
            show_error(self, "Can't make the screenshots folder", err)
            return
        open_local(shots)

    def delete(self):
        if self.current and confirm(self, "Delete entry?", f"Remove “{self.current['title']}” from this log?", "Delete"):
            store.delete_entry(self.main.db, self.current["id"])
            self.current = None
            self.reload()


# ---------- grid creator ----------

GRID_MAX, GRID_PIX = 20, 400  # cells per side; covers are kept cropped to this many pixels square


def square(pix):
    """The middle square of a cover, at most GRID_PIX wide."""
    side = min(pix.width(), pix.height())
    sq = pix.copy((pix.width() - side) // 2, (pix.height() - side) // 2, side, side)
    return sq.scaled(GRID_PIX, GRID_PIX, Qt.KeepAspectRatio, Qt.SmoothTransformation) if side > GRID_PIX else sq


def grid_name(e):
    return f"{e['creator']} – {e['title']}" if e["kind"] == "music" and e["creator"] else e["title"]


class GridCanvas(QWidget):
    """Paints the grid scaled to fit; the export paints the same thing at full size. Click a cell to pick it,
    drag one cell onto another to swap them, drop a log entry from the list onto a cell, Delete or right-click clears."""
    BASE = 100  # cell size the screen layout is computed at, before scaling to the widget

    def __init__(self, tab):
        super().__init__()
        self.tab, self.sel, self.press = tab, 0, None
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(300, 300)

    def geometry_(self):
        w, h = self.tab.size_at(self.BASE)
        s = min(self.width() / w, self.height() / h)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def cell_at(self, pos):
        s, ox, oy = self.geometry_()
        x, y = int((pos.x() - ox) / s // self.BASE), int((pos.y() - oy) / s // self.BASE)
        t = self.tab
        return y * t.cols.value() + x if 0 <= x < t.cols.value() and 0 <= y < t.rows.value() else None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().window())
        s, ox, oy = self.geometry_()
        p.translate(ox, oy)
        p.scale(s, s)
        self.tab.render(p, self.BASE, self.sel)
        p.end()

    def mousePressEvent(self, ev):
        i = self.cell_at(ev.position())
        if i is None:
            return
        if ev.button() == Qt.RightButton:
            self.tab.put(i, None)
        else:
            self.sel, self.press = i, i
        self.update()

    def mouseReleaseEvent(self, ev):
        i = self.cell_at(ev.position())
        if self.press is not None and i is not None and i != self.press:
            self.tab.swap(self.press, i)
            self.sel = i
        self.press = None
        self.update()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.tab.put(self.sel, None)
        else:
            super().keyPressEvent(ev)

    def dragEnterEvent(self, ev):
        if ev.source() is self.tab.picker:
            ev.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, ev):
        i, item = self.cell_at(ev.position()), self.tab.picker.currentItem()
        if i is not None and item:
            self.sel = i
            self.tab.put(i, item.data(ID_ROLE))


class GridTab(QWidget):
    """A chart of covers, up to 20×20, square tiles, optional names on the right, saved as a PNG.
    The layout is kept per profile (settings table, not in exported profiles: entry ids are local)."""

    def __init__(self, main):
        super().__init__()
        self.main, self.cells, self.by_id = main, [], {}
        self.pix, self.blurred = {}, {}  # cover url -> square pixmap (None: failed / asked); -> its blurred twin
        box = QHBoxLayout(self)

        left = QVBoxLayout()
        self.find = QLineEdit(placeholderText="Filter your log")
        self.find.textChanged.connect(self.fill_picker)
        self.picker = QListWidget()
        self.picker.setDragEnabled(True)
        self.picker.setDragDropMode(QAbstractItemView.DragOnly)
        self.picker.itemActivated.connect(self.place)
        left.addWidget(self.find)
        left.addWidget(self.picker, 1)
        left.addWidget(QLabel("Double-click to fill the selected cell, or drag onto a cell.", wordWrap=True, textFormat=Qt.PlainText))
        side = QWidget()
        side.setLayout(left)
        side.setMaximumWidth(320)
        box.addWidget(side)

        right = QVBoxLayout()
        bar = QHBoxLayout()
        self.cols, self.rows = QSpinBox(minimum=1, maximum=GRID_MAX, value=3), QSpinBox(minimum=1, maximum=GRID_MAX, value=3)
        self.names = QCheckBox("Names on the right")
        self.show_adult = QCheckBox("Show adult covers")
        self.cell_px = QSpinBox(minimum=50, maximum=GRID_PIX, value=300, suffix=" px", singleStep=50)
        export, clear = QPushButton("Export image…"), QPushButton("Clear")
        bar.addWidget(QLabel("Columns"))
        bar.addWidget(self.cols)
        bar.addWidget(QLabel("Rows"))
        bar.addWidget(self.rows)
        bar.addWidget(self.names)
        bar.addWidget(self.show_adult)
        bar.addStretch(1)
        bar.addWidget(clear)
        bar.addWidget(QLabel("Cell"))
        bar.addWidget(self.cell_px)
        bar.addWidget(export)
        right.addLayout(bar)
        self.canvas = GridCanvas(self)
        right.addWidget(self.canvas, 1)
        right.addWidget(QLabel("Drag a cell onto another to swap them. Right-click or Delete empties a cell.", textFormat=Qt.PlainText))
        box.addLayout(right, 1)

        for w in (self.cols, self.rows):
            w.valueChanged.connect(self.resized)
        self.names.toggled.connect(self.changed)
        self.show_adult.toggled.connect(self.canvas.update)
        clear.clicked.connect(self.clear)
        export.clicked.connect(self.export)

    # ----- data -----

    def reload(self):
        """From the current profile: the log for the picker, the saved layout for the grid."""
        self.by_id = {e["id"]: e for e in store.entries(self.main.db)}
        try:
            saved = json.loads(store.get_setting(self.main.db, "grid") or "{}")
            cols, rows, names, cells = int(saved.get("cols", 3)), int(saved.get("rows", 3)), bool(saved.get("names")), list(saved.get("cells", []))
        except (ValueError, TypeError, AttributeError):
            cols, rows, names, cells = 3, 3, False, []
        for w, v in ((self.cols, cols), (self.rows, rows), (self.names, names)):
            w.blockSignals(True)
            w.setValue(v) if isinstance(w, QSpinBox) else w.setChecked(v)
            w.blockSignals(False)
        self.cells = [c if c in self.by_id else None for c in cells]
        self.resized()
        self.fill_picker()

    def fill_picker(self, *_):
        needle = self.find.text().casefold()
        self.picker.clear()
        for e in sorted(self.by_id.values(), key=lambda e: e["title"].casefold()):
            if needle in f"{e['title']} {e['creator']}".casefold():
                item = QListWidgetItem(f"● {grid_name(e)}")
                item.setForeground(QColor(KIND_COLOR[e["kind"]]))
                item.setToolTip(store.KINDS[e["kind"]])
                item.setData(ID_ROLE, e["id"])
                self.picker.addItem(item)

    def resized(self, *_):
        n = self.cols.value() * self.rows.value()
        self.cells = (self.cells + [None] * n)[:n]  # ponytail: cells reflow in reading order; extra ones past the end are dropped
        self.canvas.sel = min(self.canvas.sel, n - 1)
        self.changed()

    def changed(self, *_):
        store.set_setting(self.main.db, "grid", json.dumps({"cols": self.cols.value(), "rows": self.rows.value(), "names": self.names.isChecked(), "cells": self.cells}))
        for c in self.cells:
            e = self.by_id.get(c)
            if e and e["cover"] and e["cover"] not in self.pix:
                url = e["cover"]
                self.pix[url] = None
                run(lambda url=url: load_cover(url), lambda path, err, url=url: self.got_cover(url, path, err))
        self.canvas.update()

    def got_cover(self, url, path, err):
        pix = None if err else QPixmap(str(path))
        if pix and not pix.isNull():
            self.pix[url] = square(pix)
            self.canvas.update()

    def put(self, i, entry_id):
        self.cells[i] = entry_id
        if entry_id is not None:  # move on to the next empty cell, so double-clicks fill the grid in order
            empty = [j for j in range(len(self.cells)) if self.cells[j] is None]
            self.canvas.sel = next((j for j in empty if j > i), empty[0] if empty else i)
        self.changed()

    def place(self, item):
        self.put(self.canvas.sel, item.data(ID_ROLE))

    def swap(self, a, b):
        self.cells[a], self.cells[b] = self.cells[b], self.cells[a]
        self.changed()

    def clear(self):
        if any(self.cells) and confirm(self, "Clear the grid?", "Empty every cell?", "Clear"):
            self.cells = [None] * len(self.cells)
            self.canvas.sel = 0
            self.changed()

    # ----- drawing: the screen and the export share this -----

    def name_lines(self):
        """One block per row: the names in it, top to bottom."""
        cols = self.cols.value()
        return [[grid_name(self.by_id[c]) for c in self.cells[r * cols:(r + 1) * cols] if c in self.by_id] for r in range(self.rows.value())]

    def name_font(self, cell):
        lines = self.name_lines()
        n = sum(map(len, lines)) + len(lines)  # a blank line between rows
        f = QFont(self.font())
        f.setPixelSize(max(6, int(min(cell * 0.16, self.rows.value() * cell / max(n, 1) / 1.2))))
        return f, lines

    def size_at(self, cell):
        w, h = self.cols.value() * cell, self.rows.value() * cell
        if self.names.isChecked():
            f, lines = self.name_font(cell)
            fm = QFontMetrics(f)
            widest = max((fm.horizontalAdvance(t) for block in lines for t in block), default=0)
            w += min(widest, cell * 6) + cell // 2  # ponytail: very long names are cut at six cells wide. The slack: text measures wider scaled down on screen
        return w, h

    def render(self, p, cell, sel=None):
        cols = self.cols.value()
        w, h = self.size_at(cell)
        p.fillRect(QRect(0, 0, w, h), QColor("black"))
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        gap = max(1, cell // 50)
        for i, c in enumerate(self.cells):
            box = QRect(i % cols * cell + gap, i // cols * cell + gap, cell - 2 * gap, cell - 2 * gap)
            e = self.by_id.get(c)
            pix = self.pix.get(e["cover"]) if e and e["cover"] else None
            if pix:
                if store.adult(e) and not self.show_adult.isChecked():
                    pix = self.blurred.setdefault(e["cover"], blur(pix))
                p.drawPixmap(box, pix)
                if pix is self.blurred.get(e["cover"]):
                    p.save()
                    f = QFont(self.font())
                    f.setPixelSize(max(6, cell // 8))
                    p.setFont(f)
                    badge(p, box)
                    p.restore()
            else:
                p.fillRect(box, QColor("#202020"))
                if e:  # no cover (yet): the name, so the cell isn't a mystery
                    f = QFont(self.font())
                    f.setPixelSize(max(6, cell // 9))
                    p.setFont(f)
                    p.setPen(QColor("#cccccc"))
                    p.drawText(box.adjusted(4, 4, -4, -4), Qt.AlignCenter | Qt.TextWordWrap, grid_name(e))
            if sel == i:
                p.setPen(QPen(self.palette().highlight().color(), max(2, cell // 25)))
                p.setBrush(Qt.NoBrush)
                p.drawRect(box)
        if self.names.isChecked():
            f, lines = self.name_font(cell)
            fm = QFontMetrics(f)
            p.setFont(f)
            p.setPen(QColor("white"))
            x, y, room = cols * cell + cell // 6, 0, w - cols * cell - cell // 3
            for r, block in enumerate(lines):
                y = max(y, r * cell)  # each row's names start level with that row, unless the row above ran long
                for t in block:
                    p.drawText(QRect(x, y, room, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(t, Qt.ElideRight, room))
                    y += fm.height()
                y += fm.height() // 2

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export grid", str(Path.home() / f"{self.main.profile}-grid.png"), "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        cell = self.cell_px.value()
        img = QImage(*self.size_at(cell), QImage.Format_RGB32)
        p = QPainter(img)
        self.render(p, cell)
        p.end()
        if img.save(path):
            self.main.statusBar().showMessage(f"Saved {img.width()}×{img.height()} grid to {path}")
        else:
            show_error(self, "Export failed", f"Couldn't write {path}")


# ---------- main window ----------

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Logger")
        self.resize(1200, 760)
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
        scan_act = QAction("Scan for media…", self)
        scan_act.triggered.connect(lambda: ScanDialog(self).exec())
        bar.addAction(scan_act)

        self.home, self.grid = Home(self), GridTab(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.home, "Home")
        self.tabs.addTab(self.grid, "Grid creator")
        self.tabs.currentChanged.connect(self.refresh)
        self.setCentralWidget(self.tabs)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.home.filter.setFocus())
        self.load_profiles()
        self.refresh()

    def load_profiles(self):
        self.combo.clear()
        self.combo.addItems(store.list_profiles())
        self.combo.setCurrentText(self.profile)

    def refresh(self, *_):
        self.tabs.currentWidget().reload()

    def show_stats(self):
        if hasattr(self, "home"):  # the first fill runs while Home is still being built
            self.statusBar().showMessage(f"{self.profile}: {self.home.stats()}")

    def switch(self, name):
        self.db.close()
        self.profile, self.db = name, store.open_db(name)
        self.conf["profile"] = name
        store.save_conf(self.conf)
        self.load_profiles()
        self.home.current = None
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    def crash(kind, exc, tb):
        log.critical("unhandled error: %s", "".join(traceback.format_exception(kind, exc, tb)).rstrip())
    sys.excepthook = crash
    threading.excepthook = lambda a: crash(a.exc_type, a.exc_value, a.exc_traceback)
    app = QApplication(sys.argv)
    app.setApplicationName("medialogger")
    app.setDesktopFileName("medialogger")
    bridge = Bridge()
    import PySide6
    log.info("started: Python %s, PySide6 %s, %s %s", platform.python_version(), PySide6.__version__, platform.system(), platform.release())
    log.info("profiles in %s", store.data_dir() / "profiles")
    win = Main()
    win.show()
    if "--screenshot" in sys.argv:  # a picture of the real window (CI checks it on Linux and Windows)
        shot = sys.argv[sys.argv.index("--screenshot") + 1]
        QTimer.singleShot(3000, lambda: (log.info("screenshot %s: %s", shot, win.grab().save(shot)), app.quit()))
    code = app.exec()
    POOL.shutdown(wait=False, cancel_futures=True)
    sys.exit(code)
