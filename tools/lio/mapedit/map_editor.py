#!/usr/bin/env python3
"""
MOLA occupancy-map editor — clean up and hand-edit a ROS map_server grid.

Input/output: standard map_server pair (PGM P5 + YAML). Pixel values:
    OCC=0 (black, wall/obstacle), UNKNOWN=205 (gray), FREE=254 (white).

Features
  Auto-clean (Tools menu, undoable):
    - denoise:   remove small isolated occupied blobs -> free
    - fill:      unknown handling, aggressive (all unknown->free) or
                 conservative (only unknown not touching the border -> free)
    - morphology: close occupied (seal thin gaps) / open (strip specks)
  Manual (toolbar):
    - wall pen (occ) / free pen / unknown pen, adjustable brush size
    - virtual wall: click two endpoints to draw an occupied line
    - rectangle fill -> free / occ / unknown
    - undo / redo (Ctrl+Z / Ctrl+Y)
  Middle-drag = pan, wheel = zoom.

Export writes <stem>_edited.pgm + <stem>_edited.yaml (resolution/origin
inherited), optionally a PNG for display.

Headless self-test:  python3 map_editor.py --selftest <floor.pgm>
"""
import sys, os, argparse
import numpy as np
import yaml
from PIL import Image
from scipy import ndimage

OCC, UNKNOWN, FREE = 0, 205, 254


# ----------------------------- map IO --------------------------------------
def load_map(pgm_path):
    arr = np.array(Image.open(pgm_path).convert("L"), dtype=np.uint8)
    meta = {}
    yaml_path = os.path.splitext(pgm_path)[0] + ".yaml"
    if os.path.exists(yaml_path):
        with open(yaml_path) as f:
            meta = yaml.safe_load(f) or {}
    return arr, meta, yaml_path


def save_map(arr, meta, pgm_out):
    Image.fromarray(arr.astype(np.uint8), mode="L").save(pgm_out)
    yaml_out = os.path.splitext(pgm_out)[0] + ".yaml"
    out = dict(meta) if meta else {}
    out["image"] = os.path.basename(pgm_out)
    out.setdefault("resolution", 0.05)
    out.setdefault("origin", [0.0, 0.0, 0.0])
    out.setdefault("negate", 0)
    out.setdefault("occupied_thresh", 0.65)
    out.setdefault("free_thresh", 0.196)
    with open(yaml_out, "w") as f:
        yaml.safe_dump(out, f, default_flow_style=None, sort_keys=False)
    return yaml_out


# --------------------------- cleaning ops ----------------------------------
def denoise(arr, min_area=12):
    """Remove occupied blobs smaller than min_area px -> free."""
    out = arr.copy()
    occ = out == OCC
    lbl, n = ndimage.label(occ)
    if n:
        sizes = ndimage.sum(np.ones_like(lbl), lbl, index=np.arange(1, n + 1))
        small = np.isin(lbl, np.nonzero(sizes < min_area)[0] + 1)
        out[small] = FREE
    return out


def fill_unknown(arr, aggressive=True):
    """aggressive: every unknown -> free.
    conservative: only unknown regions NOT touching the image border -> free."""
    out = arr.copy()
    unk = out == UNKNOWN
    if aggressive:
        out[unk] = FREE
        return out
    lbl, n = ndimage.label(unk)
    if n:
        border = set(np.unique(np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]])))
        border.discard(0)
        interior = ~np.isin(lbl, list(border)) & unk
        out[interior] = FREE
    return out


def morph(arr, close_iter=1, open_iter=0):
    """Morphological cleanup on the occupied layer only."""
    out = arr.copy()
    occ = out == OCC
    if close_iter:
        occ = ndimage.binary_closing(occ, iterations=close_iter)
    if open_iter:
        occ = ndimage.binary_opening(occ, iterations=open_iter)
    out[(out != UNKNOWN) & occ] = OCC          # keep unknown as-is
    out[(out == OCC) & ~occ] = FREE
    return out


def auto_clean(arr, min_area=12, aggressive=True, close_iter=1, open_iter=0):
    a = denoise(arr, min_area)
    a = morph(a, close_iter, open_iter)
    a = fill_unknown(a, aggressive)
    return a


# ------------------------------- GUI ---------------------------------------
def launch_gui(pgm_path):
    from PyQt5 import QtCore, QtGui, QtWidgets

    arr0, meta, yaml_path = load_map(pgm_path)

    def to_qimage(a):
        a = np.ascontiguousarray(a.astype(np.uint8))
        h, w = a.shape
        return QtGui.QImage(a.data, w, h, w, QtGui.QImage.Format_Grayscale8).copy()

    class Canvas(QtWidgets.QGraphicsView):
        def __init__(self, arr, status):
            super().__init__()
            self.arr = arr
            self.status = status
            self.tool = "wall"
            self.brush = 4
            self.undo, self.redo = [], []
            self.line_p0 = None
            self.rect_p0 = None
            self._painting = False
            self.scene_ = QtWidgets.QGraphicsScene(self)
            self.setScene(self.scene_)
            self.pix = self.scene_.addPixmap(QtGui.QPixmap.fromImage(to_qimage(arr)))
            self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
            self.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)

        # ---- value for current tool ----
        def tool_value(self):
            return {"wall": OCC, "free": FREE, "unknown": UNKNOWN,
                    "vwall": OCC}.get(self.tool, OCC)

        def refresh(self):
            self.pix.setPixmap(QtGui.QPixmap.fromImage(to_qimage(self.arr)))

        def push_undo(self):
            self.undo.append(self.arr.copy())
            if len(self.undo) > 30:
                self.undo.pop(0)
            self.redo.clear()

        def do_undo(self):
            if self.undo:
                self.redo.append(self.arr.copy())
                self.arr = self.undo.pop()
                self.refresh()

        def do_redo(self):
            if self.redo:
                self.undo.append(self.arr.copy())
                self.arr = self.redo.pop()
                self.refresh()

        def apply_array(self, new_arr):
            self.push_undo()
            self.arr = new_arr
            self.refresh()

        # ---- drawing primitives on numpy ----
        def paint_disk(self, r, c, val):
            R = self.brush
            h, w = self.arr.shape
            r0, r1 = max(0, r - R), min(h, r + R + 1)
            c0, c1 = max(0, c - R), min(w, c + R + 1)
            yy, xx = np.ogrid[r0:r1, c0:c1]
            mask = (yy - r) ** 2 + (xx - c) ** 2 <= R * R
            self.arr[r0:r1, c0:c1][mask] = val

        def paint_line(self, r0, c0, r1, c1, val):
            n = int(max(abs(r1 - r0), abs(c1 - c0))) + 1
            for t in np.linspace(0, 1, n):
                self.paint_disk(int(round(r0 + (r1 - r0) * t)),
                                int(round(c0 + (c1 - c0) * t)), val)

        def scene_rc(self, ev):
            p = self.mapToScene(ev.pos())
            return int(p.y()), int(p.x())

        # ---- events ----
        def wheelEvent(self, ev):
            f = 1.25 if ev.angleDelta().y() > 0 else 0.8
            self.scale(f, f)

        def mousePressEvent(self, ev):
            if ev.button() == QtCore.Qt.MiddleButton:
                self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
                fake = QtGui.QMouseEvent(ev.type(), ev.pos(), QtCore.Qt.LeftButton,
                                         QtCore.Qt.LeftButton, ev.modifiers())
                return super().mousePressEvent(fake)
            if ev.button() != QtCore.Qt.LeftButton:
                return super().mousePressEvent(ev)
            r, c = self.scene_rc(ev)
            if self.tool in ("wall", "free", "unknown"):
                self.push_undo(); self._painting = True
                self.paint_disk(r, c, self.tool_value()); self.refresh()
            elif self.tool == "vwall":
                if self.line_p0 is None:
                    self.line_p0 = (r, c); self.status(f"virtual wall: start ({r},{c}), click end")
                else:
                    self.push_undo()
                    self.paint_line(*self.line_p0, r, c, OCC); self.refresh()
                    self.line_p0 = None; self.status("virtual wall drawn")
            elif self.tool == "rect":
                self.rect_p0 = (r, c); self.status(f"rect: corner ({r},{c}), drag & release")

        def mouseMoveEvent(self, ev):
            r, c = self.scene_rc(ev)
            self.status(f"[{self.tool}] px=({r},{c}) brush={self.brush}")
            if self._painting and (ev.buttons() & QtCore.Qt.LeftButton):
                self.paint_disk(r, c, self.tool_value()); self.refresh()
            super().mouseMoveEvent(ev)

        def mouseReleaseEvent(self, ev):
            if ev.button() == QtCore.Qt.MiddleButton:
                super().mouseReleaseEvent(ev)
                self.setDragMode(QtWidgets.QGraphicsView.NoDrag); return
            self._painting = False
            if self.tool == "rect" and self.rect_p0 is not None:
                r1, c1 = self.scene_rc(ev); r0, c0 = self.rect_p0
                rr = sorted([max(0, r0), max(0, r1)]); cc = sorted([max(0, c0), max(0, c1)])
                self.push_undo()
                self.arr[rr[0]:rr[1] + 1, cc[0]:cc[1] + 1] = self.rect_val
                self.refresh(); self.rect_p0 = None
            super().mouseReleaseEvent(ev)

    class Main(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"Map Editor — {os.path.basename(pgm_path)}")
            self.status = self.statusBar().showMessage
            self.canvas = Canvas(arr0, self.status)
            self.meta = meta
            self.setCentralWidget(self.canvas)
            self.canvas.rect_val = FREE
            self._toolbar(); self._menu()
            self.resize(1200, 900)
            self.status("loaded. wall/free/unknown pens, virtual wall, rect fill; middle-drag pan, wheel zoom")

        def _toolbar(self):
            tb = self.addToolBar("tools")
            grp = QtWidgets.QActionGroup(self)
            for name, label in [("wall", "Wall pen"), ("free", "Free pen"),
                                ("unknown", "Unknown pen"), ("vwall", "Virtual wall"),
                                ("rect", "Rect fill")]:
                a = QtWidgets.QAction(label, self, checkable=True)
                a.triggered.connect(lambda _, n=name: self.set_tool(n))
                grp.addAction(a); tb.addAction(a)
                if name == "wall":
                    a.setChecked(True)
            tb.addSeparator()
            tb.addWidget(QtWidgets.QLabel(" brush "))
            sb = QtWidgets.QSpinBox(); sb.setRange(1, 60); sb.setValue(4)
            sb.valueChanged.connect(lambda v: setattr(self.canvas, "brush", v))
            tb.addWidget(sb)
            tb.addSeparator()
            tb.addWidget(QtWidgets.QLabel(" rect-> "))
            cb = QtWidgets.QComboBox(); cb.addItems(["free", "occ", "unknown"])
            cb.currentTextChanged.connect(
                lambda t: setattr(self.canvas, "rect_val",
                                  {"free": FREE, "occ": OCC, "unknown": UNKNOWN}[t]))
            tb.addWidget(cb)

        def set_tool(self, n):
            self.canvas.tool = n; self.canvas.line_p0 = None
            self.status(f"tool: {n}")

        def _menu(self):
            m = self.menuBar().addMenu("&Auto-clean")
            m.addAction("Auto-clean (denoise + morph + aggressive unknown->free)",
                        self.autoclean_aggr)
            m.addAction("Auto-clean (conservative: keep border unknown)",
                        self.autoclean_cons)
            m.addSeparator()
            m.addAction("Denoise only", lambda: self.canvas.apply_array(denoise(self.canvas.arr)))
            m.addAction("Fill ALL unknown -> free",
                        lambda: self.canvas.apply_array(fill_unknown(self.canvas.arr, True)))
            e = self.menuBar().addMenu("&Edit")
            u = QtWidgets.QAction("Undo", self); u.setShortcut("Ctrl+Z")
            u.triggered.connect(self.canvas.do_undo); e.addAction(u)
            r = QtWidgets.QAction("Redo", self); r.setShortcut("Ctrl+Y")
            r.triggered.connect(self.canvas.do_redo); e.addAction(r)
            f = self.menuBar().addMenu("&File")
            s = QtWidgets.QAction("Export (pgm+yaml)", self); s.setShortcut("Ctrl+S")
            s.triggered.connect(self.export); f.addAction(s)
            f.addAction("Export PNG (display)", self.export_png)

        def autoclean_aggr(self):
            self.canvas.apply_array(auto_clean(self.canvas.arr, aggressive=True))
            self.status("auto-clean (aggressive) applied")

        def autoclean_cons(self):
            self.canvas.apply_array(auto_clean(self.canvas.arr, aggressive=False))
            self.status("auto-clean (conservative) applied")

        def export(self):
            stem = os.path.splitext(pgm_path)[0]
            out = stem + "_edited.pgm"
            yout = save_map(self.canvas.arr, self.meta, out)
            self.status(f"saved {out} + {os.path.basename(yout)}")

        def export_png(self):
            from PyQt5 import QtGui
            stem = os.path.splitext(pgm_path)[0]
            out = stem + "_edited.png"
            Image.fromarray(self.canvas.arr.astype(np.uint8), "L").save(out)
            self.status(f"saved {out}")

    app = QtWidgets.QApplication(sys.argv)
    w = Main(); w.show()
    sys.exit(app.exec_())


# ------------------------------ CLI ----------------------------------------
def selftest(pgm):
    arr, meta, _ = load_map(pgm)
    print(f"loaded {pgm} shape={arr.shape} vals={np.unique(arr)}")
    print(f"  occ={np.mean(arr==OCC)*100:.1f}% unknown={np.mean(arr==UNKNOWN)*100:.1f}% free={np.mean(arr==FREE)*100:.1f}%")
    # round-trip
    tmp = "/tmp/_mapedit_rt.pgm"
    save_map(arr, meta, tmp)
    back, _, _ = load_map(tmp)
    assert np.array_equal(arr, back), "round-trip mismatch!"
    print("  round-trip: lossless OK")
    a = auto_clean(arr, aggressive=True)
    print(f"  after aggressive auto-clean: occ={np.mean(a==OCC)*100:.1f}% "
          f"unknown={np.mean(a==UNKNOWN)*100:.1f}% free={np.mean(a==FREE)*100:.1f}%")
    c = auto_clean(arr, aggressive=False)
    print(f"  after conservative auto-clean: unknown={np.mean(c==UNKNOWN)*100:.1f}% free={np.mean(c==FREE)*100:.1f}%")
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pgm", nargs="?",
                    default="/home/liang/Projects/d1max-patrol/runs/mola/coverage2/floor.pgm")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest(args.pgm)
    else:
        launch_gui(args.pgm)
