#!/usr/bin/env python3
"""
Source Compile Tool — PySide6 rewrite of the Source Engine map compiler.

PIP DEPENDENCIES
----------------
pip install PySide6 QtAwesome pyqtdarktheme

DATA LAYOUT
-----------
    config/presets.json, config/settings.ini, config/game_dirs.json,
    config/hidden_games.json, config/maps.json (map queue),
    config/parameters/<PROCESS>/parameters.json (base options)
    config/parameters/<PROCESS>/toolsplusplus.json (Tools++ options)
    logs/source_compile_tool.log

Parameter JSON format is compatible with CompilePal++
(Parameters/<PROCESS>/parameters.json + toolsplusplus.json).
Files are created on first launch and may be edited by hand.
"""

# =============================================================================
# SECTION 1 — IMPORTS
# =============================================================================
from __future__ import annotations

import glob
import codecs
import ctypes
import io
import json
import locale
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from PySide6.QtCore import (
    QAbstractListModel, QByteArray, QEasingCurve, QEvent, QMimeData, QModelIndex,
    QPersistentModelIndex, QObject, QPropertyAnimation, QSettings, QSize, Qt, QThread, QTimer,
    Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QIcon,
    QDragMoveEvent, QImage, QPainter, QPalette, QPixmap, QRadialGradient,
    QPen, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog,
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStyle,
    QScrollArea, QSpinBox, QSplitter, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from shiboken6 import isValid

try:
    import qdarktheme
except Exception:
    qdarktheme = None

try:
    import qtawesome as qta
except Exception:
    qta = None


class StandardArrowButton(QToolButton):
    """A tool button that remembers its Qt-provided arrow shape."""

    standard_arrow: QStyle.StandardPixmap

    def __init__(self, arrow: QStyle.StandardPixmap, parent: QWidget) -> None:
        super().__init__(parent)
        self.standard_arrow = arrow


class ClickToFocusSpinBox(QSpinBox):
    """Only consume the mouse wheel after the user clicks into the field."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._wheel_armed = False
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        # Avoid acquiring keyboard focus (and showing a text caret) from a
        # wheel interaction; focus this editor only after an actual click.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.lineEdit().setTextMargins(0, 0, 24, 0)
        # QSpinBox delegates text editing to an internal QLineEdit, so observe
        # clicks there as well as clicks on the spin buttons themselves.
        self.lineEdit().installEventFilter(self)
        self._up_button = self._make_step_button(QStyle.StandardPixmap.SP_ArrowUp,
                                                self.stepUp)
        self._down_button = self._make_step_button(QStyle.StandardPixmap.SP_ArrowDown,
                                                  self.stepDown)
        self.refresh_arrow_icons()
        self._up_button.pressed.connect(self._arm_from_button)
        self._down_button.pressed.connect(self._arm_from_button)
        self._position_step_buttons()

    def _make_step_button(
        self, standard_icon: QStyle.StandardPixmap, callback: Callable[[], None]
    ) -> StandardArrowButton:
        button = StandardArrowButton(standard_icon, self)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoRepeat(True)
        button.setIconSize(QSize(14, 14))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.clicked.connect(callback)
        button.setStyleSheet(
            "QToolButton { background-color: palette(button); "
            "border: 1px solid palette(mid); border-radius: 4px; "
            "padding: 0 0 0 5px; }"
            "QToolButton:hover { background-color: palette(midlight); }"
            "QToolButton:pressed { background-color: palette(dark); }")
        return button

    def refresh_arrow_icons(self) -> None:
        """Tint Qt's built-in arrow shapes with the current palette text color."""
        tint = self.palette().color(QPalette.ColorRole.ButtonText)
        for button in (self._up_button, self._down_button):
            source = self.style().standardIcon(button.standard_arrow).pixmap(QSize(18, 18))
            if source.isNull():
                continue
            image = source.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            xs, ys = [], []
            for y in range(image.height()):
                for x in range(image.width()):
                    if image.pixelColor(x, y).alpha() > 0:
                        xs.append(x)
                        ys.append(y)
            if not xs:
                continue
            glyph = source.copy(min(xs), min(ys),
                                max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
            pixmap = QPixmap(source.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.drawPixmap((pixmap.width() - glyph.width()) // 2,
                               (pixmap.height() - glyph.height()) // 2, glyph)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), tint)
            painter.end()
            button.setIcon(QIcon(pixmap))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.StyleChange)
                and hasattr(self, "_up_button")
                and hasattr(self, "_down_button")):
            self.refresh_arrow_icons()

    def _position_step_buttons(self) -> None:
        button_width = 24
        button_height = max(14, (self.height() - 6) // 2)
        x = self.width() - button_width - 3
        self._up_button.setGeometry(x, 2, button_width, button_height)
        self._down_button.setGeometry(
            x, self.height() - button_height - 2, button_width, button_height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_step_buttons()

    def _arm_from_button(self) -> None:
        self._wheel_armed = True
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.lineEdit():
            if event.type() == QEvent.Type.MouseButtonPress:
                self._wheel_armed = True
            elif event.type() == QEvent.Type.FocusOut:
                self._wheel_armed = False
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        self._wheel_armed = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:
        self._wheel_armed = False
        super().focusOutEvent(event)

    def wheelEvent(self, event) -> None:
        if (not self._wheel_armed
                or not (self.hasFocus() or self.lineEdit().hasFocus())):
            event.ignore()
            return
        super().wheelEvent(event)


# =============================================================================
# SECTION 2 — ICON HELPERS
# =============================================================================
_ICON_MAP: Dict[str, List[str]] = {
    "compile": ["fa6s.play", "fa5s.play", "mdi.play"],
    "cancel": ["fa6s.stop", "fa5s.stop", "mdi.stop"],
    "refresh": ["fa6s.arrows-rotate", "fa6s.rotate", "fa5s.sync", "mdi.refresh"],
    "theme": ["fa6s.sun", "fa5s.sun", "mdi.white-balance-sunny"],
    "theme_dark": ["fa6s.moon", "fa5s.moon"],
    "game": ["fa6s.gamepad", "fa5s.gamepad", "mdi.gamepad"],
    "tools": ["fa6s.toolbox", "fa5s.toolbox", "mdi.toolbox", "mdi.toolbox-outline"],
    "tools_plus": ["fa6s.bolt", "fa5s.bolt", "mdi.lightning-bolt"],
    "map": ["fa6s.map", "fa5s.map", "mdi.map"],
    "add": ["fa6s.plus", "fa5s.plus", "mdi.plus"],
    "remove": ["fa6s.minus", "fa5s.minus", "mdi.minus"],
    "delete": ["fa6s.trash", "fa6s.trash-can", "fa5s.trash", "mdi.delete"],
    "presets": ["fa6s.gears", "fa6s.gear", "fa5s.cogs", "mdi.cog"],
    "clone": ["fa6s.copy", "fa5s.copy", "mdi.content-copy"],
    "preset_item": ["fa6s.bookmark", "fa5s.bookmark", "mdi.bookmark"],
    "processes": ["fa6s.list-check", "fa6s.tasks", "fa5s.tasks", "mdi.format-list-checks"],
    "proc_item": ["fa6s.circle-play", "fa5s.play-circle", "mdi.play-circle"],
    "folder": ["fa6s.folder-open", "fa5s.folder-open", "mdi.folder-open"],
    "file": ["fa6s.file", "fa5s.file", "mdi.file"],
    "param_add": ["fa6s.circle-plus", "fa5s.plus-circle", "mdi.plus-circle"],
    "log": ["fa6s.list", "fa5s.list", "mdi.format-list-bulleted"],
    "clear": ["fa6s.eraser", "fa5s.eraser", "mdi.eraser"],
    "hide": ["fa6s.eye-slash", "fa5s.eye-slash", "mdi.eye-off"],
    "search": ["fa6s.magnifying-glass", "fa5s.search", "mdi.magnify"],
    "copy": ["fa6s.copy", "fa5s.copy", "mdi.content-copy"],
    # compile-stage icons: "stage_<ProcessKind name in lower case>"; each key lists
    # fallbacks (Phosphor first, then Material Design / Font Awesome)
    "stage_vbsp": ["ph.cube", "mdi6.cube-outline", "fa6s.cube", "mdi.cube-outline"],
    "stage_vvis": ["ph.eye", "mdi6.eye-outline", "fa6s.eye", "mdi.eye-outline"],
    "stage_vrad": ["ph.lightbulb", "mdi6.lightbulb-outline", "fa6s.lightbulb", "mdi.lightbulb-outline"],
    "stage_copy": ["ph.copy", "mdi6.content-copy", "fa6s.copy", "mdi.content-copy"],
    "stage_nav": ["ph.gear", "mdi6.cog", "fa6s.gear", "mdi.cog"],
    "stage_pack": ["ph.package", "mdi6.package-variant-closed", "fa6s.box", "mdi.package-variant-closed"],
    "stage_bspzip": ["ph.file-zip", "mdi6.zip-box-outline", "fa6s.file-zipper", "mdi.zip-box"],
    "stage_cubemaps": ["mdi6.cube-scan", "mdi.cube-scan", "ph.cube", "fa6s.cubes"],
    "stage_game": ["ph.game-controller", "mdi6.gamepad-variant-outline", "fa6s.gamepad", "mdi.gamepad"],
    "stage_custom": ["ph.terminal-window", "mdi6.console", "fa6s.terminal", "mdi.console"],
    "stage_shutdown": ["ph.power", "mdi6.power", "fa6s.power-off", "mdi.power"],
}

_ICON_FG: Dict[str, str] = {"color": "#e6edf3"}
_NEUTRAL: List[Tuple[Any, str]] = []


_ICON_CACHE: Dict[Tuple[str, str], QIcon] = {}


def qicon(key: str, color: Optional[str] = None) -> QIcon:
    """Build (and cache) a qtawesome icon. qta.icon() is slow, so every
    (key, color) pair is only rendered once — switching themes back and forth
    then costs nothing for icons that were already created."""
    if qta is None:
        return QIcon()
    color = color or _ICON_FG["color"]
    cache_key = (key, color)
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    icon = QIcon()
    for name in _ICON_MAP.get(key, [key]):
        try:
            icon = qta.icon(name, color=color)
            break
        except Exception:
            continue
    _ICON_CACHE[cache_key] = icon
    return icon


def _prune_neutral() -> None:
    _NEUTRAL[:] = [(w, k) for w, k in _NEUTRAL if isValid(w)]


def neutral(widget: Any, key: str) -> Any:
    widget.setIcon(qicon(key))
    if len(_NEUTRAL) > 500:
        _prune_neutral()
    _NEUTRAL.append((widget, key))
    return widget


def refresh_neutral_icons() -> None:
    _prune_neutral()
    for widget, key in _NEUTRAL:
        try:
            widget.setIcon(qicon(key))
        except RuntimeError:
            pass


def rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


# =============================================================================
# SECTION 3 — PATHS, CONFIG & LOGGING
# =============================================================================
SCRIPT_DIR: Path = Path(__file__).resolve().parent
CONFIG_DIR: Path = SCRIPT_DIR / "config"
LOGS_DIR: Path = SCRIPT_DIR / "logs"
PARAMETERS_DIR: Path = CONFIG_DIR / "parameters"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

PRESETS_FILE: Path = CONFIG_DIR / "presets.json"
SETTINGS_FILE: Path = CONFIG_DIR / "settings.ini"
GAME_DIRS_FILE: Path = CONFIG_DIR / "game_dirs.json"
HIDDEN_GAMES_FILE: Path = CONFIG_DIR / "hidden_games.json"
MAPS_FILE: Path = CONFIG_DIR / "maps.json"
LOG_FILE: Path = LOGS_DIR / "source_compile_tool.log"

_handlers: List[logging.Handler] = [
    RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("Source Compile Tool")
logger.info("Source Compile Tool started. Config: %s", CONFIG_DIR)

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def load_dirs_config() -> Dict[str, Any]:
    try:
        data = json.loads(GAME_DIRS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            dirs = [Path(p) for p in data.get("dirs", []) if isinstance(p, str) and p.strip()]
            return {"dirs": dirs, "auto": bool(data.get("auto_scan", True))}
        if isinstance(data, list):
            return {"dirs": [Path(p) for p in data if isinstance(p, str) and p.strip()], "auto": True}
    except (OSError, ValueError):
        pass
    return {"dirs": [], "auto": True}


def save_dirs_config(dirs: List[Path], auto: bool) -> None:
    try:
        GAME_DIRS_FILE.write_text(
            json.dumps({"dirs": [str(p) for p in dirs], "auto_scan": bool(auto)}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        logger.error("Failed to save game dirs: %s", e)


def load_hidden_games() -> List[str]:
    try:
        data = json.loads(HIDDEN_GAMES_FILE.read_text(encoding="utf-8"))
        names = data.get("names", []) if isinstance(data, dict) else data
        return [n for n in names if isinstance(n, str) and n]
    except (OSError, ValueError):
        return []


def save_hidden_games(names: List[str]) -> None:
    try:
        HIDDEN_GAMES_FILE.write_text(
            json.dumps({"names": names}, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to save hidden games: %s", e)


def hide_game(name: str) -> None:
    names = load_hidden_games()
    if name not in names:
        names.append(name)
        save_hidden_games(names)


def unhide_game(name: str) -> None:
    save_hidden_games([n for n in load_hidden_games() if n != name])


# =============================================================================
# SECTION 4 — ENUMS, PARAMETER FILES (CompilePal++ format) & DATA CLASSES
# =============================================================================
class ProcessKind(str, Enum):
    VBSP = "VBSP"
    VVIS = "VVIS"
    VRAD = "VRAD"
    COPY = "COPY"
    NAV = "NAV"
    PACK = "PACK"
    BSPZIP = "BSPZIP"
    CUBEMAPS = "Cubemaps"
    GAME = "GAME"
    CUSTOM = "Custom"
    SHUTDOWN = "Shutdown"


PROCESS_ORDER: List[ProcessKind] = list(ProcessKind)

_APPID_NAMES: Dict[int, str] = {
    240: "CS:S", 300: "DoD:S", 440: "TF2", 550: "L4D2", 620: "Portal 2",
    669270: "Momentum Mod",
}


class LogSeverity(int, Enum):
    DEBUG = 0
    INFO = 1
    OK = 2
    WARNING = 3
    ERROR = 4
    HINT = 5   # explanation lines printed under known compile errors


# First log line of every map; the log view re-enables error hints when it sees it.
LOG_MAP_MARKER = "── Processing "


# ---------------------------------------------------------------------------
# Embedded defaults, written to config/parameters/<PROC>/*.json on first run.
# Format mirrors CompilePal++ Parameters/<PROC>/parameters.json and
# toolsplusplus.json; extra optional keys (ValueType / DefaultValue /
# DefaultEnabled) control this app's widgets and preset seeding.
# ---------------------------------------------------------------------------
def _PD(name: str, param: str, desc: str, has_value: bool = False,
        vfile: bool = False, vfolder: bool = False, tp: bool = False,
        warn: str = "", vtype: str = "string", dval: str = "",
        den: bool = False, multi: bool = False,
        games: Optional[List[int]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "Name": name, "Parameter": param, "Description": desc,
        "CanHaveValue": has_value, "ValueIsFile": vfile, "ValueIsFolder": vfolder,
        "ToolsPlusPlusOnly": tp, "Warning": warn, "ValueType": vtype,
        "DefaultValue": dval, "DefaultEnabled": den,
    }
    if multi:
        entry["CanBeUsedMoreThanOnce"] = True
    if games:
        entry["CompatibleGames"] = list(games)
    return entry


DEFAULT_PARAMETER_FILES: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "VBSP": {
        "parameters": [
            _PD("Verbose", "-v", "Turn on verbose output."),
            _PD("Only Entities", "-onlyents",
                "Only import the entities from the VMF. Brushes and internal entities "
                "are not modified. Conserves existing lighting data."),
            _PD("Only Props", "-onlyprops",
                "Only update static and detail props (i.e. the internal entities).",
                warn="Does not generate a .prt file, making VVIS fail!"),
            _PD("Write GLView", "-glview", "Writes glview debug data next to the VMF."),
            _PD("Remove func_detail", "-nodetail",
                "Removes func_detail brushes; the leftover geometry affects visibility."),
            _PD("Remove water", "-nowater", "Get rid of water brushes."),
            _PD("Low priority", "-low", "Run as an idle-priority process."),
            _PD("VPROJECT override", "-vproject",
                "Override the VPROJECT environment variable.", True, vfolder=True),
            _PD("Extra search path", "-insert_search_path",
                "Include an extra base directory for mounting additional content.",
                True, vfolder=True),
            _PD("Embed directory", "-embed",
                "Embed the contents of a directory in the pakfile lump.", True, vfolder=True),
            _PD("Instance path", "-instancepath",
                "Overrides the InstancePath key in gameinfo.", True, vfolder=True),
            _PD("No vconfig UI", "-novconfig",
                "Don't bring up graphical UI on vproject errors."),
            _PD("Threads", "-threads",
                "Number of threads VBSP uses (default: CPU cores).", True, vtype="int"),
            _PD("Verbose entities", "-verboseentities",
                "With -v, disables verbose output for submodels."),
            _PD("No welding", "-noweld", "Don't join face vertexes together."),
            _PD("No CSG", "-nocsg", "Don't chop out intersecting brush areas."),
            _PD("No shared edges", "-noshare",
                "Emit unique face edges instead of sharing them."),
            _PD("No t-junction fix", "-notjunc", "Don't fixup any t-junctions."),
            _PD("Nodraw triggers", "-nodrawtriggers",
                "Compile all triggers as if they used nodraw."),
            _PD("No outer-shell optimization", "-noopt",
                "Disables removal of the unseen outer shell faces."),
            _PD("No prune", "-noprune", "Don't prune neighboring solid nodes."),
            _PD("No merge", "-nomerge", "Don't merge chopped faces on nodes."),
            _PD("No water merge", "-nomergewater", "Don't merge chopped faces on water."),
            _PD("No subdivision", "-nosubdiv", "Don't subdivide faces for lightmapping."),
            _PD("Micro volume warning", "-micro",
                "Warn when brushes with a volume below this are output (default 1.0).", True),
            _PD("Full detail", "-fulldetail",
                "Mark all detail geometry as normal geometry."),
            _PD("Leak test", "-leaktest", "Stop processing the map if a leak is detected."),
            _PD("Bump all", "-bumpall",
                "Force all surfaces to be treated as bump mapped."),
            _PD("Snap axial", "-snapaxial", "Snap axial planes to integer coordinates."),
            _PD("Block size", "-blocksize",
                "Size of each grid square VBSP chops the level on (default 1024).",
                True, vtype="int", dval="1024"),
            _PD("Dump static props", "-dumpstaticprops",
                "Dump static props to staticprop###.txt."),
            _PD("Dump collision", "-dumpcollide", "Write files with collision info."),
            _PD("Force sky vis", "-forceskyvis",
                "Enable vis calculations in 3D skybox leaves."),
            _PD("Luxel scale", "-luxelscale",
                "Scale all lightmaps by this amount (default 1.0).", True),
            _PD("Min luxel scale", "-minluxelscale",
                "Limit the minimum luxel scale in the map.", True),
            _PD("Light if missing", "-lightifmissing",
                "Force lightmaps for all surfaces even if they don't need them."),
            _PD("Keep stale zip", "-keepstalezip",
                "Keep the BSP's zip files intact but regenerate everything else."),
            _PD("Vis granularity", "-visgranularity",
                "Force visibility splits N units along X, Y, Z (vector).", True),
            _PD("Replace materials", "-replacematerials",
                "Substitute materials according to cfg/materialsub.cfg."),
            _PD("Full minidumps", "-FullMinidumps", "Write large minidumps on crash."),
            _PD("No virtual mesh", "-novirtualmesh",
                "Fix physics entities falling through displacements at the cost of BSP size."),
            _PD("Xbox mode", "-xbox",
                "Treat %CompileTrigger as nodraw and %CompileWater as %CompileNoLight."),
            _PD("Static prop combine", "-StaticPropCombine",
                "Merge static props per spcombinerules.txt to lower draw calls."),
            _PD("Auto combine rules", "-StaticPropCombine_AutoCombine",
                "Automatically generate static prop combine rules."),
            _PD("Combine consider vis", "-StaticPropCombine_ConsiderVis",
                "Combine all props in a group that share visclusters."),
            _PD("Suggest combine rules", "-StaticPropCombine_SuggestRules",
                "List models sharing a material that belong in spcombinerules.txt."),
            _PD("Min combine instances", "-StaticPropCombine_MinInstances",
                "Minimum props in a combine group required to create a combined prop.",
                True, vtype="int"),
            _PD("Keep sources", "-KeepSources",
                "Don't delete autogenerated QCs and unpacked model files."),
            _PD("Max lightmap dim", "-maxlightmapdim",
                "Maximum size of a brush face lightmap (default 32).", True),
            _PD("Default luxel size", "-defaultluxelsize",
                "Default luxel scale when lightmapscale is 0 or missing (default 16).", True),
            _PD("All Detail", "-alldetail",
                "Make all geometry func_detail to improve VVIS times. Use with a "
                "radius override of 2500 in VVIS.",
                warn="Support varies between versions of source."),
            _PD("Normal Priority", "-normal_priority",
                "Run process with a normal process priority (be default below "
                "normal).. Will prevent process from getting run on an efficiency "
                "core for certain CPUs.",
                warn="Using above normal process priority can cause other applications to become slow/unresponsive"),
            _PD("Verbose (long form)", "-verbose",
                "Turn on verbose output for debug purposes. Same as -v."),
            _PD("Static Prop Combine - Print Combine Rules", "-staticpropcombine_printcombinerules",
                "Prints the combine rules.",
                warn="CS:GO only."),
            _PD("Static Prop Combine - Color Instances", "-staticpropcombine_colorinstances",
                "Instances of combined props get colored.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - Fast Reflection", "-combineignore_fastreflection",
                "Combine props, even if they have differing Render in Fast "
                "Reflections settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - Normals", "-combineignore_normals",
                "Combine props, even if they have differing Ignore Normals "
                "settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - No Shadow", "-combineignore_noshadow",
                "Combine props, even if they have differing Disable Shadows "
                "settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - No Vertex Lighting", "-combineignore_novertexlighting",
                "Combine props, even if they have differing Disable Vertex lighting "
                "settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - No Flashlight", "-combineignore_noflashlight",
                "Combine props, even if they have differing Disable flashlight "
                "settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - No Self Shadowing", "-combineignore_noselfshadowing",
                "Combine props, even if they have differing Disable Self-Shadowing "
                "settings.",
                warn="CS:GO only."),
            _PD("Static Prop Combine Ignore - Disable Shadow Depth", "-combineignore_disableshadowdepth",
                "Combine props, even if they have differing Disable ShadowDepth "
                "settings.",
                warn="CS:GO only."),
        ],
        "toolsplusplus": [
            _PD("No leak test (Tools++)", "-noleaktest",
                "Disables leaktest, which is enabled by default in VBSP++.", tp=True),
            _PD("Max luxel scale (Tools++)", "-maxluxelscale",
                "Clamp the maximum luxel scale (default: no limit).", True, tp=True),
            _PD("No default cubemap (Tools++)", "-nodefaultcubemap",
                "Disables automatic skybox cubemap generation.", tp=True),
            _PD("Allow dynamic props as static (Tools++)", "-allowdynamicpropsasstatic",
                "Allow all models with the 'static' flag in the model viewer to be used on "
                "prop_static, even when their propdata doesn't contain 'allowstatic'.", tp=True),
            _PD("Allow detail cracks (Tools++)", "-allowdetailcracks",
                "Don't fixup t-junctions on func_detail.", tp=True),
            _PD("Cull verts (Tools++)", "-cullverts",
                "Always reduce vertex usage even when the limit isn't exceeded.", tp=True),
            _PD("No bevel (Tools++)", "-nobevel",
                "Disables brushsides for all angled geometry (prefer func_nobevel).", tp=True),
            _PD("No name fixup (Tools++)", "-nonamefixup",
                "Disable default instance name prefixing with InstanceAuto.", tp=True),
            _PD("Singleplayer BSP (Tools++)", "-singleplayer",
                "Compile for the Half-Life 2 / SDK13 singleplayer BSP format.", tp=True),
            _PD("L4D2 BSP (Tools++)", "-l4d2",
                "Compile for the Left 4 Dead 2 BSP format.", tp=True),
            _PD("Portal 2 BSP (Tools++)", "-portal2",
                "Compile for the Portal 2 BSP format.", tp=True),
            _PD("Alien Swarm BSP (Tools++)", "-asw",
                "Compile for Alien Swarm compatible BSP format.", tp=True),
            _PD("CS:GO BSP (Tools++)", "-csgo",
                "Compile for the CS:GO branch BSP format.", tp=True),
            _PD("Warn missing instances (Tools++)", "-warnMissingInstances",
                "Ignore missing instance VMFs (warn only).", tp=True),
            _PD("No HDR cubemaps (Tools++)", "-nohdrcubemap",
                "Don't write HDR cubemaps to the BSP (LDR-only compiles).", tp=True),
            _PD("No LDR cubemaps (Tools++)", "-noldrcubemap",
                "Don't write LDR cubemaps to the BSP.", tp=True),
            _PD("No bump lightmaps (Tools++)", "-nobump",
                "Disables bumpmap lightmaps on all faces.", tp=True),
            _PD("Report search paths (Tools++)", "-report_search_paths",
                "List current search paths for debugging.", tp=True),
            _PD("Missing material (Tools++)", "-missingmaterial",
                "Replace missing brush materials with this material name.", True, tp=True),
            _PD("Force material (Tools++)", "-forcematerial",
                "Force all brush materials to be replaced with this name.", True, tp=True),
            _PD("Entity first (Tools++)", "-entfirst",
                "Force an entity classname to always sort first in the entity list.",
                True, tp=True),
            _PD("Simple ladders (Tools++)", "-simpleladders",
                "Compile func_simpleladder format (L4D2).", tp=True),
            _PD("Static prop format (Tools++)", "-staticpropformat",
                "Static prop format: v6…v14 (default v10).", True, tp=True),
            _PD("Max brushes (Tools++)", "-maxbrushes",
                "Override MAX_MAP_BRUSHES (engine must support higher limits).",
                True, vtype="int", tp=True),
            _PD("Max brushsides (Tools++)", "-maxbrushsides",
                "Override MAX_MAP_BRUSHSIDES.", True, vtype="int", tp=True),
            _PD("Max dispinfo (Tools++)", "-maxdispinfo",
                "Override the displacement limit.", True, vtype="int", tp=True),
            _PD("Max displacement lightmap dimension (Tools++)", "-maxdisplightmapdim",
                "Override the maximum lightmap dimension for displacements.",
                True, vtype="int", tp=True),
            _PD("Max texinfo (Tools++)", "-maxtexinfo",
                "Override the texinfo limit.", True, vtype="int", tp=True),
            _PD("Append content search paths (Tools++)", "-append_search_path",
                "Add an extra search path behind the ones from gameinfo.txt "
                "(multiple paths comma separated).", True, tp=True),
            _PD("Game base directory (Tools++)", "-basedir",
                "Path to the game's base directory, deduced from the mod directory "
                "by default.",
                True, vfolder=True, tp=True),
            _PD("block (Tools++)", "-block",
                "Control the grid size mins/maxs that the map is chopped on. "
                "Compiler default: N N.",
                True, tp=True),
            _PD("blocks (Tools++)", "-blocks",
                "Control the grid dimensions that the map is chopped on. Compiler "
                "default: N N N N.",
                True, tp=True),
            _PD("Box3D physics (Tools++)", "-box3d",
                "Load vphysics_box3d to generate collision data.",
                tp=True),
            _PD("BSP format (Tools++)", "-bspformat",
                "BSP version to write. Supported versions: 19 (HL2), 20 (TF2), 21 "
                "(ASW/L4D2/Portal 2/CS:GO). Compiler default: 20.",
                True, tp=True, vtype="int", dval="20"),
            _PD("csgoclipcontents (Tools++)", "-csgoclipcontents",
                "Compile CS:GO grenadeclip/droneclip contents.",
                tp=True),
            _PD("cullall (Tools++)", "-cullall",
                "Force all forms of culling.",
                tp=True),
            _PD("cullbrushes (Tools++)", "-cullbrushes",
                "Always cull brushes, even if limit isn't exceeded yet.",
                tp=True),
            _PD("cullbrushsides (Tools++)", "-cullbrushsides",
                "Always cull brushsides, even if limit isn't exceeded yet.",
                tp=True),
            _PD("cullplanes (Tools++)", "-cullplanes",
                "Always cull planes, even if limit isn't exceeded yet.",
                tp=True),
            _PD("cullplanesaggressive (Tools++)", "-cullplanesaggressive",
                "Force aggressive plane culling.",
                tp=True),
            _PD("defaultcubemap (Tools++)", "-defaultcubemap",
                "Obsolete (on by default). Generates default LDR and HDR cubemaps.",
                tp=True),
            _PD("dumpstaticprop (Tools++)", "-dumpstaticprop",
                "Dump static props to staticprop*.txt.",
                tp=True),
            _PD("dxlevel (Tools++)", "-dxlevel",
                "Don't include entities whose dxlevels preclude this dxlevel. "
                "Compiler default: 0.",
                True, tp=True, vtype="int", dval="0"),
            _PD("insurgency (Tools++)", "-insurgency",
                "Use Insurgency compatible BSP format.",
                tp=True),
            _PD("lightformat (Tools++)", "-lightformat",
                "World light version to write. Supported versions: 0 (HL2/TF2), 1 "
                "(ASW/L4D2/Portal 2/CS:GO). Compiler default: 0.",
                True, tp=True, vtype="int", dval="0"),
            _PD("materialdebug (Tools++)", "-materialdebug",
                "Print debug information about materials.",
                tp=True),
            _PD("Content mount configuration (Tools++)", "-mountcfg",
                "Path to a mount.cfg listing extra content to mount. Intended for "
                "Garry's Mod. Compiler default: cfg/mount.cfg.",
                True, vfile=True, tp=True, dval="cfg/mount.cfg"),
            _PD("nocull (Tools++)", "-nocull",
                "Don't reduce brushes, brushsides, vertexes or planes.",
                tp=True),
            _PD("nodetailclip (Tools++)", "-nodetailclip",
                "Don't clip detail brush faces with each other.",
                tp=True),
            _PD("nodisp4virtualmesh (Tools++)", "-nodisp4virtualmesh",
                "Enable physics compression for power of 4 displacements.",
                tp=True),
            _PD("noineligiblevertexlitprops (Tools++)", "-noineligiblevertexlitprops",
                "Don't mark bumpmap/phong props as not supporting vertex lighting.",
                tp=True),
            _PD("nophysics (Tools++)", "-nophysics",
                "Don't generate physics collision for the map.",
                tp=True),
            _PD("nostyledlights (Tools++)", "-nostyledlights",
                "Named lights become static lights, or are removed if initially "
                "dark.",
                tp=True),
            _PD("showineligiblevertexlitprops (Tools++)", "-showineligiblevertexlitprops",
                "Show what props got marked as not supporting vertex lighting.",
                tp=True),
        ],
    },
    "VVIS": {
        "parameters": [
            _PD("Fast vis", "-fast",
                "Only do a quick first pass; does not actually test visibility."),
            _PD("Radius override", "-radius_override",
                "Force a maximum vis radius in units.", True, vtype="int"),
            _PD("No sort", "-nosort", "Don't sort portals (an optimization)."),
            _PD("Tmp in", "-tmpin", "Read portals from \\tmp\\<mapname>.prt."),
            _PD("Tmp out", "-tmpout", "Write portals to \\tmp\\<mapname>.prt."),
            _PD("Low priority", "-low", "Run as an idle-priority process."),
            _PD("Threads", "-threads",
                "Override CPU threads (max 16 stock, unlimited in TF2 branch / VVIS++).",
                True, vtype="int"),
            _PD("Verbose", "-v", "Turn on verbose output."),
            _PD("No vconfig UI", "-novconfig",
                "Don't bring up graphical UI on vproject errors."),
            _PD("Normal Priority", "-normal_priority",
                "Run process with a normal process priority (be default below "
                "normal).. Will prevent process from getting run on an efficiency "
                "core for certain CPUs.",
                warn="Using above normal process priority can cause other applications to become slow/unresponsive"),
            _PD("Verbose (long form)", "-verbose",
                "Turn on verbose output for debug purposes. Same as -v."),
        ],
        "toolsplusplus": [
            _PD("Append content search paths (Tools++)", "-append_search_path",
                "Add an extra search path behind the ones from gameinfo.txt. "
                "Multiple paths can be specified with a , (comma)",
                True, tp=True),
            _PD("Game base directory (Tools++)", "-basedir",
                "Path to the game's base directory, deduced from the mod directory "
                "by default.",
                True, vfolder=True, tp=True),
            _PD("Prepend content search paths (Tools++)", "-insert_search_path",
                "Add an extra search path in front of the ones from gameinfo.txt. "
                "Multiple paths can be specified with a , (comma)",
                True, tp=True),
            _PD("Content mount configuration (Tools++)", "-mountcfg",
                "Path to a mount.cfg listing extra content to mount. Intended for "
                "Garry's Mod. Compiler default: cfg/mount.cfg.",
                True, vfile=True, tp=True, dval="cfg/mount.cfg"),
            _PD("report_search_paths (Tools++)", "-report_search_paths",
                "Show search paths for assets.",
                tp=True),
            _PD("trace (Tools++)", "-trace",
                "Writes a linefile that traces the vis from one cluster to another "
                "for debugging map vis. Compiler default: <N> <N>.",
                True, tp=True),
        ],
    },
    "VRAD": {
        "parameters": [
            _PD("LDR", "-ldr", "Compile Standard (Low) Dynamic Range lighting only."),
            _PD("HDR", "-hdr", "Compile High Dynamic Range lighting only."),
            _PD("Both", "-both",
                "Compile both; in VRAD++ LDR data is redirected to HDR at no extra cost."),
            _PD("Fast lighting", "-fast", "Quick low quality lighting for previewing."),
            _PD("Final quality", "-final",
                "High quality sun/indirect lighting (implies -extrasky 16 in stock)."),
            _PD("Extra sky rays", "-extrasky",
                "Trace N times as many rays for indirect light and sky ambient.",
                True, vtype="int"),
            _PD("Extra RAD file", "-lights",
                "Load a custom .rad file in addition to lights.rad.", True, vfile=True),
            _PD("Bounces", "-bounce",
                "Maximum number of light ray bounces (default 100).", True, vtype="int"),
            _PD("Smoothing angle", "-smooth",
                "Phong smoothing threshold in degrees (default 45).", True, vtype="int"),
            _PD("Luxel density", "-luxeldensity",
                "Scale down all luxels (default/max 1).", True),
            _PD("Soft sun", "-softsun",
                "Treat the sun as an area light of N degrees (soft shadows).", True),
            _PD("Static prop lighting", "-StaticPropLighting",
                "Per-vertex lighting on prop_static (slow with many props)."),
            _PD("Static prop polys", "-StaticPropPolys",
                "Use real meshes instead of collision meshes for prop shadows."),
            _PD("Texture shadows", "-TextureShadows",
                "Cast texture shadows from $alphatest/$translucent prop surfaces.",
                warn="Models must be compiled with $casttextureshadows or listed with "
                     "forcetextureshadow in the lights file."),
            _PD("Low priority", "-low", "Run as a low-priority process."),
            _PD("Threads", "-threads", "Override the number of CPU threads used.",
                True, vtype="int"),
            _PD("No supersampling", "-noextra",
                "Disable supersampling (blockier lighting)."),
            _PD("Chop", "-chop",
                "Smallest luxel widths for a bounce patch on edges (default 4).",
                True, vtype="int"),
            _PD("Max chop", "-maxchop",
                "Coarsest luxel widths for a patch in face interiors (default 4).",
                True, vtype="int"),
            _PD("Disp chop", "-dispchop",
                "Smallest acceptable luxel width of a displacement patch (default 8).",
                True, vtype="int"),
            _PD("Large disp sample radius", "-LargeDispSampleRadius",
                "Gather bounced light across a wider area on displacements."),
            _PD("Fast ambient", "-fastambient",
                "Low quality per-leaf ambient sampling to save time."),
            _PD("No detail light", "-nodetaillight", "Don't light detail props."),
            _PD("No skybox recurse", "-noskyboxrecurse",
                "Turn off recursion into the 3D skybox."),
            _PD("No prop self-shadow", "-nossprops",
                "Globally disable self-shadowing on static props."),
            _PD("Only static props", "-OnlyStaticProps",
                "Only perform direct static prop lighting."),
            _PD("Dump prop lightmaps", "-dumppropmaps",
                "Dump computed static prop lightmaps as TGA files."),
            _PD("Verbose", "-v", "Turn on verbose output."),
            _PD("No vconfig UI", "-novconfig",
                "Don't bring up graphical UI on vproject errors."),
            _PD("AO scale", "-aoscale",
                "Intensity multiplier for simulated/baked ambient occlusion "
                "(default 0.5 in ++).", True),
            _PD("AO radius", "-aoradius",
                "Radius of simulated ambient occlusion (default 32 in ++).", True),
            _PD("Static prop sample scale", "-StaticPropSampleScale",
                "Extra sampling factor for prop vertex lighting.", True,
                warn="Note that -final is the same as -StaticPropSampleScale 16."),
            _PD("Prop bounces", "-StaticPropBounce",
                "Number of static prop light bounces to simulate (default 0).",
                True, vtype="int"),
            _PD("Lightmap format", "-lightmapformat",
                "Lightmap format, e.g. rgbea / rgbeastyle3 (CS:GO branch).", True),
            _PD("Static Prop Lighting Final", "-StaticPropLightingFinal",
                "Generate per-vertex prop_static lighting (uses higher/final "
                "quality processing)",
                warn="CS:GO only. always enabled when compiling with -final"),
            _PD("Static Prop Lighting Old", "-StaticPropLightingOld",
                "Generate per-vertex prop_static lighting using the old algorithm.",
                warn="CS:GO only."),
            _PD("Normal Priority", "-normal_priority",
                "Run process with a normal process priority (be default below "
                "normal).. Will prevent process from getting run on an efficiency "
                "core for certain CPUs.",
                warn="Using above normal process priority can cause other applications to become slow/unresponsive"),
            _PD("Verbose (long form)", "-verbose",
                "Turn on verbose output for debug purposes. Same as -v."),
        ],
        "toolsplusplus": [
            _PD("CPU only (Tools++)", "-cpu",
                "Disable the GPU raytracer and force CPU raytracing.", tp=True),
            _PD("GPU software RT (Tools++)", "-gpusoftwarert",
                "Use software raytracing on GPUs without RT support (may be slower).", tp=True),
            _PD("Ambient occlusion (Tools++)", "-ambientocclusion",
                "Enable baked AO (alias -ao); fast custom implementation.", tp=True),
            _PD("AO prop samples (Tools++)", "-aopropsamples",
                "AO samples for static props (0 disables). 12/16/32 on fast/normal/final.",
                True, vtype="int", tp=True),
            _PD("AO face samples (Tools++)", "-aofacesamples",
                "AO samples for brushes/displacements (0 disables). "
                "16/32/96 on fast/normal/final.", True, vtype="int", tp=True),
            _PD("AO debug (Tools++)", "-aodebug",
                "Only write ambient occlusion to the lightmaps.", tp=True),
            _PD("Prop alpha (Tools++)", "-propalpha",
                "Write sun contribution amount to prop vertex lighting alpha.", tp=True),
            _PD("Extra soft (Tools++)", "-extrasoft",
                "Sample multiplier for soft lights (_light_radius). Compiler default: 1.",
                True, tp=True),
            _PD("Reflectivity scale (Tools++)", "-reflectivityscale",
                "Scale the reflectivity of all surfaces for bounced lighting.", True, tp=True),
            _PD("Global reflectivity scale (Tools++)", "-globalreflectivityscale",
                "Scale bounced light on props and ambient cubes alongside brushes.",
                True, tp=True),
            _PD("World texture shadows (Tools++)", "-worldtextureshadows",
                "Let $alphatest brush textures cast texture shadows "
                "(needs -textureshadows).", tp=True),
            _PD("Translucent shadows (Tools++)", "-translucentshadows",
                "Extend world texture shadows to $translucent surfaces.", tp=True),
            _PD("Force texture shadows (Tools++)", "-forcetextureshadows",
                "Force texture shadows on all props.", tp=True),
            _PD("No disp supersampling (Tools++)", "-noextradisp",
                "Disable displacement supersampling (old behavior).", tp=True),
            _PD("No geometric edge pairing (Tools++)", "-nopairgeometricedges",
                "Disable seam-fixing edge pairing between brush entities.", tp=True),
            _PD("No edge pairing (Tools++)", "-nopairedges",
                "Disable edge pairing on the same entity.", tp=True),
            _PD("No light culling (Tools++)", "-nolightcull",
                "Disable the light culling optimization (use if lighting looks wrong).", tp=True),
            _PD("Min light cull intensity (Tools++)", "-minlightcullintensity",
                "Intensity cutoff threshold for light culling (default 0.01).", True, tp=True),
            _PD("Prop ambient cubes (Tools++)", "-propambient",
                "Static props shadow ambient cubes (experimental).", tp=True),
            _PD("Patch lightmap size (Tools++)", "-patchlightmapsize",
                "Minimum lightmap size for patches. Compiler default: 16.",
                True, vtype="int", tp=True),
            _PD("Chop texlights (Tools++)", "-choptexlights",
                "Subdivide faces with texture lights for a smoother result.", tp=True),
            _PD("Fast radial (Tools++)", "-fastradial",
                "Use fast radiosity gathering like in -fast mode.", tp=True),
            _PD("Disp sample factor (Tools++)", "-dispsamplefactor",
                "Displacement lighting influence multiplier (default 1).", True, tp=True),
            _PD("Disp patch sample factor (Tools++)", "-disppatchsamplefactor",
                "Displacement bounced light influence multiplier (default 1).", True, tp=True),
            _PD("Indirect only (Tools++)", "-indirectonly",
                "Only bake indirect light.", tp=True),
            _PD("Report search paths (Tools++)", "-report_search_paths",
                "List current search paths for debugging.", tp=True),
            _PD("Prop debug (Tools++)", "-propdebug",
                "Track which props take a long time to compile.", tp=True),
            _PD("Prop indirect mode (Tools++)", "-StaticPropIndirectMode",
                "0 CSGO-style, 1 HL2-style (dark), 2 Orange Box-style (bright).",
                True, vtype="int", tp=True),
            _PD("Old disp lighting (Tools++)", "-DisplacementLightingOld",
                "Use the old displacement bounced lighting calculations.", tp=True),
            _PD("Minimum ambient light (Tools++)", "-ambient",
                "Apply a minimum ambient lighting value to the map. Compiler "
                "default: 0 0 0.",
                True, tp=True, dval="0 0 0"),
            _PD("Ambient occlusion (alias) (Tools++)", "-ao",
                "Same as -ambientocclusion.",
                tp=True),
            _PD("Append content search paths (Tools++)", "-append_search_path",
                "Add an extra search path behind the ones from gameinfo.txt. "
                "Multiple paths can be specified with a , (comma)",
                True, tp=True),
            _PD("Game base directory (Tools++)", "-basedir",
                "Path to the game's base directory, deduced from the mod directory "
                "by default.",
                True, vfolder=True, tp=True),
            _PD("Box3D physics (Tools++)", "-box3d",
                "Load vphysics_box3d to trace collision data.",
                tp=True),
            _PD("centersamples (Tools++)", "-centersamples",
                "Move sample centers.",
                tp=True),
            _PD("coring (Tools++)", "-coring",
                "Light threshold to force to blackness (minimizes lightmaps) "
                "Compiler default: 1.",
                True, tp=True, vtype="int", dval="1"),
            _PD("debugextra (Tools++)", "-debugextra",
                "Places debugging data in lightmaps to visualize supersampling.",
                tp=True),
            _PD("disppatchradius (Tools++)", "-disppatchradius",
                "Maximum radius allowed for displacement patches. Used in face "
                "interiors. Compiler default: 1500.",
                True, tp=True, vtype="int", dval="1500"),
            _PD("dlight (Tools++)", "-dlight",
                "Minimum light threshold for creating texlights. Compiler default: "
                "0.1.",
                True, tp=True, dval="0.1"),
            _PD("Sky light contribution (Tools++)", "-sky",
                "Set ambient sunlight contribution in shaded areas.",
                True, tp=True),
            _PD("dlightmap (Tools++)", "-dlightmap",
                "Move sample centers.",
                tp=True),
            _PD("dump (Tools++)", "-dump",
                "Write patches to debug files.",
                tp=True),
            _PD("dumpnormals (Tools++)", "-dumpnormals",
                "Write normals to debug files.",
                tp=True),
            _PD("dumptrace (Tools++)", "-dumptrace",
                "Write ray-tracing environment to debug files.",
                tp=True),
            _PD("extendedsoftlimits (Tools++)", "-extendedsoftlimits",
                "Disables warning about exceeding soft limits.",
                tp=True),
            _PD("GPU submission time (Tools++)", "-gpusubmit",
                "How long one GPU submission may run, in seconds. Lower it if the "
                "display driver is timing out. Compiler default: 0.5.",
                True, tp=True, dval="0.5"),
            _PD("Prepend content search paths (Tools++)", "-insert_search_path",
                "Add an extra search path in front of the ones from gameinfo.txt. "
                "Multiple paths can be specified with a , (comma)",
                True, tp=True),
            _PD("loghash (Tools++)", "-loghash",
                "Log the sample hash table to samplehash.txt.",
                tp=True),
            _PD("maxbrushes (Tools++)", "-maxbrushes",
                "Set maximum brush limit. Compiler default: 8192.",
                True, tp=True, vtype="int", dval="8192"),
            _PD("maxbrushsides (Tools++)", "-maxbrushsides",
                "Set maximum brushside limit. Compiler default: 65536.",
                True, tp=True, vtype="int", dval="65536"),
            _PD("maxdispinfo (Tools++)", "-maxdispinfo",
                "Set maximum displacement limit. Compiler default: 2048.",
                True, tp=True, vtype="int", dval="2048"),
            _PD("maxdispsamplesize (Tools++)", "-maxdispsamplesize",
                "Set max displacement sample size. Compiler default: 512.",
                True, tp=True, vtype="int", dval="512"),
            _PD("maxtexinfo (Tools++)", "-maxtexinfo",
                "Set maximum texinfo limit. Compiler default: 12288.",
                True, tp=True, vtype="int", dval="12288"),
            _PD("minstyledlightcullintensity (Tools++)", "-minstyledlightcullintensity",
                "Minimum light threshold where styled (i.e. named) lights should be "
                "cutoff. Lowering this can increase lightmap filesize and lower "
                "in-game performance. Compiler default: 0.5.",
                True, tp=True, dval="0.5"),
            _PD("Content mount configuration (Tools++)", "-mountcfg",
                "Path to a mount.cfg listing extra content to mount. Intended for "
                "Garry's Mod. Compiler default: cfg/mount.cfg.",
                True, vfile=True, tp=True, dval="cfg/mount.cfg"),
            _PD("nostyledlights (Tools++)", "-nostyledlights",
                "Named lights become static lights, or are removed if initially "
                "dark.",
                tp=True),
            _PD("notexscale (Tools++)", "-notexscale",
                "Disable scaling texlights based on texture scale.",
                tp=True),
            _PD("notextureshadows (Tools++)", "-notextureshadows",
                "Undo every texture shadow option.",
                tp=True),
            _PD("onlydetail (Tools++)", "-onlydetail",
                "Only light detail props and per-leaf lighting.",
                tp=True),
            _PD("rederrors (Tools++)", "-rederrors",
                "Show radial light errors as black instead of red.",
                tp=True),
            _PD("scale (Tools++)", "-scale",
                "Intensity multiplier of all lights. Compiler default: 1.",
                True, tp=True, vtype="int", dval="1"),
            _PD("skipAmbientLights (Tools++)", "-skipAmbientLights",
                "Skip ambient lights.",
                tp=True),
            _PD("skipLocalLights (Tools++)", "-skipLocalLights",
                "Skip point and spotlights for baking.",
                tp=True),
            _PD("skipPVS (Tools++)", "-skipPVS",
                "Skip PVS optimization for lights.",
                tp=True),
            _PD("skipSunLights (Tools++)", "-skipSunLights",
                "Skip sun lights.",
                tp=True),
            _PD("StaticPropNormals (Tools++)", "-StaticPropNormals",
                "When lighting static props, just show their normal vector.",
                tp=True),
            _PD("StopOnExit (Tools++)", "-StopOnExit",
                "Wait for a keypress on exit.",
                tp=True),
            _PD("supportslightdirectional (Tools++)", "-supportslightdirectional",
                "Disables classname renaming of light_directional.",
                tp=True),
            _PD("supportslightprojected (Tools++)", "-supportslightprojected",
                "Disables classname renaming of light_projected.",
                tp=True),
        ],
    },
    "PACK": {
        "parameters": [
            _PD("Detailed Report", "-detailedreport",
                "Outputs a detailed report of packed assets"),
            _PD("Verbose", "-verbose",
                "Outputs a complete listing of added assets"),
            _PD("Dryrun", "-dryrun",
                "Creates a txt file for bspzip usage but does not pak"),
            _PD("Rename Nav File", "-renamenav",
                "Renames the nav file to embed.nav", games=[240, 300, 440]),
            _PD("Rename Particle Manifest File", "-renameparticlemanifest",
                "Renames the particle manifest file to particles.txt",
                games=[240, 300, 440]),
            _PD("Include", "-include", "Packs specified file",
                True, vfile=True, multi=True),
            _PD("Include Directory", "-includedir",
                "Packs all files in specified directory",
                True, vfile=True, vfolder=True, multi=True),
            _PD("Exclude", "-exclude", "Ignores specified file",
                True, vfile=True, multi=True),
            _PD("Exclude Directory", "-excludedir",
                "Ignores this folder when looking for assets",
                True, vfile=True, vfolder=True, multi=True),
            _PD("Exclude VPK Files", "-excludevpk",
                "Exclude files from packing that are already in the specified VPK",
                True, vfile=True, multi=True),
            _PD("VPK", "-vpk", "Pack into a VPK instead of BSP.",
                games=[550, 620]),
            _PD("Addon Info", "-ainfo", "Packs specified file as addoninfo.txt",
                True, vfile=True, games=[550, 620]),
            _PD("Include File List", "-includefilelist",
                "Packs all files in the file list",
                True, vfile=True, multi=True),
            _PD("Add Source Directory", "-sourcedirectory",
                "Adds an additional content search directory when looking for content to pack",
                True, vfile=True, vfolder=True, multi=True),
            _PD("No .sw.vtx", "-noswvtx",
                "Skip packing unused .sw.vtx files to save filesize."),
        ],
        "toolsplusplus": [],
    },
    "BSPZIP": {
        "parameters": [
            _PD("Compress (LZMA)", "-compress",
                "Compress the BSP with LZMA when repacking."),
            _PD("Delete Cubemaps", "-deletecubemaps",
                "Deletes all .vtf files in the map."),
        ],
        "toolsplusplus": [
            _PD("Verbose (Tools++)", "-verbose",
                "More detailed information (BSPZIP++).", tp=True),
            _PD("Threads (Tools++)", "-threads",
                "Compression threads (BSPZIP++).", True, vtype="int", tp=True),
        ],
    },
    "COPY": {"parameters": [], "toolsplusplus": []},
    "NAV": {
        "parameters": [
            _PD("Cheats", "+sv_cheats",
                "Required for nav generation in most games (passed as +sv_cheats 1).",
                True, vtype="int", dval="1", den=True),
            _PD("Windowed", "-windowed", "Run the game in a window."),
            _PD("Skip intro", "-novid", "Skip the startup video."),
            _PD("Window width", "-w", "Window width in pixels.",
                True, vtype="int", dval="1280"),
            _PD("Window height", "-h", "Window height in pixels.",
                True, vtype="int", dval="720"),
            _PD("Hidden", "-hidden",
                "Runs the game client off-screen during compile."),
            _PD("Text Mode", "-textmode",
                "Runs the game client in text mode only."),
            _PD("No Sound", "-nosound",
                "Disables sound.",
                warn="Can cause crashes on certain engine branches"),
        ],
        "toolsplusplus": [],
    },
    "CUBEMAPS": {
        "parameters": [
            _PD("Windowed", "-windowed", "Run the game in a window.", den=True),
            _PD("Skip intro", "-novid", "Skip the startup video.", den=True),
            _PD("Window width", "-w", "Window width in pixels.",
                True, vtype="int", dval="1280", den=True),
            _PD("Window height", "-h", "Window height in pixels.",
                True, vtype="int", dval="720", den=True),
            _PD("Hidden", "-hidden",
                "Runs the game client off-screen during compile."),
            _PD("No Sound", "-nosound",
                "Disables sound.",
                warn="Can cause crashes on certain engine branches"),
            _PD("Iterations", "-iterations",
                "Number of times to build cubemaps. Useful for recursive "
                "reflections.",
                True, vtype="int", dval="1"),
        ],
        "toolsplusplus": [],
    },
    "GAME": {
        "parameters": [
            _PD("Cheats", "+sv_cheats",
                "Enables game cheats (passed as +sv_cheats 1). "
                "Dangerous to use in multiplayer games.", True, vtype="int", dval="1"),
            _PD("Windowed", "-windowed", "Run the game in a window."),
            _PD("Skip intro", "-novid", "Skip the startup video."),
            _PD("Window width", "-w", "Window width in pixels.",
                True, vtype="int", dval="1280"),
            _PD("Window height", "-h", "Window height in pixels.",
                True, vtype="int", dval="720"),
            _PD("Tools", "-tools",
                "Enables engine tools.",
                warn="Can cause lighting errors in game."),
            _PD("Developer Mode", "-dev",
                "Disables the automatic loading of menu background maps and stops "
                "the quit dialog from appearing on exit."),
            _PD("Console", "-console",
                "Starts the game with the console enabled."),
            _PD("Window Width", "-width",
                "Sets window resolution width.",
                True, vtype="int", dval="1280"),
            _PD("Window Height", "-height",
                "Sets window resolution height.",
                True, vtype="int", dval="720"),
            _PD("No border", "-noborder",
                "Removes the window border in windowed mode."),
            _PD("Hijack", "-hijack",
                "Hijacks the current game process rather than launching a second "
                "one."),
            _PD("Insecure", "-insecure",
                "Runs the game in insecure mode.",
                warn="May cause issues with multiplayer servers."),
            _PD("Make Dev Shots", "-makedevshots",
                "Takes point_devshot_camera screenshots automatically.",
                warn="-dev recommended to be off."),
            _PD("Disable Freeze Time", "+mp_enableroundwaittime",
                "Disables the freeze time at the start of rounds.",
                True, vtype="int", dval="0"),
        ],
        "toolsplusplus": [],
    },
    "CUSTOM": {"parameters": [], "toolsplusplus": []},
    "SHUTDOWN": {
        "parameters": [
            _PD("Delay seconds", "--delay-seconds",
                "Seconds to wait before shutting the system down.",
                True, vtype="int", dval="5", den=True),
        ],
        "toolsplusplus": [],
    },
}


# Compiler default values for VRAD options that take a value. Without them an option
# ticked in the UI had an empty value (spin box showed 0) and was silently left out
# of the command line.
_VRAD_VALUE_DEFAULTS: Dict[str, str] = {
    "-extrasky": "1", "-bounce": "100", "-smooth": "45", "-luxeldensity": "1",
    "-softsun": "0", "-chop": "4", "-maxchop": "4", "-dispchop": "8",
    "-aoscale": "0.5", "-aoradius": "40", "-StaticPropSampleScale": "4",
    "-StaticPropBounce": "0", "-lightmapformat": "rgbe", "-aopropsamples": "16",
    "-aofacesamples": "32", "-extrasoft": "1", "-reflectivityscale": "1",
    "-globalreflectivityscale": "1", "-minlightcullintensity": "0.01",
    "-patchlightmapsize": "16", "-dispsamplefactor": "1",
    "-disppatchsamplefactor": "1", "-StaticPropIndirectMode": "1",
}
for _section in DEFAULT_PARAMETER_FILES["VRAD"].values():
    for _entry in _section:
        if _entry["CanHaveValue"] and not _entry["DefaultValue"]:
            _entry["DefaultValue"] = _VRAD_VALUE_DEFAULTS.get(_entry["Parameter"], "")


PARAMS_SCHEMA = 4  # bump when the embedded catalog changes: refreshes config/parameters/*.json
PARAMS_SCHEMA_FILE: Path = PARAMETERS_DIR / ".schema"


def ensure_parameter_files() -> None:
    """Create config/parameters/<PROC>/{parameters,toolsplusplus}.json.
    When PARAMS_SCHEMA changes, old files are kept as *.json.bak and rewritten
    (hand edits can be copied back from the .bak files)."""
    PARAMETERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        stale = int(PARAMS_SCHEMA_FILE.read_text(encoding="utf-8").strip() or 0) != PARAMS_SCHEMA
    except (OSError, ValueError):
        stale = True
    for kind in PROCESS_ORDER:
        folder = PARAMETERS_DIR / kind.value
        folder.mkdir(parents=True, exist_ok=True)
        # NOTE: DEFAULT_PARAMETER_FILES is keyed by enum *name* (CUBEMAPS), while the
        # folder uses the enum *value* (Cubemaps) - looking up by value returned
        # empty defaults for Cubemaps / Custom / Shutdown.
        defaults = DEFAULT_PARAMETER_FILES.get(kind.name,
                                               {"parameters": [], "toolsplusplus": []})
        for fname in ("parameters.json", "toolsplusplus.json"):
            target = folder / fname
            if target.exists() and not stale:
                continue
            try:
                if target.exists():
                    shutil.copy2(target, target.with_name(fname + ".bak"))
                target.write_text(
                    json.dumps(defaults.get(fname.replace(".json", ""), []),
                               indent=2, ensure_ascii=False),
                    encoding="utf-8")
                logger.info("Wrote default parameter file %s", target)
            except OSError as e:
                logger.error("Failed to write %s: %s", target, e)
    if stale:
        try:
            PARAMS_SCHEMA_FILE.write_text(str(PARAMS_SCHEMA), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write %s: %s", PARAMS_SCHEMA_FILE, e)


@dataclass
class ParameterSpec:
    name: str
    flag: str
    desc: str
    kind: str = "flag"  # "flag" | "string" | "int" | "file" | "dir"
    plus_only: bool = False
    warning: str = ""
    default: Any = True
    default_enabled: bool = False
    multi: bool = False                              # CanBeUsedMoreThanOnce
    games: Optional[List[int]] = None                # CompatibleGames (Steam app ids)


def _kind_from_json(p: Dict[str, Any]) -> str:
    if not p.get("CanHaveValue", False):
        return "flag"
    if p.get("ValueIsFolder", False):   # checked first: folder params also set ValueIsFile
        return "dir"
    if p.get("ValueIsFile", False):
        return "file"
    return "int" if p.get("ValueType") == "int" else "string"


def _spec_from_json(p: Dict[str, Any], plus_only: bool) -> ParameterSpec:
    kind = _kind_from_json(p)
    return ParameterSpec(
        name=p.get("Name", p.get("Parameter", "").strip()),
        flag=p.get("Parameter", "").strip(),
        desc=p.get("Description", ""),
        kind=kind,
        plus_only=plus_only or bool(p.get("ToolsPlusPlusOnly", False)),
        warning=p.get("Warning", "") or "",
        default=True if kind == "flag" else (p.get("DefaultValue") or ""),
        default_enabled=bool(p.get("DefaultEnabled", False)),
        multi=bool(p.get("CanBeUsedMoreThanOnce", False)),
        games=[int(g) for g in p["CompatibleGames"]] if p.get("CompatibleGames") else None,
    )


def load_parameters_for_process(kind: ProcessKind) -> List[ParameterSpec]:
    """Read parameters.json + toolsplusplus.json (CompilePal++ layout)."""
    folder = PARAMETERS_DIR / kind.value
    specs: List[ParameterSpec] = []
    base = folder / "parameters.json"
    if base.exists():
        try:
            data = json.loads(base.read_text(encoding="utf-8"))
            specs.extend(_spec_from_json(p, False) for p in data)
        except (OSError, ValueError) as e:
            logger.error("Failed to load %s: %s", base, e)
    plus = folder / "toolsplusplus.json"
    if plus.exists():
        try:
            data = json.loads(plus.read_text(encoding="utf-8"))
            for p in data:
                spec = _spec_from_json(p, True)
                idx = next((i for i, s in enumerate(specs)
                            if s.flag.lower() == spec.flag.lower()), None)
                if idx is None:
                    specs.append(spec)
                else:
                    # Same flag in both files: the base entry stays usable with stock
                    # compilers (it must NOT become Tools++-only, otherwise -final,
                    # -both, -fast, -bounce... are dropped from the command line).
                    old = specs[idx]
                    if old.kind != "flag" and old.default in ("", None) and spec.default:
                        old.default = spec.default
                    if not old.warning and spec.warning:
                        old.warning = spec.warning
        except (OSError, ValueError) as e:
            logger.error("Failed to load %s: %s", plus, e)
    if kind in (ProcessKind.VBSP, ProcessKind.VVIS, ProcessKind.VRAD):
        # Compiler flags are switched on only by presets (e.g. "Full - Both"),
        # never globally via DefaultEnabled.
        for sp in specs:
            sp.default_enabled = False
    return specs


ensure_parameter_files()
PARAM_CATALOG: Dict[ProcessKind, List[ParameterSpec]] = {
    kind: load_parameters_for_process(kind) for kind in PROCESS_ORDER
}


def spec_for(kind: ProcessKind, flag: str) -> Optional[ParameterSpec]:
    low = flag.strip().lower()
    return next((s for s in PARAM_CATALOG.get(kind, []) if s.flag.lower() == low), None)


@dataclass
class GameConfig:
    name: str
    path: Path
    appid: Optional[int] = None
    tools_plus_plus: bool = False
    vbsp_path: Optional[Path] = None
    vvis_path: Optional[Path] = None
    vrad_path: Optional[Path] = None
    bspzip_path: Optional[Path] = None
    game_exe: Optional[Path] = None

    def executable_for(self, kind: ProcessKind) -> Optional[Path]:
        return {
            ProcessKind.VBSP: self.vbsp_path,
            ProcessKind.VVIS: self.vvis_path,
            ProcessKind.VRAD: self.vrad_path,
            ProcessKind.PACK: self.bspzip_path,
            ProcessKind.BSPZIP: self.bspzip_path,
            ProcessKind.CUBEMAPS: self.game_exe,
            ProcessKind.NAV: self.game_exe,
            ProcessKind.GAME: self.game_exe,
        }.get(kind)

    def plus_for(self, kind: ProcessKind) -> bool:
        """True when Tools++ options may be passed to this process' executable
        (e.g. vrad++.exe). Compilers that are stock builds must not get them."""
        exe = {
            ProcessKind.VBSP: self.vbsp_path,
            ProcessKind.VVIS: self.vvis_path,
            ProcessKind.VRAD: self.vrad_path,
            ProcessKind.PACK: self.bspzip_path,
            ProcessKind.BSPZIP: self.bspzip_path,
        }.get(kind)
        if exe is not None:
            return exe.stem.endswith("++")
        return self.tools_plus_plus

    @property
    def maps_dir(self) -> Path:
        """Folder where the game expects compiled maps (COPY stage target)."""
        return self.path / "maps"


@dataclass
class MapFile:
    path: Path
    enabled: bool = True


@dataclass
class Parameter:
    name: str
    value: Any
    enabled: bool = True
    kind: str = "string"  # "flag" | "string" | "int" | "file" | "dir"


@dataclass
class ProcessConfig:
    kind: ProcessKind
    enabled: bool = True
    parameters: List[Parameter] = field(default_factory=list)
    custom_executable: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "enabled": self.enabled,
            "parameters": [asdict(p) for p in self.parameters],
            "custom_executable": self.custom_executable,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessConfig":
        proc = cls(
            kind=ProcessKind(data["kind"]),
            enabled=data.get("enabled", True),
            parameters=[Parameter(**p) for p in data.get("parameters", [])],
            custom_executable=data.get("custom_executable"),
        )
        return normalize_process(proc, seed_defaults=False)

    def option_pairs(self, allow_plus: bool = True) -> List[Tuple[str, Optional[str]]]:
        """(flag, value) pairs for enabled params; value is None for plain flags.
        Parameters that may be used more than once (CanBeUsedMoreThanOnce) hold
        several values separated by ';' and yield one pair per value."""
        pairs: List[Tuple[str, Optional[str]]] = []
        for p in self.parameters:
            if not p.enabled:
                continue
            sp = spec_for(self.kind, p.name)
            if sp is not None and sp.plus_only and not allow_plus:
                continue
            if p.kind == "flag" or isinstance(p.value, bool):
                if p.value:
                    pairs.append((p.name, None))
            elif p.value is not None and str(p.value) != "":
                if sp is not None and sp.multi:
                    pairs += [(p.name, v.strip()) for v in str(p.value).split(";") if v.strip()]
                else:
                    pairs.append((p.name, str(p.value)))
        return pairs

    def enabled_options(self, allow_plus: bool = True) -> List[str]:
        """Rendered option tokens ('-flag' or '-opt value') for enabled params."""
        opts: List[str] = []
        for name, value in self.option_pairs(allow_plus):
            opts.append(name)
            if value is not None:
                opts.append(value)
        return opts

    def build_args(self, vmf_path: Optional[Path], bsp_path: Optional[Path],
                   game_dir: Optional[Path], allow_plus: bool = True) -> List[str]:
        """Command line. Source tools take the map name LAST and need
        '-game <dir>'. In Custom parameters $map, $bsp and $game are substituted.
        Tools++-only options are skipped when allow_plus is False."""
        subs = {
            "$map": str(vmf_path) if vmf_path else "",
            "$bsp": str(bsp_path) if bsp_path else "",
            "$game": str(game_dir) if game_dir else "",
        }

        def sub(text: str) -> str:
            for key, val in subs.items():
                text = text.replace(key, val)
            return text

        opts = [sub(o) for o in self.enabled_options(allow_plus)]

        target = bsp_path or vmf_path
        base = str(target.with_suffix("")) if target else ""
        map_name = target.stem if target else ""
        game = ["-game", str(game_dir)] if game_dir else []

        if self.kind in (ProcessKind.VBSP, ProcessKind.VVIS, ProcessKind.VRAD):
            return game + opts + ([base] if base else [])
        if self.kind == ProcessKind.BSPZIP:
            return ["-repack"] + game + opts + ([str(bsp_path)] if bsp_path else [])
        if self.kind == ProcessKind.CUBEMAPS:
            return game + opts + ["-buildcubemaps"] + (["+map", map_name] if map_name else [])
        if self.kind == ProcessKind.GAME:
            return game + (["+map", map_name] if map_name else []) + opts
        if self.kind == ProcessKind.NAV:
            return (game + (["+map", map_name] if map_name else []) + opts
                    + ["+nav_generate", "+nav_save", "+quit"])
        if self.kind in (ProcessKind.COPY, ProcessKind.PACK):
            return []   # COPY is internal; PACK runs CompileWorker._pack_bsp
        return opts

    def preview_command(self, allow_plus: bool = True) -> str:
        """Human-readable command line with placeholders, like CompilePal++'s
        basis string preview ($game$ / $map$ / $bsp$ are substituted at run)."""
        opts = self.enabled_options(allow_plus)
        kind = self.kind
        if kind == ProcessKind.COPY:
            return "copy $bsp$ → $mapsDir$"
        if kind == ProcessKind.SHUTDOWN:
            delay = next((str(p.value) for p in self.parameters
                          if p.name == "--delay-seconds" and p.enabled), "5")
            return f"shutdown in {delay}s"
        if kind == ProcessKind.CUSTOM:
            return " ".join(opts) if opts else "(custom executable, no arguments)"
        if kind == ProcessKind.PACK:
            return " ".join(["-game $game$ -bsp $bsp$ -bspZip $bspZip$ -keys $keys$", *opts])
        parts: List[str] = []
        if kind == ProcessKind.BSPZIP:
            parts.append("-repack")
        parts.extend(opts)
        if kind in (ProcessKind.VBSP, ProcessKind.VVIS):
            parts.append("-game $game$ $map$")
        elif kind == ProcessKind.VRAD:
            parts.append("-game $game$ $bsp$")
        elif kind == ProcessKind.BSPZIP:
            parts.append("-game $game$ $bsp$")
        elif kind == ProcessKind.CUBEMAPS:
            parts.append("-game $game$ -buildcubemaps +map $mapname$")
        elif kind == ProcessKind.GAME:
            parts.append("-game $game$ +map $mapname$")
        elif kind == ProcessKind.NAV:
            parts.append("-game $game$ +map $mapname$ +nav_generate +nav_save +quit")
        return " ".join(parts)


def normalize_process(proc: ProcessConfig, seed_defaults: bool = True) -> ProcessConfig:
    """Ensure every catalog option of this process kind exists exactly once
    (flag names matched case-insensitively); unknown/custom parameters are kept
    at the end. Missing options are added disabled; DefaultEnabled is applied
    only when seed_defaults is True (new processes) - never when loading a
    saved preset, otherwise new/missing flags leak into every preset."""
    specs = PARAM_CATALOG.get(proc.kind, [])
    if not specs:
        return proc
    by_name: Dict[str, Parameter] = {}
    for p in proc.parameters:
        by_name.setdefault(p.name.strip().lower(), p)
    out: List[Parameter] = []
    for sp in specs:
        p = by_name.pop(sp.flag.lower(), None)
        if p is None:
            value = True if sp.kind == "flag" else sp.default
            p = Parameter(sp.flag, value,
                          enabled=bool(seed_defaults and sp.default_enabled), kind=sp.kind)
        else:
            p.name = sp.flag
            if sp.kind == "flag":
                p.value = True
            elif isinstance(p.value, bool) or p.value is None:
                p.value = sp.default
            p.kind = sp.kind
        out.append(p)
    out.extend(by_name.values())
    proc.parameters = out
    return proc


def make_process(kind: ProcessKind) -> ProcessConfig:
    return normalize_process(ProcessConfig(kind=kind))


@dataclass
class Preset:
    name: str
    processes: List[ProcessConfig] = field(default_factory=list)
    map_regex: Optional[str] = None  # optional: only matching maps are compiled

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name,
                "processes": [p.to_dict() for p in self.processes],
                "map_regex": self.map_regex}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Preset":
        return cls(name=data["name"],
                   processes=[ProcessConfig.from_dict(p) for p in data.get("processes", [])],
                   map_regex=data.get("map_regex"))

    def filter_maps(self, maps: List[Path]) -> List[Path]:
        if not self.map_regex:
            return maps
        try:
            rx = re.compile(self.map_regex, re.IGNORECASE)
        except re.error:
            return maps
        return [m for m in maps if rx.search(m.name)]


# =============================================================================
# SECTION 5 — MODELS
# =============================================================================
def make_settings() -> QSettings:
    return QSettings(str(SETTINGS_FILE), QSettings.Format.IniFormat)


def is_checked(value: Any) -> bool:
    return getattr(value, "value", value) == 2


class GameModel(QObject):
    games_changed = Signal()
    game_selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._games: List[GameConfig] = []
        self._selected: Optional[GameConfig] = None

    def all_games(self) -> List[GameConfig]:
        return list(self._games)

    def selected(self) -> Optional[GameConfig]:
        return self._selected

    def replace_games(self, games: List[GameConfig]) -> None:
        previous = self._selected.name if self._selected else None
        last = make_settings().value("last_game", None)
        self._games = list(games)
        self._selected = None
        for wanted in (previous, last):
            if wanted:
                self._selected = next((g for g in games if g.name == wanted), None)
                if self._selected:
                    break
        if self._selected is None and games:
            self._selected = games[0]
        self.games_changed.emit()
        if self._selected:
            self.game_selected.emit(self._selected)

    def select(self, game: GameConfig) -> None:
        if any(g is game for g in self._games):
            self._selected = game
            make_settings().setValue("last_game", game.name)
            self.game_selected.emit(game)

    def save_state(self) -> None:
        if self._selected:
            make_settings().setValue("last_game", self._selected.name)


class MapListModel(QAbstractListModel):
    MIME = "application/x-compilepal-maps"

    def __init__(self) -> None:
        super().__init__()
        self._maps: List[MapFile] = []
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save)
        self._load()

    # ---- persistence (config/maps.json) -----------------------------------
    def touch(self) -> None:
        self._save_timer.start()

    def flush(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()

    def _save(self) -> None:
        try:
            MAPS_FILE.write_text(
                json.dumps({"maps": [{"path": str(m.path), "enabled": m.enabled}
                                     for m in self._maps]},
                           indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            logger.error("Failed to save map list: %s", e)

    def _load(self) -> None:
        """Restore the queue (order and checkbox state); files that no longer
        exist are skipped."""
        try:
            data = json.loads(MAPS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = data.get("maps", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return
        skipped = 0
        for e in entries:
            if isinstance(e, str):
                e = {"path": e}
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                continue
            path = Path(e["path"])
            if not path.is_file() or path.suffix.lower() not in (".vmf", ".bsp"):
                skipped += 1
                continue
            if any(m.path == path for m in self._maps):
                continue
            self._maps.append(MapFile(path=path, enabled=bool(e.get("enabled", True))))
        logger.info("Restored %d map(s) from %s (%d missing skipped)",
                    len(self._maps), MAPS_FILE.name, skipped)

    def add_paths(self, paths: List[Path]) -> None:
        added = 0
        for p in paths:
            if not p.is_file() or p.suffix.lower() not in (".vmf", ".bsp"):
                continue
            if any(m.path == p for m in self._maps):
                continue
            n = len(self._maps)
            self.beginInsertRows(QModelIndex(), n, n)
            self._maps.append(MapFile(path=p))
            self.endInsertRows()
            added += 1
        if added:
            logger.info("Added %d map(s) to queue", added)
            self.touch()

    def remove_rows(self, rows: List[int]) -> None:
        for r in sorted(set(rows), reverse=True):
            if 0 <= r < len(self._maps):
                self.beginRemoveRows(QModelIndex(), r, r)
                del self._maps[r]
                self.endRemoveRows()
        self.touch()

    def clear_all(self) -> None:
        self.beginResetModel()
        self._maps.clear()
        self.endResetModel()
        self.touch()

    def enabled_paths(self) -> List[Path]:
        return [m.path for m in self._maps if m.enabled]

    def move_rows(self, rows: List[int], dest: int) -> None:
        rows = sorted({r for r in rows if 0 <= r < len(self._maps)})
        if not rows:
            return
        items = [self._maps[r] for r in rows]
        dest -= sum(1 for r in rows if r < dest)
        self.beginResetModel()
        for r in reversed(rows):
            del self._maps[r]
        dest = max(0, min(dest, len(self._maps)))
        self._maps[dest:dest] = items
        self.endResetModel()
        self.touch()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        return 0 if parent.isValid() else len(self._maps)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
        if index.isValid():
            f |= (Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                  | Qt.ItemFlag.ItemIsDragEnabled)
        return f

    def data(
        self, index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._maps)):
            return None
        item = self._maps[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.path.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(item.path)
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if item.enabled else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.UserRole:
            return str(item.path)
        if role == Qt.ItemDataRole.ForegroundRole and not item.enabled:
            return QBrush(QColor("#888888"))
        return None

    def setData(
        self, index: QModelIndex | QPersistentModelIndex, value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if index.isValid() and role == Qt.ItemDataRole.CheckStateRole:
            self._maps[index.row()].enabled = is_checked(value)
            model_index = self.index(index.row(), index.column())
            self.dataChanged.emit(model_index, model_index, [role])
            self.touch()
            return True
        return False

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    def mimeTypes(self) -> List[str]:
        return ["text/uri-list", self.MIME]

    def mimeData(self, indexes) -> QMimeData:
        mime = QMimeData()
        rows = sorted({i.row() for i in indexes if i.isValid()})
        mime.setData(self.MIME, QByteArray.fromStdString(",".join(map(str, rows))))
        return mime

    def dropMimeData(
        self, data: QMimeData, action: Qt.DropAction, row: int, column: int,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if action == Qt.DropAction.IgnoreAction:
            return True
        if data.hasUrls():
            self.add_paths([Path(u.toLocalFile()) for u in data.urls()])
            return True
        if data.hasFormat(self.MIME):
            try:
                rows = [int(r) for r in data.data(self.MIME).toStdString().split(",") if r]
            except ValueError:
                return False
            if row < 0:
                row = parent.row() if parent.isValid() else len(self._maps)
            self.move_rows(rows, row)
            return True
        return False


class PresetModel(QObject):
    changed = Signal()
    preset_selected = Signal(object)

    # Canonical preset set, 1:1 with the CompilePal++ UI list
    # (Fast / Normal / Full-* / Publish-*).
    DEFAULT_PRESET_ORDER: List[str] = [
        "Fast", "Normal", "Full - LDR", "Full - HDR", "Full - Both",
        "Publish - Both", "Publish - HDR",
    ]
    # Names that belong to shipped seeds (current or legacy) and are rebuilt
    # on schema migration; everything else is treated as user-created.
    LEGACY_SEED_NAMES = {"Fast", "Full LDR", "Full HDR", "Publish", "Pack",
                         "Full - LDR", "Full - HDR", "Full - Both",
                         "Publish - Both", "Publish - HDR", "Normal"}
    SCHEMA_VERSION = 7   # 7: Publish presets use PACK instead of BSPZIP

    def __init__(self) -> None:
        super().__init__()
        self._presets: List[Preset] = []
        self._selected: Optional[Preset] = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save)
        self._load()
        self._migrate()
        self._restore_last()
        if self._selected is None and self._presets:
            self._selected = self._presets[0]

    # ---- defaults ----------------------------------------------------------
    @classmethod
    def _build_default_presets(cls) -> List[Preset]:
        def flag(name: str) -> Parameter:
            return Parameter(name, True, kind="flag")

        def num(name: str, value: str) -> Parameter:
            return Parameter(name, value, enabled=True, kind="int")

        def off(proc: ProcessConfig) -> ProcessConfig:
            proc.enabled = False
            return proc

        K = ProcessKind

        def vrad_full(mode: str) -> ProcessConfig:
            """Final-quality VRAD in the given dynamic range (Full - LDR/HDR,
            Publish - HDR): Final + Static Prop Lighting + Static Prop Polys."""
            return ProcessConfig(K.VRAD, parameters=[
                flag("-final"), flag("-StaticPropLighting"),
                flag("-StaticPropPolys"), flag(f"-{mode}"),
            ])

        def vrad_full_both() -> ProcessConfig:
            """CompilePal++ 'Full - Both' VRAD block: final both-range lighting
            with the full Tools++ quality set."""
            return ProcessConfig(K.VRAD, parameters=[
                flag("-both"), flag("-final"), flag("-StaticPropLighting"),
                flag("-StaticPropPolys"), flag("-nolightcull"),
                flag("-ambientocclusion"), num("-patchlightmapsize", "8"),
                num("-StaticPropSampleScale", "4"), flag("-translucentshadows"),
                flag("-worldtextureshadows"), num("-extrasoft", "1"),
                flag("-TextureShadows"), num("-smooth", "45"),
            ])

        presets = [
            # Quick iteration: fast vis + fast low-bounce lighting.
            Preset("Fast", [
                ProcessConfig(K.VBSP),
                ProcessConfig(K.VVIS, parameters=[flag("-fast"), flag("-nosort")]),
                ProcessConfig(K.VRAD, parameters=[flag("-fast"), num("-bounce", "2")]),
                ProcessConfig(K.COPY),
                off(ProcessConfig(K.GAME)),
            ]),
            # Full pipeline, standard quality.
            Preset("Normal", [
                ProcessConfig(K.VBSP),
                ProcessConfig(K.VVIS),
                ProcessConfig(K.VRAD),
                ProcessConfig(K.COPY),
                off(ProcessConfig(K.GAME)),
            ]),
            # Final quality, single dynamic range, no packing.
            Preset("Full - LDR", [
                ProcessConfig(K.VBSP), ProcessConfig(K.VVIS), vrad_full("ldr"),
                ProcessConfig(K.COPY), off(ProcessConfig(K.GAME)),
            ]),
            Preset("Full - HDR", [
                ProcessConfig(K.VBSP), ProcessConfig(K.VVIS), vrad_full("hdr"),
                ProcessConfig(K.COPY), off(ProcessConfig(K.GAME)),
            ]),
            # CompilePal++ variant: both ranges + the Tools++ quality block.
            Preset("Full - Both", [
                ProcessConfig(K.VBSP, parameters=[flag("-allowdynamicpropsasstatic")]),
                ProcessConfig(K.VVIS), vrad_full_both(),
                ProcessConfig(K.COPY), off(ProcessConfig(K.GAME)),
            ]),
            # Ready to ship: final lighting + packed content; NAV/CUBEMAPS are
            # present but off by default (they launch the game).
            Preset("Publish - Both", [
                ProcessConfig(K.VBSP), ProcessConfig(K.VVIS), vrad_full("both"),
                ProcessConfig(K.COPY), off(ProcessConfig(K.NAV)),
                ProcessConfig(K.PACK), off(make_process(K.CUBEMAPS)),
            ]),
            Preset("Publish - HDR", [
                ProcessConfig(K.VBSP), ProcessConfig(K.VVIS), vrad_full("hdr"),
                ProcessConfig(K.COPY), off(ProcessConfig(K.NAV)),
                ProcessConfig(K.PACK), off(make_process(K.CUBEMAPS)),
            ]),
        ]
        for preset in presets:
            for proc in preset.processes:
                normalize_process(proc)
        return presets

    def _migrate(self) -> None:
        """Rebuild the shipped presets once per schema bump; user-created
        presets (any other name) are preserved."""
        settings = make_settings()
        try:
            schema = int(str(settings.value("preset_schema", 1) or 1))
        except (TypeError, ValueError):
            schema = 1

        if not self._presets:
            self._presets = self._build_default_presets()
            self._selected = self._presets[0]
            self._save()
            settings.setValue("preset_schema", self.SCHEMA_VERSION)
            return

        if schema < self.SCHEMA_VERSION:
            replaceable = self.LEGACY_SEED_NAMES | set(self.DEFAULT_PRESET_ORDER)
            customs = [p for p in self._presets if p.name not in replaceable]
            previous = self._selected.name if self._selected else None
            self._presets = self._build_default_presets() + customs
            self._selected = next((p for p in self._presets if p.name == previous),
                                  self._presets[0])
            self._save()
            settings.setValue("preset_schema", self.SCHEMA_VERSION)
            logger.info("Presets migrated to schema %d", self.SCHEMA_VERSION)

    def _restore_last(self) -> None:
        last = make_settings().value("last_preset", None)
        if last:
            match = next((p for p in self._presets if p.name == str(last)), None)
            if match is not None:
                self._selected = match

    def save_state(self) -> None:
        if self._selected is not None:
            make_settings().setValue("last_preset", self._selected.name)

    # ---- API ---------------------------------------------------------------
    def all_presets(self) -> List[Preset]:
        return list(self._presets)

    def selected(self) -> Optional[Preset]:
        return self._selected

    def touch(self) -> None:
        self._save_timer.start()

    def flush(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()

    def add(self, preset: Preset) -> Preset:
        base, i = preset.name, 1
        while any(p.name == preset.name for p in self._presets):
            preset.name = f"{base} ({i})"
            i += 1
        self._presets.append(preset)
        if self._selected is None:
            self._selected = preset
        self._save()
        self.changed.emit()
        return preset

    def remove(self, preset: Preset) -> None:
        if not any(p is preset for p in self._presets):
            return
        self._presets = [p for p in self._presets if p is not preset]
        if self._selected is preset:
            self._selected = self._presets[0] if self._presets else None
            self.save_state()
        self._save()
        self.changed.emit()
        self.preset_selected.emit(self._selected)

    def select(self, preset: Preset) -> None:
        if any(p is preset for p in self._presets):
            self._selected = preset
            self.save_state()
            self.preset_selected.emit(preset)

    def clone(self, preset: Preset) -> Preset:
        return self.add(Preset(
            name=f"{preset.name} (Copy)",
            processes=[
                ProcessConfig(
                    kind=p.kind, enabled=p.enabled,
                    parameters=[Parameter(**asdict(x)) for x in p.parameters],
                    custom_executable=p.custom_executable,
                ) for p in preset.processes
            ],
            map_regex=preset.map_regex,
        ))

    def _save(self) -> None:
        try:
            PRESETS_FILE.write_text(
                json.dumps([p.to_dict() for p in self._presets], indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            logger.error("Failed to save presets: %s", e)

    def _load(self) -> None:
        if not PRESETS_FILE.exists():
            return
        try:
            data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            self._presets = [Preset.from_dict(d) for d in data]  # normalizes inside
            if self._presets:
                self._selected = self._presets[0]
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error("Failed to load presets (%s) — keeping a .bak copy", e)
            try:
                PRESETS_FILE.replace(PRESETS_FILE.with_suffix(".json.bak"))
            except OSError:
                pass
            self._presets = []


# =============================================================================
# SECTION 6 — CONTROLLERS
# =============================================================================
class GameDiscoveryController(QObject):
    finished = Signal(list)

    @staticmethod
    def _steam_roots() -> List[Path]:
        roots: List[Path] = []

        def add(p: Path) -> None:
            if p not in roots:
                roots.append(p)

        for steam in (Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")):
            add(steam / "steamapps" / "common")
            try:
                text = (steam / "steamapps" / "libraryfolders.vdf").read_text(
                    encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for raw in re.findall(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
                add(Path(raw.replace("\\\\", "\\")) / "steamapps" / "common")
        for letter in "DEFG":
            add(Path(f"{letter}:/SteamLibrary/steamapps/common"))

        parts = SCRIPT_DIR.parts
        for i, part in enumerate(parts):
            if part.lower() == "common" and i > 0 and parts[i - 1].lower() == "steamapps":
                add(Path(*parts[: i + 1]))
                break
        return roots

    @staticmethod
    def _find_gameinfos(folder: Path) -> List[Tuple[Path, Path]]:
        direct = folder / "gameinfo.txt"
        if direct.is_file():
            return [(folder, direct)]
        found: List[Tuple[Path, Path]] = []
        try:
            for sub in sorted(folder.iterdir()):
                info = sub / "gameinfo.txt"
                if sub.is_dir() and info.is_file():
                    found.append((sub, info))
        except OSError:
            pass
        return found

    @classmethod
    def _find_gameinfos_deep(cls, folder: Path, depth: int) -> List[Tuple[Path, Path]]:
        direct = folder / "gameinfo.txt"
        if direct.is_file():
            return [(folder, direct)]
        if depth <= 0:
            return []
        out: List[Tuple[Path, Path]] = []
        try:
            children = sorted((c for c in folder.iterdir() if c.is_dir()),
                              key=lambda p: p.name)
        except OSError:
            return out
        for child in children:
            out.extend(cls._find_gameinfos_deep(child, depth - 1))
        return out

    @classmethod
    def _scan_custom(cls, root: Path, games: List[GameConfig], seen: set) -> None:
        if not root.is_dir():
            return
        key = os.path.normcase(str(root))
        if key in seen:
            return

        found = cls._find_gameinfos_deep(root, 2)
        if found:
            for game_dir, info in found:
                gkey = os.path.normcase(str(game_dir))
                if gkey in seen:
                    continue
                seen.add(gkey)
                try:
                    cfg = cls._parse(game_dir, info)
                    if cfg:
                        games.append(cfg)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", game_dir, e)
            return

        cfg = GameConfig(name=root.name, path=root)
        for sd in (root, root / "win64", root / "bin", root / "bin" / "win64"):
            if sd.is_dir():
                cls._locate_compilers(cfg, sd)
        if any((cfg.vbsp_path, cfg.vvis_path, cfg.vrad_path, cfg.bspzip_path)):
            seen.add(key)
            games.append(cfg)

    @Slot()
    def run(self) -> None:
        games: List[GameConfig] = []
        seen: set = set()

        cfg = load_dirs_config()
        roots = self._steam_roots() if cfg["auto"] else []

        for root in roots:
            if not root.is_dir():
                continue
            try:
                children = [c for c in root.iterdir() if c.is_dir()]
            except OSError:
                continue
            for child in children:
                for game_dir, info in self._find_gameinfos(child):
                    gkey = os.path.normcase(str(game_dir))
                    if gkey in seen:
                        continue
                    seen.add(gkey)
                    try:
                        g = self._parse(game_dir, info)
                        if g:
                            games.append(g)
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", game_dir, e)
        for custom in cfg["dirs"]:
            self._scan_custom(custom, games, seen)

        hidden = set(load_hidden_games())
        if hidden:
            games = [g for g in games if g.name not in hidden]

        games.sort(key=lambda g: g.name.lower())
        self.finished.emit(games)

    @staticmethod
    def _parse(game_dir: Path, gameinfo: Path) -> Optional[GameConfig]:
        try:
            text = gameinfo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        m = re.search(r'"SteamAppId"\s+"(\d+)"', text, re.IGNORECASE)
        cfg = GameConfig(name=game_dir.name, path=game_dir,
                         appid=int(m.group(1)) if m else None)

        parent = game_dir.parent
        search_dirs = [
            game_dir / "bin", game_dir / "bin" / "win64",
            parent / "bin", parent / "bin" / "win64", parent / "bin" / "x64",
            parent / "bin" / "tools_plusplus", parent / "bin" / "win64" / "tools_plusplus",
            game_dir / "bin" / "tools_plusplus", game_dir / "sourcesdk" / "bin",
        ]
        for sd in search_dirs:
            if sd.is_dir():
                GameDiscoveryController._locate_compilers(cfg, sd)
        GameDiscoveryController._locate_game_exe(cfg, game_dir)
        return cfg

    @staticmethod
    def _locate_compilers(cfg: GameConfig, search_dir: Path) -> None:
        mapping = {"vbsp": "vbsp_path", "vvis": "vvis_path",
                   "vrad": "vrad_path", "bspzip": "bspzip_path"}
        for name, attr in mapping.items():
            for suffix in ("++.exe", "++", ".exe", ""):
                path = search_dir / f"{name}{suffix}"
                if path.is_file():
                    plus = suffix.startswith("++")
                    if plus or getattr(cfg, attr) is None:
                        setattr(cfg, attr, path)
                    cfg.tools_plus_plus = cfg.tools_plus_plus or plus
                    break

    @staticmethod
    def _locate_game_exe(cfg: GameConfig, game_dir: Path) -> None:
        known = {"hl2.exe", "hl2", "gmod.exe", "tf.exe", "csgo.exe", "portal2.exe",
                 "left4dead2.exe", "dota2.exe", "bms.exe", "blackmesa.exe", "insurgency.exe"}
        for folder in (game_dir.parent, game_dir):
            try:
                for c in folder.iterdir():
                    if c.is_file() and c.name.lower() in known:
                        cfg.game_exe = c
                        return
            except OSError:
                continue


# =============================================================================
# SECTION 7b — PACK (built-in dependency packer)
# =============================================================================
# Port of CompilePal's built-in PACK stage: reads the BSP, finds the materials,
# textures, models, sounds and utility files it depends on in the game's search
# paths, writes a bspzip file list and (unless -dryrun) embeds it with
# `bspzip -addlist`. Only loose files are found: content that exists only inside
# VPK archives is never packed (same as stock CompilePal).
_PACK_KEYS: Dict[str, frozenset] = {
    "texture": frozenset({
        '$ambientocclusiontexture', '$basetexture', '$basetexture2', '$basetexture3',
        '$basetexture4', '$decaltexture', '$blendmodulatetexture', '$bumpmap',
        '$bumpmap2', '$bumpmask', '$cloudalphatexture', '$detail',
        '$detail2', '$tintmasktexture', '$masks', '$masks1',
        '$masks2', '$dudvmap', '$envmap', '$envmapmask',
        '$envmapmask2', '$flowmap', '$flowbounds', '$flow_noise_texture',
        '$emissiveblendtexture', '$emissiveblendbasetexture', '$emissiveblendflowtexture', '$fleshbordertexture1d',
        '$fleshcubetexture', '$fleshinteriornoisetexture', '$fleshinteriortexture', '$fleshnormaltexture',
        '$fleshsubsurfacetexture', '$hdrbasetexture', '$hdrcompressedtexture', '$lightwarptexture',
        '$material', '$normalmap', '$fresnelrangestexture', '$phongexponenttexture',
        '$phongwarptexture', '$reflecttexture', '$refracttexture', '$refracttinttexture',
        '$selfillummask', '$selfillumtexture', '$texture', '$texture2',
        '$worldspacetint', '$worldspacetype', '$emissiveblendtexture', '$emissiveblendbasetexture',
        '$emissiveblendflowtexture', '%tooltexture', 'srgb?$basetexture', '$iris',
        '$ambientoccltexture', '$mraotexture', '$emissiontexture', '$portalmasktexture',
        '$portalcolortexture',
    }),
    "material": frozenset({
        '$bottommaterial', '$crackmaterial', '$fallbackmaterial', '$underwateroverlay',
        'include', 'material',
    }),
    "sound": frozenset({
        'breaksound', 'closesound', 'gassound', 'gustsound',
        'locked_sound', 'message', 'messagesound', 'movementsound',
        'movepingsound', 'movesound', 'noise1', 'noise2',
        'puntsound', 'reversalsoundlarge', 'reversalsoundmedium', 'reversalsoundsmall',
        'slidesoundback', 'slidesoundfwd', 'soundcloseoverride', 'soundlockedoverride',
        'soundmoveoverride', 'soundopenoverride', 'soundunlockedoverride', 'startclosesound',
        'startsound', 'stopsound', 'unlocked_sound', 'point_allies_capsound',
        'point_axis_capsound', 'point_resetsound', 'fireendsound', 'firestartsound',
        'flysound', 'incomingsound', 'launchsound', 'pulsefiresound',
        'rotatesound', 'rotatestartsound', 'rotatestopsound', 'team_capsound_0',
        'team_capsound_2', 'team_capsound_3', 'disablesound', 'm_soundname',
        'commentaryfile', 'commentaryfile_nohdr', 'prop_pickup_sound', 'prop_drop_sound',
    }),
    "vmf_material": frozenset({
        'material', 'texture', 'ropematerial', 'overlaymaterial',
        'point_hud_icon_neutral', 'point_hud_icon_axis', 'point_hud_icon_allies', 'point_hud_icon_timercap',
        'point_hud_icon_bombed', 'spritename', 'team_icon_0', 'team_icon_2',
        'team_icon_3', 'team_overlay_0', 'team_overlay_2', 'team_overlay_3',
        'team_base_icon_2', 'team_base_icon_3', 'overlaymaterial', 'particletrailmaterial',
        'overlayname1', 'overlayname2', 'overlayname3', 'overlayname4',
        'overlayname5', 'overlayname6', 'overlayname7', 'overlayname8',
        'overlayname9', 'overlayname10', 'font',
    }),
    "model": frozenset({
        'model', 'shootmodel', 'point_allies_model', 'point_axis_model',
        'point_reset_model', 'missilemodel', 'team_model_0', 'team_model_2',
        'team_model_3', 'flag_model', 'plymodel', 'powerup_model',
        'swapmodel', 'prop_model_name', 'modeloverride',
    }),
}

_PACK_SKY_SUFFIXES = ("rt", "lf", "bk", "ft", "up", "dn")
_PACK_MDL_EXTS = (".vvd", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx",
                  ".phy", ".ani", ".vta")


def pack_source_dirs(game_dir: Path) -> List[Path]:
    """Loose content folders from gameinfo.txt SearchPaths (VPK entries are
    skipped); the game folder always comes first."""
    game_dir = game_dir.resolve()
    out: List[Path] = [game_dir]
    root = game_dir.parent
    try:
        text = (game_dir / "gameinfo.txt").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    text = re.sub(r"//[^\n]*", "", text)
    m = re.search(r'"?searchpaths"?\s*\{', text, re.IGNORECASE)
    if not m:
        return out
    depth, i = 1, m.end()
    start = i
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    for line in text[start:i - 1].splitlines():
        parts = re.findall(r'"[^"]*"|\S+', line)
        if len(parts) < 2:
            continue
        val = parts[1].strip('"').replace("\\", "/")
        if ".vpk" in val.lower() or "|gamebin|" in val.lower():
            continue
        val = re.sub(r"\|gameinfo_path\|", str(game_dir).replace("\\", "/") + "/",
                     val, flags=re.IGNORECASE)
        val = re.sub(r"\|all_source_engine_paths\|", str(root).replace("\\", "/") + "/",
                     val, flags=re.IGNORECASE)
        p = Path(val)
        if not p.is_absolute():
            p = root / p
        candidates = ([Path(g) for g in sorted(glob.glob(str(p)))]
                      if "*" in val else [p])
        for c in candidates:
            try:
                c = c.resolve()
            except OSError:
                continue
            if c.is_dir() and c not in out:
                out.append(c)
    return out


class _PackBsp:
    """Minimal read-only VBSP reader (entities, texdata, static props, pakfile)."""
    LUMP_ENTITIES, LUMP_GAME, LUMP_PAKFILE = 0, 35, 40
    LUMP_TEXDATA_STRING_DATA, LUMP_TEXDATA_STRING_TABLE = 43, 44

    def __init__(self, path: Path) -> None:
        self.data = path.read_bytes()
        if len(self.data) < 1036 or struct.unpack_from("<i", self.data, 0)[0] != 0x50534256:
            raise ValueError("not a Source engine BSP (VBSP) file")
        self.lumps = [struct.unpack_from("<4i", self.data, 8 + 16 * i) for i in range(64)]
        if any(lump[3] for lump in self.lumps):
            raise ValueError("BSPZIP does not support compressed BSPs")

    def lump(self, idx: int) -> bytes:
        off, length, _ver, _fourcc = self.lumps[idx]
        return self.data[off:off + length] if length > 0 and off > 0 else b""

    def entities(self) -> List[Dict[str, str]]:
        text = self.lump(self.LUMP_ENTITIES).split(b"\0", 1)[0].decode("utf-8", "replace")
        ents: List[Dict[str, str]] = []
        for block in re.findall(r"\{([^{}]*)\}", text):
            ent: Dict[str, str] = {}
            for k, v in re.findall(r'"([^"]*)"\s+"([^"]*)"', block):
                ent.setdefault(k.lower(), v)
            if ent:
                ents.append(ent)
        return ents

    def texdata_names(self) -> List[str]:
        table = self.lump(self.LUMP_TEXDATA_STRING_TABLE)
        strings = self.lump(self.LUMP_TEXDATA_STRING_DATA)
        out: List[str] = []
        for i in range(len(table) // 4):
            off = struct.unpack_from("<i", table, i * 4)[0]
            if 0 <= off < len(strings):
                out.append(strings[off:].split(b"\0", 1)[0].decode("latin-1"))
        return out

    def static_prop_models(self) -> List[str]:
        gl = self.lump(self.LUMP_GAME)
        if len(gl) < 4:
            return []
        count = struct.unpack_from("<i", gl, 0)[0]
        for i in range(max(0, min(count, 64))):
            base = 4 + 16 * i
            if base + 16 > len(gl):
                break
            gid, _flags, _ver, ofs, _len = struct.unpack_from("<iHHii", gl, base)
            if gid != 0x73707270:  # 'sprp'
                continue
            if ofs < 0 or ofs + 4 > len(self.data):
                return []
            n = struct.unpack_from("<i", self.data, ofs)[0]
            if not 0 < n <= 100000 or ofs + 4 + n * 128 > len(self.data):
                return []
            return [self.data[ofs + 4 + j * 128: ofs + 4 + (j + 1) * 128]
                    .split(b"\0", 1)[0].decode("latin-1") for j in range(n)]
        return []

    def pak(self) -> Optional[zipfile.ZipFile]:
        raw = self.lump(self.LUMP_PAKFILE)
        if not raw:
            return None
        try:
            return zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            return None


class BspPacker:
    """Collects the files a BSP depends on (internal path -> external file)."""
    _VMT_LINE = re.compile(r'^\s*"?([$%]?[\w.\-]+)"?\s+"?([^"\r\n]*?)"?\s*$')

    def __init__(self, source_dirs: List[Path], exclude_files: set,
                 exclude_dirs: List[Path], skip_sw_vtx: bool = False) -> None:
        self.dirs = source_dirs
        self.exclude_files = exclude_files
        self.exclude_dirs = [os.path.normcase(str(d)).rstrip("\\/") for d in exclude_dirs]
        self.skip_sw_vtx = skip_sw_vtx
        self.files: Dict[str, Tuple[str, Path, str]] = {}  # lower internal -> (internal, file, category)
        self.warnings: List[str] = []
        self._pak: Optional[zipfile.ZipFile] = None
        self._pak_names: set = set()
        self._seen_mats: set = set()
        self._seen_models: set = set()

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _norm(value: str) -> str:
        return value.strip().strip('"').replace("\\", "/").lstrip("/").lower()

    def find(self, rel: str) -> Optional[Path]:
        for d in self.dirs:
            p = d / rel
            if p.is_file():
                return p
        return None

    def add(self, internal: str, external: Path, category: str, force: bool = False) -> bool:
        internal = internal.replace("\\", "/").lstrip("/")
        key = internal.lower()
        if key in self.files or (not force and key in self._pak_names):
            return False
        ext_norm = os.path.normcase(str(external))
        if ext_norm in self.exclude_files:
            return False
        if any(ext_norm.startswith(d + os.sep) for d in self.exclude_dirs):
            return False
        self.files[key] = (internal, external, category)
        return True

    # ---- materials / textures ----------------------------------------------
    def add_material(self, value: str) -> None:
        rel = self._norm(value)
        if not rel:
            return
        if not rel.startswith("materials/"):
            rel = "materials/" + rel
        if not rel.endswith(".vmt"):
            rel += ".vmt"
        if rel in self._seen_mats:
            return
        self._seen_mats.add(rel)
        if rel in self._pak_names and self._pak is not None:
            try:
                text = self._pak.read(self._pak_real_name(rel)).decode("utf-8", "ignore")
            except (KeyError, zipfile.BadZipFile):
                return
        else:
            ext = self.find(rel)
            if ext is None:
                return
            self.add(rel, ext, "Materials")
            try:
                text = ext.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return
        self._scan_vmt(text)

    def _pak_real_name(self, lower_name: str) -> str:
        assert self._pak is not None
        for n in self._pak.namelist():
            if n.replace("\\", "/").lower() == lower_name:
                return n
        return lower_name

    def _scan_vmt(self, text: str) -> None:
        text = re.sub(r"//[^\n]*", "", text)
        for line in text.splitlines():
            m = self._VMT_LINE.match(line)
            if not m:
                continue
            key, val = m.group(1).lower(), m.group(2).strip()
            if not val:
                continue
            if key in _PACK_KEYS["texture"]:
                self.add_texture(val)
            elif key in _PACK_KEYS["material"]:
                self.add_material(val)

    def add_texture(self, value: str) -> None:
        rel = self._norm(value)
        if not rel or rel == "env_cubemap":
            return
        if not rel.startswith("materials/"):
            rel = "materials/" + rel
        if not rel.endswith(".vtf"):
            rel += ".vtf"
        ext = self.find(rel)
        if ext is not None:
            self.add(rel, ext, "Textures")

    # ---- models --------------------------------------------------------------
    def add_model(self, value: str) -> None:
        rel = self._norm(value)
        if rel.endswith(".vmt"):          # e.g. env_sprite "model" is a material
            self.add_material(rel)
            return
        if not rel.endswith(".mdl") or rel in self._seen_models:
            return
        self._seen_models.add(rel)
        ext = self.find(rel)
        if ext is None:
            return
        self.add(rel, ext, "Models")
        stem, ext_stem = rel[:-4], str(ext.with_suffix(""))
        for suffix in _PACK_MDL_EXTS:
            if self.skip_sw_vtx and suffix == ".sw.vtx":
                continue
            cand = Path(ext_stem + suffix)
            if cand.is_file():
                self.add(stem + suffix, cand, "Models")
        self._scan_mdl(ext)

    def _scan_mdl(self, path: Path) -> None:
        """Material names (cdtextures x textures) and $includemodel entries."""
        try:
            d = path.read_bytes()
            if len(d) < 344 or d[:4] != b"IDST":
                return

            def cstr(off: int) -> str:
                if not 0 <= off < len(d):
                    return ""
                return d[off:d.index(b"\0", off)].decode("latin-1")

            n_tex, o_tex, n_cd, o_cd = struct.unpack_from("<4i", d, 204)
            n_inc, o_inc = struct.unpack_from("<2i", d, 336)
            cds = [cstr(struct.unpack_from("<i", d, o_cd + 4 * i)[0])
                   for i in range(max(0, min(n_cd, 64)))] or [""]
            for i in range(max(0, min(n_tex, 1024))):
                base = o_tex + 64 * i
                name = cstr(base + struct.unpack_from("<i", d, base)[0])
                if not name:
                    continue
                for cd in cds:
                    self.add_material(cd + name)
            for i in range(max(0, min(n_inc, 64))):
                base = o_inc + 8 * i
                self.add_model(cstr(base + struct.unpack_from("<i", d, base + 4)[0]))
        except (struct.error, ValueError, OSError, IndexError) as e:
            self.warnings.append(f"Could not read {path.name}: {e}")

    # ---- sounds ---------------------------------------------------------------
    def add_sound(self, value: str) -> None:
        v = value.strip().strip('"').lstrip("*#@<>^()}$!?").replace("\\", "/").lstrip("/").lower()
        if not v.endswith((".wav", ".mp3")):
            return                          # soundscript names are not files
        rel = v if v.startswith("sound/") else "sound/" + v
        ext = self.find(rel)
        if ext is not None:
            self.add(rel, ext, "Sounds")

    # ---- whole map ---------------------------------------------------------------
    def collect(self, bsp: _PackBsp, map_name: str, rename_nav: bool = False) -> None:
        self._pak = bsp.pak()
        if self._pak is not None:
            self._pak_names = {n.replace("\\", "/").lower() for n in self._pak.namelist()}

        for name in bsp.texdata_names():
            self.add_material(name)
        for name in bsp.static_prop_models():
            self.add_model(name)

        for ent in bsp.entities():
            for key, val in ent.items():
                if key in _PACK_KEYS["model"]:
                    self.add_model(val)
                elif key in _PACK_KEYS["vmf_material"]:
                    self.add_material(val)
                elif key in _PACK_KEYS["sound"]:
                    self.add_sound(val)
            if ent.get("classname") == "worldspawn":
                sky = ent.get("skyname", "").strip()
                if sky:
                    for suffix in _PACK_SKY_SUFFIXES:
                        self.add_material(f"skybox/{sky}{suffix}")
                if ent.get("detailmaterial"):
                    self.add_material(ent["detailmaterial"])

        n = map_name.lower()
        utility = [
            (f"maps/{n}.nav", "maps/embed.nav" if rename_nav else f"maps/{n}.nav"),
            (f"maps/{n}.txt", None), (f"maps/{n}.res", None),
            (f"maps/{n}_level_sounds.txt", None),
            (f"scripts/soundscapes_{n}.txt", None),
            (f"resource/overviews/{n}.txt", None),
        ]
        for rel, internal in utility:
            ext = self.find(rel)
            if ext is not None:
                self.add(internal or rel, ext, "Misc")

    def write_list(self, path: Path) -> None:
        """bspzip -addlist format: internal path line, then external path line."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for internal, ext, _cat in self.files.values():
            lines += [internal, str(ext)]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


class CompileWorker(QThread):
    log_line = Signal(str, int)
    progress = Signal(int, int)
    step_start = Signal(str)
    log_partial = Signal(str)   # live, not-yet-terminated output line (e.g. VVIS PortalFlow dots)
    finished_ok = Signal(float)
    finished_err = Signal(str, float)

    CANCELLED = "Cancelled by user"

    def __init__(self, game: GameConfig, preset: Preset, maps: List[Path]) -> None:
        super().__init__()
        self.game = game
        self.preset = preset
        self.maps = maps
        self._cancelled = False
        self._proc: Optional[subprocess.Popen] = None
        self._copied_bsp: Optional[Path] = None   # set by COPY, used by PACK

    def cancel(self) -> None:
        self._cancelled = True
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self) -> None:
        t0 = time.time()
        enabled = [p for p in self.preset.processes if p.enabled]
        if not enabled:
            self.finished_err.emit("The preset has no enabled processes", 0.0)
            return
        total = len(enabled) * len(self.maps)
        step = 0
        try:
            for map_path in self.maps:
                if self._cancelled:
                    break
                from_bsp = map_path.suffix.lower() == ".bsp"
                bsp = map_path if from_bsp else map_path.with_suffix(".bsp")
                vmf = map_path.with_suffix(".vmf") if from_bsp else map_path
                self._copied_bsp = None
                self.log_line.emit(f"{LOG_MAP_MARKER}{map_path.name} ──", LogSeverity.INFO.value)

                for process in enabled:
                    if self._cancelled:
                        break
                    step += 1
                    self.progress.emit(step, total)
                    self.step_start.emit(f"{process.kind.value} — {map_path.name}")

                    if process.kind == ProcessKind.VBSP and from_bsp:
                        self.log_line.emit("VBSP skipped: a .bsp was selected directly",
                                           LogSeverity.INFO.value)
                        continue
                    if not self._run_one(process, vmf, bsp):
                        if self._cancelled:
                            break
                        self.finished_err.emit(
                            f"Step {process.kind.value} failed for {map_path.name}",
                            time.time() - t0)
                        return

            if self._cancelled:
                self.finished_err.emit(self.CANCELLED, time.time() - t0)
            else:
                self.finished_ok.emit(time.time() - t0)
        except Exception as e:
            logger.exception("CompileWorker crashed")
            self.finished_err.emit(str(e), time.time() - t0)

    def _run_one(self, process: ProcessConfig, vmf: Path, bsp: Path) -> bool:
        kind = process.kind
        if kind == ProcessKind.SHUTDOWN:
            return self._shutdown(process)
        if kind == ProcessKind.COPY:
            return self._copy_bsp(bsp)
        if kind == ProcessKind.PACK:
            return self._pack_bsp(process, bsp)

        if process.custom_executable:
            exe: Optional[Path] = Path(process.custom_executable)
        elif kind == ProcessKind.CUSTOM:
            self.log_line.emit("Custom process: no executable configured",
                               LogSeverity.ERROR.value)
            return False
        else:
            exe = self.game.executable_for(kind)

        if exe is None or not exe.is_file():
            if kind in (ProcessKind.CUBEMAPS, ProcessKind.NAV, ProcessKind.GAME):
                self.log_line.emit(
                    f"{kind.value}: game executable not found — skipped",
                    LogSeverity.WARNING.value)
                return True
            self.log_line.emit(f"{kind.value}: executable not found ({exe or 'not set'})",
                               LogSeverity.ERROR.value)
            return False

        if process.custom_executable and kind != ProcessKind.CUSTOM:
            allow_plus = Path(process.custom_executable).stem.endswith("++")
        else:
            allow_plus = self.game.plus_for(kind)
        args = process.build_args(vmf, bsp, self.game.path, allow_plus=allow_plus)
        cwd = exe.parent if kind in (ProcessKind.CUBEMAPS, ProcessKind.NAV,
                                     ProcessKind.GAME) else vmf.parent
        return self._run_executable(exe, args, cwd)

    def _copy_bsp(self, bsp: Path) -> bool:
        if not bsp.is_file():
            self.log_line.emit(f"COPY: compiled BSP not found ({bsp})",
                               LogSeverity.ERROR.value)
            return False
        dest_dir = self.game.maps_dir
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bsp, dest_dir / bsp.name)
        except OSError as e:
            self.log_line.emit(f"COPY failed: {e}", LogSeverity.ERROR.value)
            return False
        self._copied_bsp = dest_dir / bsp.name
        self.log_line.emit(f"Copied {bsp.name} → {dest_dir}", LogSeverity.OK.value)
        return True

    def _pack_bsp(self, process: ProcessConfig, bsp: Path) -> bool:
        """PACK stage: find the map's dependencies, write a bspzip file list and
        embed it (bspzip -addlist). Works on the COPY stage's BSP when there is
        one, then copies the packed BSP back next to the VMF (like CompilePal)."""
        emit = self.log_line.emit
        ERR, WARN = LogSeverity.ERROR.value, LogSeverity.WARNING.value
        INFO, OK = LogSeverity.INFO.value, LogSeverity.OK.value

        target = self._copied_bsp if (self._copied_bsp and self._copied_bsp.is_file()) else bsp
        if not target.is_file():
            emit(f"PACK: BSP not found ({target})", ERR)
            return False

        opts: Dict[str, List[str]] = {}
        appid = self.game.appid
        for name, value in process.option_pairs():
            spec = spec_for(ProcessKind.PACK, name)
            if spec is not None and spec.games and appid and appid not in spec.games:
                emit(f"PACK: {name} ignored (not supported by this game)", WARN)
                continue
            opts.setdefault(name.lower(), []).append(value or "")

        def has(flag: str) -> bool:
            return flag in opts

        def values(flag: str) -> List[str]:
            return [v for v in opts.get(flag, []) if v]

        if has("-vpk") or has("-ainfo"):
            emit("PACK: VPK packing (-vpk / -ainfo) is not supported by this app yet - "
                 "disable those options to pack into the BSP", ERR)
            return False
        if has("-excludevpk"):
            emit("PACK: -excludevpk is not supported yet (VPK contents are not read); ignored", WARN)
        if has("-renameparticlemanifest"):
            emit("PACK: particle manifests are not packed by this app; "
                 "-renameparticlemanifest ignored", WARN)

        verbose, dryrun = has("-verbose"), has("-dryrun")
        exe: Optional[Path] = None
        if not dryrun:
            exe = (Path(process.custom_executable) if process.custom_executable
                   else self.game.bspzip_path)
            if exe is None or not exe.is_file():
                emit(f"PACK: bspzip executable not found ({exe or 'not set'})", ERR)
                return False

        emit("PACK - automated packaging", INFO)
        source_dirs = pack_source_dirs(self.game.path)
        for pattern in values("-sourcedirectory"):
            hits = [Path(g).resolve() for g in glob.glob(pattern) if os.path.isdir(g)]
            if not hits:
                emit(f"PACK: no matching folders for {pattern}", WARN)
            source_dirs += [h for h in hits if h not in source_dirs]
        if verbose:
            emit("Source directories:", INFO)
            for d in source_dirs:
                emit(f"  {d}", INFO)

        exclude_files: set = set()
        for f in values("-exclude"):
            if Path(f).is_file():
                exclude_files.add(os.path.normcase(str(Path(f).resolve())))
            else:
                emit(f"PACK: could not find file to exclude: {f}", WARN)
        exclude_dirs: List[Path] = []
        for d in values("-excludedir"):
            if Path(d).is_dir():
                exclude_dirs.append(Path(d).resolve())
            else:
                emit(f"PACK: could not find folder to exclude: {d}", WARN)

        emit("Reading BSP...", INFO)
        try:
            bsp_data = _PackBsp(target)
        except (ValueError, OSError) as e:
            emit(f"PACK: {e}", ERR)
            return False

        packer = BspPacker(source_dirs, exclude_files, exclude_dirs, has("-noswvtx"))
        emit("Finding map dependencies...", INFO)
        packer.collect(bsp_data, target.stem, rename_nav=has("-renamenav"))
        for w in packer.warnings:
            emit(f"PACK: {w}", WARN)
        if self._cancelled:
            return False

        def internal_for(p: Path) -> Optional[str]:
            rp = p.resolve()
            for d in source_dirs:
                try:
                    return rp.relative_to(d).as_posix()
                except ValueError:
                    continue
            return None

        for f in values("-include"):
            p = Path(f)
            rel = internal_for(p) if p.is_file() else None
            if rel is None:
                emit(f"PACK: cannot include {f} (missing or outside the search paths)", WARN)
                continue
            packer.add(rel, p.resolve(), "Misc", force=True)
        for d in values("-includedir"):
            dp = Path(d)
            if not dp.is_dir():
                emit(f"PACK: could not find folder: {d}", WARN)
                continue
            added = 0
            for fp in sorted(dp.rglob("*")):
                rel = internal_for(fp) if fp.is_file() else None
                if rel is not None and packer.add(rel, fp.resolve(), "Misc", force=True):
                    added += 1
            emit(f"Added {added} files from {d}", INFO)
        for fl in values("-includefilelist"):
            lp = Path(fl)
            if not lp.is_file():
                emit(f"PACK: could not find file list {fl}", ERR)
                continue
            rows = [r.strip() for r in lp.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if r.strip()]
            added = 0
            for i in range(0, len(rows) - 1, 2):   # internal path line, external path line
                ext = Path(rows[i + 1])
                if ext.is_file() and packer.add(rows[i], ext, "Misc", force=True):
                    added += 1
                else:
                    emit(f"PACK: failed to pack {rows[i + 1]}", WARN)
            emit(f"Added {added} files from {lp.name}", INFO)

        list_file = CONFIG_DIR / "BSPZipFiles" / f"{target.stem}_files.txt"
        try:
            packer.write_list(list_file)
        except OSError as e:
            emit(f"PACK: cannot write file list: {e}", ERR)
            return False

        counts: Dict[str, int] = {}
        for internal, _ext, cat in packer.files.values():
            if cat == "Models" and not internal.lower().endswith(".mdl"):
                continue                      # count models, not their .vvd/.vtx/.phy files
            counts[cat] = counts.get(cat, 0) + 1
        emit("---------------------", INFO)
        for cat in ("Materials", "Textures", "Models", "Sounds", "Misc"):
            if counts.get(cat):
                emit(f"{counts[cat]} {cat.lower()} found", INFO)
        emit("---------------------", INFO)
        if verbose or has("-detailedreport"):
            for cat in ("Materials", "Textures", "Models", "Sounds", "Misc"):
                rows = sorted(i for i, _e, c in packer.files.values() if c == cat)
                if rows:
                    emit(cat, INFO)
                    for r in rows:
                        emit(f"  {r}", INFO)

        if dryrun:
            emit(f"File list saved as {list_file}", OK)
            return True
        if not packer.files:
            emit("PACK: nothing to pack", INFO)
            return True

        try:                                         # keep the unpacked BSP, like CompilePal
            shutil.copy2(target, target.with_name(target.name + ".unpacked"))
        except OSError as e:
            emit(f"PACK: could not back up the unpacked BSP: {e}", WARN)

        emit("Running bspzip...", INFO)
        assert exe is not None
        env = dict(os.environ, VPROJECT=str(self.game.path))
        args = ["-game", str(self.game.path), "-addlist",
                str(target.resolve()), str(list_file.resolve()), str(target.resolve())]
        if not self._run_executable(exe, args, exe.parent, env):
            return False

        if target != bsp:                            # packed copy -> back next to the VMF
            try:
                shutil.copy2(target, bsp)
                emit(f"Copied packed BSP back to {bsp.parent}", OK)
            except OSError as e:
                emit(f"PACK: could not copy the packed BSP back: {e}", WARN)
        emit(f"Packed {len(packer.files)} files into {target.name}", OK)
        return True

    def _shutdown(self, process: ProcessConfig) -> bool:
        delay = 5
        for p in process.parameters:
            if p.name == "--delay-seconds" and p.enabled:
                try:
                    delay = max(0, int(p.value))
                except (TypeError, ValueError):
                    pass
        self.log_line.emit(
            f"System shutdown in {delay}s (abort with: shutdown /a)", LogSeverity.WARNING.value)
        cmd = (["shutdown", "/s", "/t", str(delay)] if IS_WINDOWS
               else ["shutdown", "-h", f"+{max(1, -(-delay // 60))}"])
        try:
            subprocess.run(cmd, check=False, creationflags=NO_WINDOW)
            return True
        except OSError as e:
            self.log_line.emit(f"Shutdown failed: {e}", LogSeverity.ERROR.value)
            return False

    def _run_executable(self, exe: Path, args: List[str], cwd: Path,
                        env: Optional[Dict[str, str]] = None) -> bool:
        cmd = [str(exe), *args]
        self.log_line.emit("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd),
                           LogSeverity.DEBUG.value)
        try:
            # Binary, unbuffered pipe: read() returns as soon as ANY data is available.
            # (Iterating a text-mode pipe waits for a newline, which hides progress lines
            # such as VVIS "PortalFlow: 0...1...2..." until they are finished.)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(cwd),
                bufsize=0, creationflags=NO_WINDOW, env=env)
            self._proc = proc
        except OSError as e:
            self.log_line.emit(f"Failed to start: {e}", LogSeverity.ERROR.value)
            return False

        try:
            assert proc.stdout is not None
            self._pump_output(proc.stdout)
        except ValueError:
            pass
        proc.wait()
        self._proc = None

        if self._cancelled:
            return False
        rc = proc.returncode
        if rc == 0:
            self.log_line.emit(f"{exe.name} completed (exit code 0)", LogSeverity.OK.value)
            return True
        self.log_line.emit(f"{exe.name} failed with exit code {rc} (0x{rc & 0xFFFFFFFF:x})",
                           LogSeverity.ERROR.value)
        return False

    def _pump_output(self, stream) -> None:
        """Stream a tool's output to the log in real time.

        Complete lines go to log_line. The unfinished tail (no newline yet) is pushed
        to log_partial on every chunk so the log shows it live; a lone '\\r' inside a
        line means "overwrite the line" (console progress), so only the text after
        the last '\\r' is displayed.
        """
        decoder = codecs.getincrementaldecoder(
            locale.getpreferredencoding(False) or "utf-8")(errors="replace")
        buf = ""            # raw text of the current, unterminated line
        shown = ""          # what the log currently shows as the live line

        def final_text(raw: str) -> str:
            return raw.split("\r")[-1].rstrip()

        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += decoder.decode(chunk)
            if buf.endswith("\r"):
                # may be the first half of "\r\n"; wait for the next chunk to decide
                head, tail = buf[:-1], "\r"
            else:
                head, tail = buf, ""
            head = head.replace("\r\n", "\n")
            *lines, rest = head.split("\n")
            for raw in lines:
                text = final_text(raw)
                if text:
                    self.log_line.emit(text, self._classify_line(text).value)
                    shown = ""
                elif shown:
                    self.log_partial.emit("")
                    shown = ""
            buf = rest + tail
            live = final_text(rest)   # a pending trailing "\r" does not blank the line
            if live != shown:
                self.log_partial.emit(live)
                shown = live

        buf += decoder.decode(b"", final=True)
        text = final_text(buf.replace("\r\n", "\n"))
        if text:
            self.log_line.emit(text, self._classify_line(text).value)   # replaces the live line
        elif shown:
            self.log_partial.emit("")

    @staticmethod
    def _classify_line(line: str) -> LogSeverity:
        low = line.lower()
        # VRAD++ GPU memory table header ("peak uploaded leaked allocs") is plain info;
        # "leaked" there is a column name, not a map leak.
        if "leaked" in low and "peak" in low and "uploaded" in low:
            return LogSeverity.INFO
        if any(k in low for k in ("error", "fatal", "leaked", "could not", "failed to",
                                  "exception", "abort")):
            return LogSeverity.ERROR
        if any(k in low for k in ("warning", "caution", "can't find", "missing", "ignored")):
            return LogSeverity.WARNING
        if any(k in low for k in ("completed", "success")):
            return LogSeverity.OK
        return LogSeverity.INFO


class CompileController(QObject):
    started = Signal()
    progress = Signal(int, int)
    step_start = Signal(str)
    log_line = Signal(str, int)
    log_partial = Signal(str)
    done_ok = Signal(float)
    done_err = Signal(str, float)

    def __init__(self) -> None:
        super().__init__()
        self._worker: Optional[CompileWorker] = None
        self._all: List[CompileWorker] = []

    def is_running(self) -> bool:
        return self._worker is not None

    def start(self, game: GameConfig, preset: Preset, maps: List[Path]) -> None:
        if self.is_running():
            return
        w = CompileWorker(game, preset, maps)
        w.log_line.connect(self.log_line.emit)
        w.log_partial.connect(self.log_partial.emit)
        w.progress.connect(self.progress.emit)
        w.step_start.connect(self.step_start.emit)
        w.finished_ok.connect(self._on_ok)
        w.finished_err.connect(self._on_err)
        w.finished.connect(lambda w=w: self._on_thread_finished(w))
        self._worker = w
        self._all.append(w)
        w.start()
        self.started.emit()

    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.cancel()
        for w in list(self._all):
            w.wait(timeout_ms)

    def _retire(self) -> None:
        w, self._worker = self._worker, None
        if w is not None and w.isFinished() and w in self._all:
            self._all.remove(w)

    def _on_thread_finished(self, w: CompileWorker) -> None:
        if w is not self._worker and w in self._all:
            self._all.remove(w)

    def _on_ok(self, elapsed: float) -> None:
        self._retire()
        self.done_ok.emit(elapsed)

    def _on_err(self, msg: str, elapsed: float) -> None:
        self._retire()
        self.done_err.emit(msg, elapsed)


# =============================================================================
# SECTION 6c — COMPILE ERROR HINTS
# =============================================================================
# Every line that appears in the "Compile output" log is matched against the
# patterns below. When a known error/warning is found, a short explanation
# ("What it means" / "How to fix") is printed right under it. Each hint is shown
# only once per map, so a message that repeats hundreds of times does not flood
# the log. Lines that match a hint are also promoted to at least the severity
# of the hint (e.g. an unrecognised "Overlay touching too many faces" line is
# shown as a warning even though it contains no "warning" keyword).
#
# Sources: Valve Developer Community "Compile Errors", Interlopers "Error page",
# plus common messages printed by VBSP / VVIS / VRAD and the Windows runtime.
# To add a hint, append a _h(...) entry to COMPILE_HINTS (specific patterns must
# come BEFORE generic ones because the first match wins).
@dataclass(frozen=True)
class CompileHint:
    key: str
    stage: str
    title: str
    pattern: "re.Pattern[str]"
    meaning: str
    fix: str
    severity: LogSeverity

    def lines(self) -> List[str]:
        return [
            f"    ↳ HINT: {self.title} [{self.stage}; shown once per map]",
            f"       What it means: {self.meaning}",
            f"       How to fix: {self.fix}",
        ]


_HINT_SEVERITY: Dict[str, LogSeverity] = {
    "E": LogSeverity.ERROR, "W": LogSeverity.WARNING, "I": LogSeverity.INFO,
}


def _h(key: str, stage: str, title: str, regex: str, meaning: str, fix: str,
       sev: str = "E") -> CompileHint:
    return CompileHint(key, stage, title, re.compile(regex, re.IGNORECASE),
                       meaning, fix, _HINT_SEVERITY[sev])


# MAX_MAP_<NAME> hard limits of the BSP format: name -> (meaning, fix).
# The exact numeric limit depends on the engine branch, so none is quoted here.
_MAP_LIMITS: Dict[str, Tuple[str, str]] = {
    "PLANES": (
        "The map contains too many unique planes (brush faces at different angles/positions).",
        "Simplify the geometry: avoid many arbitrarily angled brushes, remove tiny details, "
        "and build fine detail from models (props) instead of brushes."),
    "BRUSHES": (
        "The map contains too many brushes.",
        "Merge brushes, delete hidden/unused ones, replace small detail with props, or split "
        "the level into several smaller maps."),
    "BRUSHSIDES": (
        "The map contains too many brush faces (sides).",
        "Reduce the number of sides on complex brushes (arches, cylinders, vertex-edited "
        "shapes) or replace them with props."),
    "NODES": (
        "The BSP tree has too many nodes - usually many small brushes cutting up the world.",
        "Turn small details into func_detail, merge or remove tiny brushes and simplify the layout."),
    "LEAFS": (
        "The BSP tree has too many leaves - the world geometry is split into too many pieces.",
        "Make small structures func_detail, remove unnecessary brush clutter, simplify rooms "
        "and use props for detail."),
    "LEAFFACES": (
        "Too many face references in leaves - the world geometry is too fragmented.",
        "Simplify the geometry and make small details func_detail or props."),
    "LEAFBRUSHES": (
        "Too many brush references in leaves - the world geometry is too fragmented.",
        "Simplify the geometry and make small details func_detail or props."),
    "FACES": (
        "The map contains too many faces.",
        "Remove hidden faces, simplify geometry, lower displacement power and use props for detail."),
    "VERTS": (
        "The map contains too many vertices.",
        "Simplify brushes and displacements, and use props for detail."),
    "VERTEXES": (
        "The map contains too many vertices.",
        "Simplify brushes and displacements, and use props for detail."),
    "EDGES": (
        "The map contains too many edges.",
        "Simplify brushes and displacements, and use props for detail."),
    "SURFEDGES": (
        "The map contains too many surface edges.",
        "Simplify brushes and displacements, and use props for detail."),
    "TEXINFO": (
        "Too many unique texture setups - every different combination of material, scale, "
        "rotation, shift and lightmap scale uses one texinfo entry.",
        "Use consistent texture scales/lightmap scales, avoid rotating or shifting textures on "
        "every face, and align textures to World/Face with the same values where possible."),
    "TEXDATA": (
        "The map uses too many different materials.",
        "Reuse materials, remove unused ones and replace rarely used materials with common ones."),
    "DISPINFO": (
        "The map contains too many displacements.",
        "Use fewer displacements, merge neighbouring ones where possible, or use models for terrain detail."),
    "DISP_VERTS": (
        "The displacements together contain too many vertices.",
        "Lower the displacement power (e.g. 4 -> 3 or 2) or use fewer displacements."),
    "DISP_TRIS": (
        "The displacements together contain too many triangles.",
        "Lower the displacement power (e.g. 4 -> 3 or 2) or use fewer displacements."),
    "MODELS": (
        "The map has too many brush entities (func_door, func_brush, trigger_*, ...); each one is a separate model.",
        "Merge brush entities, convert decorative func_brush entities to props or func_detail, "
        "and remove unused triggers."),
    "ENTITIES": (
        "The map contains too many entities.",
        "Delete unused entities, convert static props/lights into fewer entities, and replace "
        "many info_overlay/decals with fewer ones."),
    "LIGHTING": (
        "The lightmap data is too large for the BSP.",
        "Increase the lightmap scale (use a larger number) on big or unimportant faces, "
        "apply nodraw to faces nobody sees, and reduce the amount of visible surface area."),
    "VISIBILITY": (
        "The visibility (VVIS) data is too large.",
        "Use func_detail for small structures, add areaportals/hint brushes, and divide the map "
        "into well-separated areas."),
    "PORTALS": (
        "The map contains too many visibility portals.",
        "Make small structures func_detail so they do not create portals, and simplify the layout."),
    "CLUSTERS": (
        "The map contains too many visibility clusters.",
        "Make small structures func_detail, remove unnecessary world brushes and simplify the layout."),
    "AREAS": (
        "The map contains too many areas (created by areaportals).",
        "Use fewer func_areaportal brushes - only at doors/openings where they really help."),
    "AREAPORTALS": (
        "The map contains too many areaportals.",
        "Use fewer func_areaportal/func_areaportal_window brushes."),
    "OVERLAYS": (
        "The map contains too many info_overlay entities.",
        "Remove unnecessary overlays or merge decals into the textures."),
    "WATEROVERLAYS": (
        "Too many overlay/water interactions.",
        "Remove or reduce overlays that touch water surfaces."),
    "PRIMITIVES": (
        "Too many face primitives were generated (geometry is too complex).",
        "Simplify the geometry and reduce the number of faces."),
    "PRIMVERTS": (
        "Too many primitive vertices were generated (geometry is too complex).",
        "Simplify the geometry and reduce the number of faces."),
    "PRIMINDICES": (
        "Too many primitive indices were generated (geometry is too complex).",
        "Simplify the geometry and reduce the number of faces."),
    "CUBEMAPSAMPLES": (
        "The map contains too many env_cubemap entities.",
        "Remove excess env_cubemaps (roughly one per room/lighting area is enough)."),
    "OCCLUDERS": (
        "The map contains too many func_occluder faces.",
        "Remove unnecessary occluders; keep only large ones that really hide a lot of geometry."),
    "WORLDLIGHTS": (
        "The map contains too many lights (including those emitted by self-illuminated textures).",
        "Remove or merge light/light_spot entities and avoid placing many light-emitting textures."),
}


COMPILE_HINTS: List[CompileHint] = [
    # ------------------------------------------------------------------ tool start-up
    _h("failed_start_missing", "Tool", "Compiler executable not found",
       r"Failed to start:.*(?:WinError 2|No such file|cannot find the file)",
       "The compile tool (vbsp/vvis/vrad or a custom program) could not be launched because its "
       "executable does not exist at the configured path.",
       "Check the game's bin folder and the game paths in this tool; make sure vbsp.exe, vvis.exe "
       "and vrad.exe exist there (verify the game files in Steam if they are missing)."),
    _h("failed_start_193", "Tool", "Not a valid Windows program",
       r"Failed to start:.*WinError 193",
       "The file is not a valid Windows application (wrong 32/64-bit build, or a corrupted file).",
       "Point the tool at the real vbsp/vvis/vrad .exe that ships with your game, or reinstall/verify the game files."),
    _h("failed_start_740", "Tool", "Administrator rights required",
       r"Failed to start:.*WinError 740",
       "Windows requires elevated (administrator) rights to start this program.",
       "Run this tool as administrator, or move the game/tools out of a protected folder."),
    _h("failed_start_denied", "Tool", "Windows refused to start the program",
       r"Failed to start:.*(?:WinError 5|Permission denied|Access is denied)",
       "Windows denied permission to start the program (permissions, a blocked file or antivirus).",
       "Run this tool as administrator, whitelist the game folder in your antivirus and unblock the "
       "file in its Properties dialog if Windows marked it as downloaded."),
    # ------------------------------------------------------------------ exit codes
    _h("exit_access_violation", "Tool", "Compiler crashed (access violation)",
       r"exit code .*\(0xc0000005\)",
       "The compiler crashed with a memory access violation. It is usually triggered by something it "
       "cannot handle (invalid geometry, a broken model/material, or running out of memory).",
       "Read the last lines printed before the crash - they show what was being processed. Known VBSP "
       "causes: a playerclip texture on a displacement, or 1-unit-high displacements with different "
       "powers touching each other. Run Hammer's 'Check for Problems' (Alt+P) and remove recently "
       "added parts of the map one by one to find the culprit."),
    _h("exit_stack_overflow", "Tool", "Compiler crashed (stack overflow)",
       r"exit code .*\(0xc00000fd\)",
       "The compiler ran out of stack space, usually because of extremely complex or degenerate geometry.",
       "Simplify the map, look at the last lines printed before the crash, and remove recently added "
       "geometry to find the cause."),
    _h("exit_fast_fail", "Tool", "Compiler aborted (fatal runtime check)",
       r"exit code .*\(0xc0000409\)",
       "The compiler terminated itself after detecting memory corruption or a fatal internal check.",
       "Look at the last lines printed before the abort and remove recently added geometry, models or "
       "materials to find the cause."),
    _h("exit_dll", "Tool", "Compiler could not start (DLL problem)",
       r"exit code .*\(0x(?:c0000135|c000007b|c0000142|c0000139)\)",
       "The compiler could not start because a required DLL is missing, mismatched (32/64-bit) or failed to initialise.",
       "Use vbsp/vvis/vrad from the bin folder of the game you are compiling for, reinstall the "
       "Microsoft Visual C++ Redistributables and verify the game files in Steam."),
    _h("exit_generic", "Tool", "Compiler reported an error",
       r"failed with exit code -?1 \(0x(?:1|ffffffff)\)",
       "The compiler detected a fatal problem and stopped on purpose.",
       "Scroll up to the first error printed by this tool - later errors (such as 'Error opening "
       "...bsp') are usually only consequences of it."),
    # ------------------------------------------------------------------ general / Windows
    _h("file_not_found", "General", "File not found",
       r"The system cannot find the file specified|\[Errno 2\] No such file or directory",
       "A file could not be found. This is most often printed by the copy step at the end of the "
       "compile when no .BSP exists - VBSP failed earlier and never wrote it - or when the "
       "destination folder does not exist.",
       "Find and fix the first VBSP error (usually a leak or invalid geometry). Check that the map "
       "name has no unusual characters and that the output/game 'maps' folder exists (run the game "
       "once or create the folder manually)."),
    _h("path_not_found", "General", "Folder not found",
       r"The system cannot find the path specified|The directory name is invalid",
       "A folder in the file path does not exist.",
       "Check the game, map and output paths for typos, create the missing folder and avoid very long paths."),
    _h("access_denied", "General", "File is locked or access denied",
       r"Access is denied|being used by another process|\[Errno 13\] Permission denied|sharing violation",
       "A file or folder is locked or protected - for example the BSP is still open in the game or "
       "Hammer, a cloud-sync/antivirus program is scanning it, or you lack write permission.",
       "Close the game and Hammer, pause cloud sync (e.g. OneDrive) or antivirus scanning for the "
       "folder, and run this tool as administrator if the folder is protected."),
    _h("error_opening_bsp", "VVIS/VRAD", "Cannot open the BSP",
       r"Error opening .*\.bsp",
       "VVIS/VRAD (or another step) could not find or open the BSP that VBSP should have created. "
       "It is a consequence of an earlier failure, not the real cause.",
       "Find the first error in the VBSP output and fix it. Also check the path and rename the map so "
       "it has no capital letters, dots or special characters."),
    _h("steam_startup", "General", "Steam could not be started",
       r"SteamStartup\(.*\) failed",
       "The tool could not initialise Steam (for example the registry was busy).",
       "Recompile, or restart Steam and try again."),
    _h("unbounded_volume", "VBSP", "Node with unbounded volume",
       r"node with unbounded volume",
       "Part of the map touches or lies outside the editor's grid/world boundary, so the compiler "
       "cannot compute its bounds. It can also be caused by invalid solids or a func_instance with "
       "cordons enabled inside it.",
       "Move everything away from the edges of the Hammer grid, run 'Check for Problems' (Alt+P) to "
       "find invalid solids and disable cordons inside instances."),
    _h("out_of_memory", "General", "Out of memory",
       r"out of memory|failed to allocate|bad_alloc|not enough (?:virtual )?memory|insufficient memory",
       "The compile tool ran out of memory. The 32-bit Source compile tools can only use a limited amount of RAM.",
       "Close other programs, simplify the map (fewer displacements, props and tiny brushes), use larger "
       "lightmap scales and avoid very high-quality VRAD settings for test compiles."),
    _h("gameinfo_missing", "General", "gameinfo.txt problem",
       r"(?:could ?n.?t|can.?t|cannot|unable to|failed to).*gameinfo|gameinfo\.txt.*(?:not found|missing|invalid|error)",
       "The tool could not find or read the game's gameinfo.txt, so it does not know which game "
       "folder to use.",
       "Select the correct game in this tool and make sure its folder contains gameinfo.txt "
       "(the -game / -vproject parameter must point to that folder)."),
    _h("dll_missing", "General", "Missing or invalid DLL",
       r"\b\w+\.dll\b.*(?:not found|is missing|could not be found|cannot be found|failed to load)"
       r"|(?:unable|failed|cannot|can't|could not).*(?:load|find).*\.dll",
       "A DLL needed by the compile tool is missing or could not be loaded (often the tools were copied "
       "away from the game's bin folder, or are from a different engine branch).",
       "Run the tools from the bin folder of the game you compile for, reinstall the Microsoft Visual "
       "C++ Redistributables and verify the game files in Steam."),
    _h("vmf_open", "VBSP", "Cannot open the VMF",
       r"(?:could ?n.?t|can.?t|cannot|unable to|failed to) (?:open|load|read|find) .*\.vmf",
       "The map source (.vmf) could not be opened.",
       "Check that the file exists and is saved, the path has no unusual characters, and that Hammer "
       "is not still writing the file."),
    # ------------------------------------------------------------------ VBSP: leaks and brushes
    _h("leak", "VBSP", "Map leak",
       r"\*{4} leaked \*{4}|Entity .+ leaked!",
       "The inside of the map is not sealed from the outside void. VVIS and VRAD cannot work "
       "properly on a leaking map (no visibility, full-bright or broken lighting).",
       "In Hammer use Map > Load Pointfile: a red line starts at the leaking entity and runs through "
       "the hole. Follow it in the 3D view, seal the hole and recompile. If an entity such as a "
       "func_* leaks without an obvious hole, its origin is outside the map: select it and use "
       "'Center Origins' (find all of that class via Map > Entity Report)."),
    _h("microbrush", "VBSP", "Micro brush",
       r"microbrush",
       "A brush is too small to be compiled (usually smaller than 1 Hammer unit).",
       "Find the brush with the given number (Map > Go to Brush), delete it and recreate it larger.", "W"),
    _h("floatplane", "VBSP", "Invalid brush plane",
       r"FloatPlane: bad normal",
       "A brush has an unnecessary vertex on a flat plane, usually after using the Vertex Tool or "
       "when creating small curved brushes with too many vertices.",
       "Find the brush with the given number, open the Vertex Tool and merge the unnecessary vertex "
       "with another one, or recreate the brush.", "W"),
    _h("plane_no_normal", "VBSP", "Brush plane with no normal",
       r"plane with no normal",
       "A brush face has no valid direction - the brush is degenerate/invalid.",
       "Find the brush from the message (entity and brush number), delete it and rebuild it; use "
       "'Check for Problems' (Alt+P) to locate other invalid solids."),
    _h("duplicate_plane", "VBSP", "Duplicate plane",
       r"duplicate plane",
       "Two faces of the same brush lie on the same plane, so the brush is invalid.",
       "Find the brush from the message, delete it and rebuild it (often caused by clipping or "
       "vertex editing).", "W"),
    _h("mixed_face_contents", "VBSP", "Mixed face contents",
       r"mixed face contents",
       "The faces of one brush use materials with conflicting content types (for example water, "
       "clip or sky materials mixed with normal solid ones).",
       "Give all faces of the brush materials of the same kind (use nodraw for faces nobody sees)."),
    _h("bad_planenum", "VBSP", "Bad plane number",
       r"Bad planenum",
       "Hammer saved the file incorrectly, or overlapping brushes (often a bad carve) produced an invalid plane.",
       "Re-save the map and compile again; if it persists, delete and rebuild the recently carved/overlapping brushes."),
    _h("tried_parent", "VBSP", "Node without parent",
       r"Tried parent",
       "A compile node has no parent node. It is very rare and usually caused by vertex-editing "
       "errors that passed Hammer's safety checks.",
       "Find recently vertex-edited brushes, delete and rebuild them."),
    _h("with_splits", "VBSP", "Brush face split",
       r"\bwith splits\b",
       "A brush face has been split into pieces, which is undesirable.",
       "Look for tiny brush penetrations, such as the tip of a spike touching the side of a very "
       "small brush, and move the brushes apart or align them."),
    _h("tjunctions", "VBSP", "Too many t-junctions",
       r"Too many t-junctions",
       "Too many t-junctions have to be fixed up. Its exact cause is unknown, but it usually appears "
       "together with leaks in small, enclosed maps.",
       "Seal the leak first; if the message remains, reduce the number of func_detail and func_brush entities."),
    _h("engine_no_model", "VBSP", "Brush entity without brushes",
       r"Engine Error: .* with no model",
       "A brush entity (func_*, trigger_*, ...) has no brushes tied to it, so it does nothing.",
       "Search for the entity by its origin in Hammer's Entity Report (key 'origin', value from the "
       "message); then either delete it or tie brushes to it."),
    _h("displacement_entity", "VBSP", "Displacement on a brush entity",
       r"displacement found on a\(n\) .* entity",
       "A brush entity contains a displacement; displacements are only allowed in the world.",
       "Find the brush entity that has a displacement and remove the displacement, or move that "
       "brush back to the world (Ctrl+T 'Tie to World')."),
    _h("nodraw_displacement", "VBSP", "NODRAW on displacement surface",
       r"NODRAW on displacement surface",
       "A displacement uses the NODRAW material, which prevents its surface from rendering correctly.",
       "In Hammer, find the displacement face by its face ID and replace NODRAW with a visible material "
       "suitable for the surface. Use NODRAW only on hidden brush faces, not displacement surfaces.", "W"),
    _h("displacement_physics", "VBSP", "Cannot compile displacement physics",
       r"Can't compile displacement physics",
       "The collision (physics) data for a displacement could not be built, usually because a "
       "displacement is malformed or degenerate. It makes VBSP exit without writing a BSP.",
       "Check the displacements with 'Check for Problems' (Alt+P), recreate the broken displacement, "
       "and compile VBSP alone until it succeeds."),
    _h("disp_corner_neighbors", "VBSP", "Displacement corner-neighbor overflow",
       r"overflowed .*displacement corner-neighbor",
       "Too many displacements meet at one corner, exceeding the number of neighbours the engine supports.",
       "Rearrange or merge displacements so fewer of them share a single corner point.", "W"),
    _h("overlay_face_count", "VBSP", "Overlay spans too many faces",
       r"Face List Count >= OVERLAY_BSP_FACE_COUNT",
       "An info_overlay covers more faces than the maximum (64). Brushes are cut into many pieces "
       "during compile, so a long overlay or one near stairs/small lightmap scales can touch many faces. "
       "This makes VBSP fail.",
       "Check big (stretched) overlays first, shorten or split them, avoid many world brushes cutting "
       "each other near the overlay, and avoid very low lightmap scales (1-8) on large brushes with overlays."),
    _h("overlay_too_many_faces", "VBSP", "Overlay touches too many faces",
       r"Overlay touching too many faces",
       "An info_overlay touches more than the allowed number of faces (64) and will not render correctly.",
       "Find the overlay from the coordinates/material in the message and shorten it, split it into "
       "several overlays, or simplify the surface it covers.", "W"),
    # ------------------------------------------------------------------ VBSP: materials and models
    _h("model_empty", "VBSP", "Entity without a model",
       r'Error loading studio model ""',
       "A model-based entity (prop_*) has no model set.",
       "Use Hammer's Entity Report to find entities whose 'model' key is empty (or has a placeholder "
       "value) and set a model or delete the entity."),
    _h("dynamic_prop_info_lighting", "VBSP", "Dynamic prop uses info_lighting",
       r"Entity ['\"]prop_dynamic(?:_override)?['\"].*specifies info_lighting",
       "A prop_dynamic or prop_dynamic_override specifies an info_lighting entity. The info_lighting "
       "key is intended for prop_static and does not provide lighting for dynamic props.",
       "Remove the info_lighting key from the dynamic prop. If it needs a lighting origin, use a "
       "supported dynamic lighting setup for the game; reserve info_lighting for prop_static.", "W"),
    _h("static_prop_needs_flag", "VBSP", "Model cannot be a prop_static",
       r"must be compiled with \$staticprop|which must be used on a dynamic entity",
       "A prop_static uses a model that was not compiled with $staticprop (typically a physics/"
       "breakable model). The model will not show up in game.",
       "Use prop_physics or prop_dynamic_override for this model, or choose a model meant for prop_static."),
    _h("model_load", "VBSP", "Model could not be loaded",
       r"Error loading studio model",
       "A model could not be loaded: the wrong model type is used on the entity, the file is missing "
       "or invalid, or the path is wrong.",
       "Change the entity's model (or the entity class), check that the .mdl, .vvd, .vtx and .phy "
       "files exist in the game's models folder, and look for typos in the path."),
    _h("static_prop_outside", "VBSP", "Static prop outside the map",
       r"static prop .*outside (?:of )?the (?:map|world)",
       "A prop_static lies outside the map bounds, so it is dropped.",
       "Move the prop inside the playable area or delete it.", "W"),
    _h("material_not_found", "VBSP", "Material not found",
       r'material "[^"]*" not found',
       "A surface or overlay uses a material that does not exist (no texture or a wrong file name).",
       "Replace the texture (Replace Textures, Ctrl+Shift+R) or copy the missing .vmt/.vtf into the "
       "game's materials folder.", "W"),
    _h("surfaceprop", "VBSP", "Missing surface property",
       r"Can't find surfaceprop",
       "A material has no (or an unknown) $surfaceprop, so the default is used. Often a 'model' "
       "material was applied to a world brush.",
       "Replace 'model' materials on world brushes; for custom materials add a valid $surfaceprop "
       "to the .vmt.", "W"),
    _h("crackmaterial_loop", "VBSP", "Material depends on itself ($crackmaterial)",
       r"depending on itself through materialvar \$crackmaterial",
       "A breakable (glass) material's $crackmaterial refers back to itself. The compiler ignores "
       "the loop and continues, so the map still compiles; only the crack/break effect of that "
       "material is affected.",
       "Usually safe to ignore, especially for stock materials. To remove the warning, edit the "
       ".vmt and point $crackmaterial at a different material (or remove it).", "W"),
    _h("bottommaterial_loop", "VBSP", "Material depends on itself ($bottommaterial)",
       r"depending on itself through materialvar \$bottommaterial",
       "A water material's $bottommaterial refers back to itself. The compiler ignores the loop "
       "and continues.",
       "Open the .vmt and set $bottommaterial to another material (e.g. \"dev/dev_waterbeneath2\"). "
       "Check the name carefully - names that differ only by a letter or prefix are easy to miss.", "W"),
    _h("material_self_reference", "VBSP", "Material depends on itself",
       r"is depending on itself through materialvar",
       "A material refers back to itself through one of its material variables. The compiler "
       "ignores the loop and continues.",
       "Open the .vmt, find the variable named in the message and point it at a different material.", "W"),
    _h("cubemap_multiple_refs", "VBSP", "Face touched by several cubemaps",
       r"Multiple references for cubemap",
       "A face is attached to more than one env_cubemap. The numbers at the end of the message are coordinates.",
       "Use View > Go to Coordinates to find the overlapping env_cubemaps and reassign/move the wrong one."),
    _h("skybox_hdr", "VBSP", "Skybox has no HDR data",
       r"can't load skybox file .* to build the default cubemap"
       r"|no such variable \"\$hdrbasetexture\"",
       "VBSP builds the default cubemap from your skybox and could not find the HDR version of the "
       "sky textures ($hdrbasetexture). It means the skybox set in the map properties does not work with HDR.",
       "Pick a skybox that ships with HDR textures, add $hdrbasetexture to the sky materials, or "
       "ignore it if you only compile LDR. It is rarely the real cause of a failed compile.", "W"),
    _h("vmt_missing_variable", "VBSP/VRAD", "Material parameter missing",
       r'no such variable "\$\w+" for material',
       "Something asked a material for a parameter that its .vmt does not define.",
       "Add the parameter to the .vmt or use a material that defines it. Often harmless for optional parameters.", "W"),
    _h("instance_missing", "VBSP", "Instance file not found",
       r"(?:could ?n.?t|can.?t|cannot|unable to|failed to) (?:open|find|load) .*instance"
       r"|instance .*(?:not found|missing)",
       "A func_instance points at a VMF file that cannot be found.",
       "Check the instance's file path (it is relative to the parent map or the instances folder) "
       "and that the instance file is saved."),
    _h("bad_surface_extents", "VBSP/VRAD", "Bad surface extents",
       r"Bad surface extents|too large to light|lightmap .*too (?:large|big)",
       "A face's lightmap/texture extents are outside the allowed range, typically a very large face "
       "with a very small lightmap scale or an extreme texture scale.",
       "Increase the lightmap scale on large faces (e.g. 16 or higher), split huge brushes into "
       "smaller ones, and check for extreme texture scales."),
    _h("areaportal", "VBSP", "Areaportal problem",
       r"areaportal.*(?:leaked|touches|does not touch|doesn't touch|lost|mismatch|bad|invalid)",
       "A func_areaportal does not work: it must completely fill an opening and separate exactly two areas.",
       "Use the areaportal texture on all faces of the brush, make it thin and extend it slightly "
       "into the surrounding walls, floor and ceiling, and make sure the map does not leak."),
    _h("find_portal_side", "VBSP", "Portal side not found",
       r"FindPortalSide|none of the brushes in leaf",
       "VBSP could not match a portal with a solid brush face. This is usually a consequence of a leaking map.",
       "Fix the leak first (Map > Load Pointfile) and recompile.", "W"),
    _h("hunk_overflow", "Engine", "Engine hunk overflow",
       r"hunk overflow",
       "The engine ran out of memory while loading the map: the map has too much data (geometry, "
       "displacements, props, lighting).",
       "Optimise the map: simplify geometry, reduce displacements and props, increase lightmap scales and "
       "remove unused entities, or split the map."),
    _h("mempool", "VBSP", "Memory pool message",
       r"mempool blocks left in memory",
       "A harmless bookkeeping message printed on exit; it does not affect the map.",
       "Ignore it.", "I"),
    # ------------------------------------------------------------------ VVIS
    _h("loadportals", "VVIS", "Portal file (.prt) not found",
       r"LoadPortals: couldn't read|couldn't read .*\.prt",
       "VVIS cannot find the portal file created by VBSP. VBSP did not create it (usually because of "
       "an error or a leak), or VVIS uses a different path.",
       "Fix the VBSP errors/leak first. Make sure the map name has no capital letters or dots and that "
       "you did not compile VBSP with 'Only entities' after adding areaportals. A light_environment "
       "placed outside the skybox bounds can also cause it."),
    _h("too_many_portals", "VVIS", "Leaf with too many portals",
       r"Leaf .*with too many portals|too many portals",
       "An area contains geometry that is too complex for visibility calculation.",
       "Simplify rooms and corridors and turn smaller structures into func_detail."),
    # ------------------------------------------------------------------ VRAD
    _h("static_prop_bad_vertex_data", "VRAD", "Static prop has invalid vertex data",
       r"Static prop .+?\.mdl has bad vertex data, needs recompile\?",
       "This model contains malformed or degenerate vertex data. VRAD may be unable to light or ray-trace it correctly.",
       "Recompile the model from its QC/source files with the correct game's studiomdl (Crowbar can help). "
       "Check the source mesh for invalid or duplicate vertices, zero-area triangles and broken normals. "
       "If the source files are unavailable, replace the model or remove the prop.", "W"),
    _h("texture_axis", "VRAD", "Texture axis perpendicular to face",
       r"Texture axis perpendicular to face",
       "A surface at the given coordinates has invalid texture alignment values, so it cannot be lit correctly.",
       "Find the surface at the coordinates and align the texture to World (or Face) again."),
    _h("light_styles", "VRAD", "Too many light styles on a face",
       r"Too many light styles on a face",
       "The face at the given coordinates is lit by too many 'effect' lights. Named lights (they "
       "compile both on and off states) and lights with flicker/pattern effects each take a style.",
       "Remove some of the named or flickering lights, remove names from lights that do not need to "
       "be toggled or disable their effects.", "W"),
    _h("degenerate_faces", "VRAD", "Degenerate faces",
       r"\d+ degenerate faces",
       "Some faces have no or negative area, which may cause lighting problems (it can also be related "
       "to transparent textures).",
       "Check brushes for invalid/vertex-edited geometry and zero-area faces (Alt+P) and rebuild them.", "W"),
    _h("face_vectors_parallel", "VRAD", "Face vectors parallel to face normal",
       r"face vectors parallel to face normal",
       "A face has bad texture vectors, so its lighting will look wrong. It often comes from using "
       "'Align to texture' (Alt + right mouse button).",
       "Find the recently modified brush, align the face to World or Face and set the texture "
       "values manually.", "W"),
    _h("lights_rad", "VRAD", "lights.rad not found",
       r"(?:could ?n.?t|can.?t|cannot|unable to|failed to).*lights\.rad",
       "VRAD could not read lights.rad, so self-illuminated textures will not emit light.",
       "Ensure the game's lights.rad exists, or ignore it if you do not use light-emitting textures.", "W"),
]

# one hint per MAX_MAP_* limit, then a generic fallback for unknown limit names
for _name, (_meaning, _fix) in _MAP_LIMITS.items():
    COMPILE_HINTS.append(_h(
        f"limit_{_name.lower()}", "VBSP/VVIS/VRAD", f"Map limit exceeded: MAX_MAP_{_name}",
        rf"\bMAX_MAP_{_name}\b", _meaning, _fix))
COMPILE_HINTS.append(_h(
    "limit_generic", "VBSP/VVIS/VRAD", "Map limit exceeded",
    r"\bMAX_MAP_\w+",
    "A hard limit of the BSP file format was exceeded. The name after MAX_MAP_ shows which structure "
    "ran out - the map is too large or too complex for this engine branch.",
    "Simplify the map: fewer brushes/entities/displacements, func_detail for small structures, props "
    "for detail, or split the level into several maps."))


def find_compile_hint(line: str) -> Optional[CompileHint]:
    """Return the first known hint matching a compile output line, or None."""
    if len(line) < 8:
        return None
    for hint in COMPILE_HINTS:
        if hint.pattern.search(line):
            return hint
    return None


# =============================================================================
# SECTION 7 — VIEWS
# =============================================================================
def _title_row(icon_key: str, icon_color: str, text: str) -> QHBoxLayout:
    row = QHBoxLayout()
    icon = QLabel()
    icon.setPixmap(qicon(icon_key, icon_color).pixmap(18, 18))
    title = QLabel(text)
    title.setStyleSheet("font-size: 15px; font-weight: 600;")
    row.addWidget(icon)
    row.addWidget(title)
    row.addStretch()
    return row


class GameDirsDialog(QDialog):
    dirs_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Game & tools directories")
        self.resize(640, 520)
        cfg = load_dirs_config()
        self._dirs: List[Path] = cfg["dirs"]
        self._hidden: List[str] = load_hidden_games()
        self._changed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)

        self._auto_chk = QCheckBox("Automatically scan Steam libraries")
        self._auto_chk.setToolTip(
            "When off, only the folders listed below are scanned — the game list "
            "then contains exactly what you added yourself.")
        self._auto_chk.setChecked(bool(cfg["auto"]))
        self._auto_chk.toggled.connect(self._on_auto_toggled)
        lay.addWidget(self._auto_chk)

        info = QLabel(
            "Extra folders scanned for gameinfo.txt and compiler executables "
            "(vbsp / vvis / vrad / bspzip and their Tools++ variants).\n"
            "Subfolders are searched automatically, so you can point at a game folder, "
            "a steamapps/common root or a standalone tools directory.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #8b949e; font-size: 12px;")
        lay.addWidget(info)

        self._list = QListWidget()
        self._list.setIconSize(QSize(18, 18))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        add_btn = neutral(QPushButton("Add folder"), "add")
        add_btn.clicked.connect(self._on_add)
        rem_btn = neutral(QPushButton("Remove"), "remove")
        rem_btn.clicked.connect(self._on_remove)
        clr_btn = QPushButton(qicon("delete", "#f85149"), "Clear all")
        clr_btn.clicked.connect(self._on_clear)
        for b in (add_btn, rem_btn, clr_btn):
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        hid_lbl = QLabel("Hidden games (auto-detected but excluded from the list):")
        hid_lbl.setStyleSheet("color: #8b949e; font-size: 12px;")
        lay.addWidget(hid_lbl)

        self._hidden_list = QListWidget()
        self._hidden_list.setIconSize(QSize(18, 18))
        self._hidden_list.setMaximumHeight(110)
        self._hidden_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self._hidden_list)

        unhide_btn = neutral(QPushButton("Unhide selected"), "add")
        unhide_btn.clicked.connect(self._on_unhide)
        lay.addWidget(unhide_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        hint = QLabel("Changes are applied on Close (the game list refreshes automatically).")
        hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        lay.addWidget(hint)

        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self._rebuild()

    def _rebuild(self) -> None:
        self._list.clear()
        for p in self._dirs:
            item = QListWidgetItem(qicon("folder", "#58a6ff"), str(p))
            item.setToolTip(str(p))
            if not p.is_dir():
                item.setForeground(QColor("#f85149"))
                item.setToolTip(f"{p}\n(missing)")
            self._list.addItem(item)

        self._hidden_list.clear()
        for name in self._hidden:
            self._hidden_list.addItem(QListWidgetItem(qicon("hide", "#8b949e"), name))

    def _commit(self) -> None:
        save_dirs_config(self._dirs, self._auto_chk.isChecked())
        save_hidden_games(self._hidden)
        self._changed = True
        self._rebuild()

    def _on_auto_toggled(self, _on: bool) -> None:
        self._commit()

    def _on_add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select a game or tools folder")
        if not path:
            return
        p = Path(path)
        if p not in self._dirs:
            self._dirs.append(p)
            self._commit()

    def _on_remove(self) -> None:
        rows = sorted({i.row() for i in self._list.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            del self._dirs[r]
        self._commit()

    def _on_clear(self) -> None:
        if not self._dirs:
            return
        if QMessageBox.question(
                self, "Clear directories", "Remove all custom directories?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._dirs.clear()
            self._commit()

    def _on_unhide(self) -> None:
        rows = sorted({i.row() for i in self._hidden_list.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            del self._hidden[r]
        self._commit()

    def accept(self) -> None:
        if self._changed:
            self.dirs_changed.emit()
        super().accept()

    def reject(self) -> None:
        if self._changed:
            self.dirs_changed.emit()
        super().reject()


class GameSelectorView(QWidget):
    game_selected = Signal(object)
    refresh = Signal()

    def __init__(self, model: GameModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        header = _title_row("game", "#58a6ff", "Games")
        tools_btn = neutral(QPushButton("Game && tools"), "tools")
        tools_btn.setToolTip("Add or remove folders scanned for games and compilers")
        tools_btn.clicked.connect(self._open_dirs_dialog)
        header.addWidget(tools_btn)
        btn = neutral(QPushButton("Refresh"), "refresh")
        btn.setToolTip("Refresh game list")
        btn.clicked.connect(self.refresh.emit)
        header.addWidget(btn)
        lay.addLayout(header)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setIconSize(QSize(20, 20))
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        lay.addWidget(self._list)

        model.games_changed.connect(self._rebuild)
        model.game_selected.connect(self._highlight)
        self._rebuild()

    def _open_dirs_dialog(self) -> None:
        dlg = GameDirsDialog(self)
        dlg.dirs_changed.connect(self.refresh.emit)
        dlg.exec()

    def _on_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_hide = menu.addAction(qicon("hide"), f'Hide "{item.text()}" from list')
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is act_hide:
            hide_game(item.text())
            logger.info("Game hidden: %s", item.text())
            self.refresh.emit()

    def _rebuild(self) -> None:
        selected = self._model.selected()
        self._list.blockSignals(True)
        self._list.clear()
        for g in self._model.all_games():
            plus = g.tools_plus_plus
            item = QListWidgetItem(qicon("tools_plus" if plus else "game",
                                         "#ffa657" if plus else "#58a6ff"), g.name)
            tip = f"Path: {g.path}"
            if g.appid:
                tip += f"\nAppID: {g.appid}"
            if plus:
                tip += "\nTools++ compilers detected"
            item.setToolTip(tip)
            self._list.addItem(item)
            if g is selected:
                self._list.setCurrentRow(self._list.count() - 1)
        self._list.blockSignals(False)

    def _on_row_changed(self, row: int) -> None:
        games = self._model.all_games()
        if 0 <= row < len(games):
            self._model.select(games[row])

    def _highlight(self, game: GameConfig) -> None:
        games = self._model.all_games()
        for i, g in enumerate(games):
            if g is game:
                self._list.blockSignals(True)
                self._list.setCurrentRow(i)
                self._list.blockSignals(False)
        self.game_selected.emit(game)


class MapListView(QWidget):
    def __init__(self, model: MapListModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        header = _title_row("map", "#7ee787", "Maps (batch)")
        add_btn = neutral(QPushButton("Add"), "add")
        add_btn.clicked.connect(self._on_add)
        rem_btn = neutral(QPushButton("Remove"), "remove")
        rem_btn.clicked.connect(self._on_remove)
        clr_btn = QPushButton(qicon("delete", "#f85149"), "Clear")
        clr_btn.clicked.connect(lambda: self._model.clear_all())
        for b in (add_btn, rem_btn, clr_btn):
            header.addWidget(b)
        lay.addLayout(header)

        self._view = QListView()
        self._view.setModel(self._model)
        self._view.setDragEnabled(True)
        self._view.setAcceptDrops(True)
        self._view.setDropIndicatorShown(True)
        self._view.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self._view)

        self._count_lbl = QLabel()
        self._count_lbl.setStyleSheet("color: #8b949e; font-size: 11px;")
        lay.addWidget(self._count_lbl)

        for sig in (model.rowsInserted, model.rowsRemoved, model.dataChanged, model.modelReset):
            sig.connect(lambda *_: self._update_count())
        self._update_count()

    def _update_count(self) -> None:
        self._count_lbl.setText(
            f"{len(self._model.enabled_paths())} of {self._model.rowCount()} maps queued")

    def _on_add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select map files", "", "Source Maps (*.vmf *.bsp);;All Files (*)")
        if files:
            self._model.add_paths([Path(f) for f in files])

    def _on_remove(self) -> None:
        self._model.remove_rows([i.row() for i in self._view.selectedIndexes()])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            self._model.add_paths([Path(u.toLocalFile()) for u in event.mimeData().urls()])
            event.acceptProposedAction()


class PresetView(QWidget):
    def __init__(self, model: PresetModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        header = _title_row("presets", "#d2a8ff", "Presets")
        new_btn = neutral(QPushButton("New"), "add")
        new_btn.clicked.connect(self._on_add)
        clone_btn = neutral(QPushButton("Clone"), "clone")
        clone_btn.clicked.connect(self._on_clone)
        del_btn = QPushButton(qicon("delete", "#f85149"), "Delete")
        del_btn.clicked.connect(self._on_delete)
        for b in (new_btn, clone_btn, del_btn):
            header.addWidget(b)
        lay.addLayout(header)

        self._list = QListWidget()
        self._list.setIconSize(QSize(18, 18))
        self._list.currentRowChanged.connect(self._on_row_changed)
        lay.addWidget(self._list)

        model.changed.connect(self._rebuild)
        model.preset_selected.connect(self._highlight)
        self._rebuild()

    def _rebuild(self) -> None:
        sel = self._model.selected()
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._model.all_presets():
            item = QListWidgetItem(qicon("preset_item", "#d2a8ff"), p.name)
            if p.map_regex:
                item.setToolTip(f"Only maps matching: {p.map_regex}")
            self._list.addItem(item)
            if p is sel:
                self._list.setCurrentRow(self._list.count() - 1)
        self._list.blockSignals(False)

    def _on_row_changed(self, row: int) -> None:
        presets = self._model.all_presets()
        if 0 <= row < len(presets):
            self._model.select(presets[row])

    def _highlight(self, preset: Optional[Preset]) -> None:
        self._list.blockSignals(True)
        for i, p in enumerate(self._model.all_presets()):
            if p is preset:
                self._list.setCurrentRow(i)
        self._list.blockSignals(False)

    def _on_add(self) -> None:
        name, ok = QInputDialog.getText(self, "New Preset", "Preset name:")
        if ok and name.strip():
            self._model.select(self._model.add(Preset(name=name.strip())))

    def _on_clone(self) -> None:
        sel = self._model.selected()
        if sel:
            self._model.select(self._model.clone(sel))

    def _on_delete(self) -> None:
        sel = self._model.selected()
        if sel and QMessageBox.question(
                self, "Delete preset", f'Delete preset "{sel.name}"?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._model.remove(sel)


class ProcessConfigView(QObject):
    """Owns two independent panels placed in different window columns:
    stages_panel (compile stages list) and params_panel (parameters of the
    selected stage, plus a live command-line preview like CompilePal++)."""

    def __init__(self, preset_model: PresetModel, game_model: GameModel,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pm = preset_model
        self._gm = game_model
        self._current: Optional[Preset] = preset_model.selected()
        self._current_row: int = -1
        self._cards: List[Tuple[QWidget, str]] = []

        self.stages_panel = QWidget()
        self.params_panel = QWidget()
        self._build_stages()
        self._build_params()

        preset_model.preset_selected.connect(self._on_preset_selected)
        game_model.game_selected.connect(
            lambda _g: self._show_params(self._proc_list.currentRow()))
        self._reload_processes()

    # ---- stages panel -------------------------------------------------------
    def _build_stages(self) -> None:
        lay = QVBoxLayout(self.stages_panel)
        lay.setContentsMargins(12, 12, 12, 12)

        header = _title_row("processes", "#f778ba", "Compile stages")
        add = neutral(QPushButton(), "add")
        add.setFixedWidth(34)
        add.setToolTip("Add a stage")
        add.clicked.connect(self._on_add_process)
        remove = neutral(QPushButton(), "remove")
        remove.setFixedWidth(34)
        remove.setToolTip("Remove the selected stage")
        remove.clicked.connect(self._on_remove_process)
        header.addWidget(remove)
        header.addWidget(add)
        lay.addLayout(header)

        self._proc_list = QListWidget()
        self._proc_list.setIconSize(QSize(20, 20))
        self._proc_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._proc_list.currentRowChanged.connect(self._show_params)
        self._proc_list.itemChanged.connect(self._on_proc_check_changed)
        self._proc_list.model().rowsMoved.connect(
            lambda *_: QTimer.singleShot(0, self._apply_reorder))
        lay.addWidget(self._proc_list)

    # ---- parameters panel ---------------------------------------------------
    def _build_params(self) -> None:
        lay = QVBoxLayout(self.params_panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        header = _title_row("presets", "#f778ba", "Parameters")
        custom_btn = neutral(QPushButton("Custom argument"), "param_add")
        custom_btn.setToolTip("Add a custom command line argument to the selected stage")
        custom_btn.clicked.connect(self._on_custom_argument)
        header.addWidget(custom_btn)
        lay.addLayout(header)

        self._param_search = QLineEdit()
        self._param_search.setPlaceholderText("Search selected stage parameters…")
        self._param_search.setClearButtonEnabled(True)
        self._param_search.textChanged.connect(self._apply_filter)
        lay.addWidget(self._param_search)

        self._param_count = QLabel()
        self._param_count.setStyleSheet("color: #8b949e; font-size: 12px;")
        lay.addWidget(self._param_count)

        self._params_scroll = QScrollArea()
        self._params_scroll.setWidgetResizable(True)
        self._params_scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(self._params_scroll, 1)

        # Live command-line preview of the applied parameters (CompilePal++
        # style): options + basis string with $game$ / $map$ / $bsp$ tokens.
        preview_row = QHBoxLayout()
        self._cmd_preview = QLineEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setToolTip(
            "Command line for the selected stage. $game$, $map$, $bsp$ and "
            "$mapname$ are substituted with real paths when the compile runs.")
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        preview_row.addWidget(self._cmd_preview, 1)
        copy_btn = neutral(QPushButton("Copy"), "copy")
        copy_btn.setToolTip("Copy the command line to the clipboard")
        copy_btn.clicked.connect(self._copy_preview)
        preview_row.addWidget(copy_btn)
        lay.addLayout(preview_row)

    def _copy_preview(self) -> None:
        text = self._cmd_preview.text()
        if text:
            QApplication.clipboard().setText(text)

    def _refresh_preview(self) -> None:
        proc = self._selected_proc()
        if proc is None:
            self._cmd_preview.setText("")
            return
        game = self._gm.selected()
        self._cmd_preview.setText(
            proc.preview_command(allow_plus=bool(game and game.plus_for(proc.kind))))

    def _apply_filter(self, txt: str) -> None:
        t = txt.strip().lower()
        for card, text in self._cards:
            card.setVisible(not t or t in text)

    def _on_custom_argument(self) -> None:
        proc = self._selected_proc()
        if proc is not None:
            self._on_add_param(proc, self._current_row)

    # ---- process list logic -------------------------------------------------
    def _on_preset_selected(self, preset: Optional[Preset]) -> None:
        self._current = preset
        self._reload_processes()

    def _selected_proc(self) -> Optional[ProcessConfig]:
        item = self._proc_list.currentItem()
        if not self._current or item is None:
            return None
        idx = item.data(Qt.ItemDataRole.UserRole)
        return self._current.processes[idx] if 0 <= idx < len(self._current.processes) else None

    @staticmethod
    def _stage_icon(p: ProcessConfig) -> QIcon:
        key = f"stage_{p.kind.name.lower()}"
        return qicon(key if key in _ICON_MAP else "proc_item")

    def refresh_stage_icons(self) -> None:
        """Re-color the stage icons in place (used on theme switch) instead of
        rebuilding the list and every parameter card."""
        procs = self._current.processes if self._current else []
        self._proc_list.blockSignals(True)   # setIcon fires itemChanged
        try:
            for i in range(self._proc_list.count()):
                item = self._proc_list.item(i)
                idx = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(idx, int) and 0 <= idx < len(procs):
                    item.setIcon(self._stage_icon(procs[idx]))
        finally:
            self._proc_list.blockSignals(False)

    def set_active_stage(self, kind: Optional[str], frame: int = 0) -> None:
        """Animate the icon for the compiler stage currently running."""
        active = kind.lower() if kind else None
        procs = self._current.processes if self._current else []
        self._proc_list.blockSignals(True)
        try:
            for row in range(self._proc_list.count()):
                item = self._proc_list.item(row)
                idx = item.data(Qt.ItemDataRole.UserRole)
                if not isinstance(idx, int) or not 0 <= idx < len(procs):
                    continue
                proc = procs[idx]
                stage = proc.kind.name.lower()
                icon = self._stage_icon(proc)
                if stage == active and stage in {"vbsp", "vvis", "vrad"}:
                    size = 24
                    # Render animation frames at higher resolution so Qt can
                    # downsample them cleanly to the list's 20 px icon size.
                    dpr = 4.0
                    pixel_size = int(size * dpr)
                    source = icon.pixmap(pixel_size, pixel_size)
                    canvas = QPixmap(pixel_size, pixel_size)
                    canvas.setDevicePixelRatio(dpr)
                    canvas.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(canvas)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    if stage == "vbsp":
                        painter.translate(size / 2, size / 2)
                        painter.rotate((frame * 12) % 360)
                        painter.drawPixmap(-size / 2, -size / 2, size, size, source)
                    elif stage == "vvis":
                        # Preserve the eye outline, then move the large iris as one shape.
                        painter.drawPixmap(0, 0, size, size, source)
                        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(Qt.GlobalColor.transparent)
                        # Clear the full original iris, including its antialiased edge.
                        painter.drawEllipse(7, 7, 10, 10)
                        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                        iris_x = size / 2 + math.sin(frame * 0.28) * 2.6
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor(_ICON_FG["color"]))
                        painter.drawEllipse(int(iris_x - 3), 9, 6, 6)
                    else:
                        # Fade a warm glow in and out, with rays appearing as it lights up.
                        glow = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(frame * 0.32))
                        gradient = QRadialGradient(size / 2, size / 2, 11)
                        gradient.setColorAt(0.0, QColor(255, 190, 70, int(115 * glow)))
                        gradient.setColorAt(1.0, QColor(255, 190, 70, 0))
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(gradient)
                        painter.drawEllipse(1, 1, size - 2, size - 2)
                        painter.drawPixmap(0, 0, size, size, source)
                        painter.setPen(QPen(QColor(255, 205, 105, int(220 * glow)), 1.4))
                        for angle in range(0, 360, 45):
                            painter.save()
                            painter.translate(size / 2, size / 2)
                            painter.rotate(angle)
                            painter.drawLine(0, -10, 0, -12)
                            painter.restore()
                    painter.end()
                    item.setIcon(QIcon(canvas))
                else:
                    item.setIcon(icon)
        finally:
            self._proc_list.blockSignals(False)

    def _reload_processes(self, select: Optional[ProcessConfig] = None) -> None:
        select = select or self._selected_proc()
        procs = self._current.processes if self._current else []
        self._proc_list.blockSignals(True)
        self._proc_list.clear()
        row = -1
        for i, p in enumerate(procs):
            item = QListWidgetItem(self._stage_icon(p), p.kind.value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if p.enabled else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._proc_list.addItem(item)
            if p is select:
                row = i
        if row < 0 and procs:
            row = 0
        self._proc_list.setCurrentRow(row)
        self._proc_list.blockSignals(False)
        self._show_params(row)

    def _apply_reorder(self) -> None:
        if not self._current:
            return
        procs = self._current.processes
        order = [self._proc_list.item(i).data(Qt.ItemDataRole.UserRole)
                 for i in range(self._proc_list.count())]
        selected = self._selected_proc()
        if sorted(order) == list(range(len(procs))):
            self._current.processes = [procs[i] for i in order]
            self._pm.touch()
        self._reload_processes(select=selected)

    def _on_proc_check_changed(self, item: QListWidgetItem) -> None:
        if not self._current:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if 0 <= idx < len(self._current.processes):
            self._current.processes[idx].enabled = (item.checkState() == Qt.CheckState.Checked)
            self._pm.touch()

    def _on_add_process(self) -> None:
        if not self._current:
            return
        kind, ok = QInputDialog.getItem(self.stages_panel, "Add process", "Process type:",
                                        [k.value for k in PROCESS_ORDER], 0, False)
        if ok and kind:
            proc = make_process(ProcessKind(kind))
            self._current.processes.append(proc)
            self._pm.touch()
            self._reload_processes(select=proc)

    def _on_remove_process(self) -> None:
        proc = self._selected_proc()
        if not self._current or proc is None:
            return
        if QMessageBox.question(
                self.stages_panel, "Remove process", f"Remove {proc.kind.value} from preset?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._current.processes = [p for p in self._current.processes if p is not proc]
            self._pm.touch()
            self._reload_processes()

    # ---- parameter cards ----------------------------------------------------
    def _show_params(self, row: int) -> None:
        self._current_row = row
        procs = self._current.processes if self._current else []

        # Remember scroll position so re-sorts don't jump around
        vsb = self._params_scroll.verticalScrollBar()
        prev_scroll = vsb.value() if vsb else 0

        # Dispose the previous container properly (takeWidget orphans it)
        old = self._params_scroll.takeWidget()
        if old is not None:
            old.deleteLater()

        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        if not (0 <= row < len(procs)):
            hint = QLabel("Select a stage to edit its parameters")
            hint.setStyleSheet("color: #8b949e; font-style: italic;")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(hint)
            self._param_count.setText("")
            self._cards = []
            self._params_scroll.setWidget(container)
            self._cmd_preview.setText("")
            return

        proc = procs[row]
        if proc.kind not in (ProcessKind.SHUTDOWN, ProcessKind.COPY):
            lay.addLayout(self._exe_row(proc))

        def update_count() -> None:
            n = sum(1 for p in proc.parameters if p.enabled)
            self._param_count.setText(
                f"{proc.kind.value} · {len(proc.parameters)} options · {n} enabled")
            self._refresh_preview()
        update_count()

        if not proc.parameters:
            empty = QLabel("No parameters for this process")
            empty.setStyleSheet("color: #8b949e; font-style: italic;")
            lay.addWidget(empty)

        # Enabled parameters float to the top; order inside each group is stable
        ordered = sorted(enumerate(proc.parameters),
                         key=lambda t: (not t[1].enabled, t[0]))

        self._cards = []
        for orig_idx, param in ordered:
            card, text = self._param_card(proc, param, row, update_count)
            lay.addWidget(card)
            self._cards.append((card, text))

        lay.addStretch(1)
        self._params_scroll.setWidget(container)
        self._apply_filter(self._param_search.text())
        QTimer.singleShot(
            0, lambda: self._params_scroll.verticalScrollBar().setValue(prev_scroll))

    def _param_card(self, proc: ProcessConfig, param: Parameter, proc_row: int,
                    update_count) -> Tuple[QWidget, str]:
        spec = spec_for(proc.kind, param.name)
        game = self._gm.selected()
        plus_ok = bool(game and game.plus_for(proc.kind))
        locked = bool(spec and spec.plus_only and not plus_ok)
        incompatible = bool(spec and spec.games and game and game.appid
                            and game.appid not in spec.games)
        locked = locked or incompatible

        card = QWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(4, 6, 4, 6)
        v.setSpacing(2)

        top = QHBoxLayout()
        cb = QCheckBox(spec.name if spec else param.name)
        cb.setChecked(param.enabled)
        top.addWidget(cb)
        flag_lbl = QLabel(param.name)
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        flag_lbl.setFont(mono)
        flag_lbl.setStyleSheet("color: #8b949e;")
        top.addWidget(flag_lbl)
        top.addStretch(1)
        if spec is None:
            rm = QPushButton(qicon("delete", "#f85149"), "")
            rm.setToolTip("Remove parameter")
            rm.setFixedWidth(32)
            rm.clicked.connect(lambda _=False, pr=proc, pa=param, r=proc_row:
                               self._remove_param(pr, pa, r))
            top.addWidget(rm)
        v.addLayout(top)

        desc = spec.desc if spec else "Custom parameter."
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet("color: #8b949e; font-size: 11px;")
        v.addWidget(d)

        if spec is not None and spec.warning:
            warn = QLabel(f"⚠ {spec.warning}")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #d29922; font-size: 10px; font-style: italic;")
            v.addWidget(warn)

        if spec is not None and spec.plus_only:
            note = QLabel("Tools++ only.")
            note.setStyleSheet("color: #d29922; font-size: 10px;")
            v.addWidget(note)

        if spec is not None and spec.games:
            names = ", ".join(_APPID_NAMES.get(g, str(g)) for g in spec.games)
            note = QLabel(f"Only for: {names}")
            note.setStyleSheet("color: #d29922; font-size: 10px;")
            v.addWidget(note)

        if spec is not None and spec.multi and param.kind != "flag":
            note = QLabel("Can be used more than once - separate values with ;")
            note.setStyleSheet("color: #8b949e; font-size: 10px;")
            v.addWidget(note)

        val_w: Optional[QWidget] = None
        if param.kind != "flag":
            if param.kind == "int":
                sb = ClickToFocusSpinBox()
                sb.setRange(-1000000, 1000000)
                try:
                    sb.setValue(int(param.value))
                except (TypeError, ValueError):
                    sb.setValue(0)
                sb.valueChanged.connect(lambda v_, p=param: self._set_attr(p, "value", str(v_)))
                val_w = sb
            elif param.kind in ("file", "dir"):
                wrap = QWidget()
                hl = QHBoxLayout(wrap)
                hl.setContentsMargins(0, 0, 0, 0)
                edit = QLineEdit(str(param.value or ""))
                edit.textChanged.connect(lambda t, p=param: self._set_attr(p, "value", t))
                btn = neutral(QPushButton("File" if param.kind == "file" else "Dir"),
                              "file" if param.kind == "file" else "folder")
                btn.clicked.connect(
                    lambda _=False, e=edit, k=param.kind, m=bool(spec and spec.multi):
                    self._browse(e, k, append=m))
                hl.addWidget(edit, 1)
                hl.addWidget(btn)
                val_w = wrap
            else:
                edit = QLineEdit("" if param.value is None else str(param.value))
                edit.textChanged.connect(lambda t, p=param: self._set_attr(p, "value", t))
                val_w = edit
            v.addWidget(val_w)

        if locked:
            cb.setEnabled(False)
            cb.setToolTip("Not supported by the selected game" if incompatible
                          else "Requires Tools++ compilers for the selected game")
        if val_w is not None:
            val_w.setEnabled(param.enabled and not locked)

        def on_toggled(on: bool, p=param, vw=val_w, uc=update_count, lk=locked,
                       rr=proc_row) -> None:
            p.enabled = on
            if vw is not None:
                vw.setEnabled(on and not lk)
            uc()
            self._pm.touch()
            # Re-sort: enabled parameters move to the top of the list
            self._show_params(rr)
        cb.toggled.connect(on_toggled)

        return card, f"{param.name} {desc}".lower()

    def _exe_row(self, proc: ProcessConfig) -> QHBoxLayout:
        game = self._gm.selected()
        default = game.executable_for(proc.kind) if game else None
        row = QHBoxLayout()
        edit = QLineEdit(proc.custom_executable or "")
        edit.setPlaceholderText(str(default) if default else "Path to executable")
        edit.textChanged.connect(lambda t, p=proc: self._set_exe(p, t))
        btn = neutral(QPushButton("Browse"), "folder")
        btn.clicked.connect(lambda _=False, e=edit: self._browse(e, "file"))
        row.addWidget(QLabel("Executable:" if proc.kind == ProcessKind.CUSTOM else "Override:"))
        row.addWidget(edit, 1)
        row.addWidget(btn)
        return row

    def _set_attr(self, obj: Any, name: str, value: Any) -> None:
        setattr(obj, name, value)
        self._refresh_preview()
        self._pm.touch()

    def _set_exe(self, proc: ProcessConfig, text: str) -> None:
        proc.custom_executable = text.strip() or None
        self._pm.touch()

    def _browse(self, edit: QLineEdit, kind: str, append: bool = False) -> None:
        if kind == "dir":
            path = QFileDialog.getExistingDirectory(self.params_panel, "Select folder")
        else:
            path, _ = QFileDialog.getOpenFileName(self.params_panel, "Select file",
                                                  "", "All Files (*)")
        if path:
            current = edit.text().strip().rstrip(";").strip()
            edit.setText(f"{current};{path}" if (append and current) else path)

    def _remove_param(self, proc: ProcessConfig, param: Parameter, row: int) -> None:
        proc.parameters = [p for p in proc.parameters if p is not param]
        self._pm.touch()
        self._show_params(row)

    def _on_add_param(self, proc: ProcessConfig, row: int) -> None:
        name, ok = QInputDialog.getText(
            self.params_panel, "Add parameter", "Parameter name (e.g. -fast, -threads):")
        name = name.strip()
        if not ok or not name:
            return
        spec = spec_for(proc.kind, name)
        if spec is not None:
            for p in proc.parameters:
                if p.name == name:
                    p.enabled = True
            self._pm.touch()
            self._show_params(row)
            return
        kind, ok = QInputDialog.getItem(self.params_panel, "Parameter type", "Type:",
                                        ["flag", "string", "int", "file", "dir"], 0, False)
        if not ok:
            return
        proc.parameters.append(Parameter(name, True if kind == "flag" else "", True, kind))
        self._pm.touch()
        self._show_params(row)


class LogView(QWidget):
    SEVERITY_COLORS = {
        LogSeverity.DEBUG.value: "#8b949e",
        LogSeverity.OK.value: "#3fb950",
        LogSeverity.WARNING.value: "#d29922",
        LogSeverity.ERROR.value: "#f85149",
        LogSeverity.HINT.value: "#58a6ff",
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._counts = {s.value: 0 for s in LogSeverity}
        self._hints_shown: set = set()   # hint keys already explained for the current map
        self._live = False               # True while an unterminated "live" line is at the end
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        header = _title_row("log", "#58a6ff", "Compile output")
        clr = neutral(QPushButton("Clear"), "clear")
        clr.clicked.connect(self.clear)
        header.addWidget(clr)
        lay.addLayout(header)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(10000)
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(font)
        lay.addWidget(self._text)

        badges = QHBoxLayout()
        self._badges = {
            LogSeverity.ERROR.value: self._make_badge("#f85149"),
            LogSeverity.WARNING.value: self._make_badge("#d29922"),
            LogSeverity.INFO.value: self._make_badge("#58a6ff"),
            LogSeverity.OK.value: self._make_badge("#3fb950"),
        }
        for lbl in self._badges.values():
            badges.addWidget(lbl)
        badges.addStretch()
        lay.addLayout(badges)
        self._refresh_badges()

    @staticmethod
    def _make_badge(color: str) -> QLabel:
        lbl = QLabel()
        lbl.setStyleSheet(
            f"background-color: {rgba(color, 34)}; color: {color}; "
            f"border: 1px solid {rgba(color, 102)}; border-radius: 10px; "
            f"padding: 4px 10px; font-weight: 600; font-size: 11px;")
        return lbl

    def _refresh_badges(self) -> None:
        names = {LogSeverity.ERROR.value: "Errors", LogSeverity.WARNING.value: "Warnings",
                 LogSeverity.INFO.value: "Info", LogSeverity.OK.value: "OK"}
        for sev, lbl in self._badges.items():
            lbl.setText(f"{names[sev]}: {self._counts[sev]}")

    def _drop_live(self) -> None:
        """Remove the live (unterminated) last line, if there is one."""
        if not self._live:
            return
        cursor = QTextCursor(self._text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock,
                            QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._live = False

    @Slot(str)
    def set_live(self, text: str) -> None:
        """Show/update the in-progress output line (e.g. VVIS PortalFlow) in place.
        It is not counted and is replaced by the final line when that arrives."""
        sb = self._text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self._drop_live()
        if text:
            cursor = QTextCursor(self._text.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text, QTextCharFormat())
            self._live = True
        if at_bottom:
            sb.setValue(sb.maximum())

    @Slot(str, int)
    def append(self, message: str, severity: int) -> None:
        self._drop_live()
        hint: Optional[CompileHint] = None
        if severity not in (LogSeverity.DEBUG.value, LogSeverity.HINT.value):
            if message.startswith(LOG_MAP_MARKER):
                self._hints_shown.clear()   # new map -> explain known errors again
            else:
                hint = find_compile_hint(message)
                if hint is not None and hint.severity.value > severity:
                    severity = hint.severity.value   # known problem: show at least at hint severity
        self._counts[severity] = self._counts.get(severity, 0) + 1

        entries: List[Tuple[str, int]] = [(message, severity)]
        if hint is not None and hint.key not in self._hints_shown:
            self._hints_shown.add(hint.key)
            entries.extend((text, LogSeverity.HINT.value) for text in hint.lines())
        self._write(entries)
        self._refresh_badges()

    def _write(self, entries: List[Tuple[str, int]]) -> None:
        sb = self._text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        cursor = QTextCursor(self._text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for text, sev in entries:
            fmt = QTextCharFormat()
            color_hex = self.SEVERITY_COLORS.get(sev)
            if color_hex:  # no explicit color -> follows the theme's text color
                fmt.setForeground(QColor(color_hex))
            cursor.insertText(text + "\n", fmt)
        if at_bottom:
            sb.setValue(sb.maximum())

    def clear(self) -> None:
        self._text.clear()
        self._live = False
        self._hints_shown.clear()
        self._counts = {s.value: 0 for s in LogSeverity}
        self._refresh_badges()


# =============================================================================
# SECTION 8 — THEME & MAIN WINDOW
# =============================================================================
EXTRA_QSS = """
QToolBar { border-bottom: 1px solid palette(mid); padding: 4px; spacing: 6px; }
QToolButton { border: 1px solid transparent; border-radius: 4px; padding: 4px; }
QToolButton:hover { background-color: palette(midlight); border: 1px solid palette(mid); }
QPushButton {
    background-color: palette(button); border: 1px solid palette(mid);
    border-radius: 6px; padding: 6px 14px; min-height: 20px;
}
QPushButton:hover { background-color: palette(midlight); }
QPushButton:pressed { background-color: palette(dark); }
QListWidget, QListView, QPlainTextEdit {
    background-color: palette(base); border: 1px solid palette(mid);
    border-radius: 4px; padding: 4px; outline: none;
}
QListWidget::item { padding: 6px 8px; border-radius: 4px; margin: 1px 0; }
QListWidget::item:selected { background-color: rgba(88, 166, 255, 70); color: palette(text); }
QListWidget::item:hover:!selected { background-color: palette(midlight); }
QLineEdit, QSpinBox {
    background-color: palette(base); border: 1px solid palette(mid);
    border-radius: 4px; padding: 4px 8px; min-height: 20px;
}
QLineEdit:focus { border: 1px solid palette(highlight); }
QLineEdit:read-only { color: palette(text); }
QProgressBar {
    background-color: palette(base); border: 1px solid palette(mid);
    border-radius: 6px; text-align: center; min-height: 20px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #58a6ff, stop:1 #7ee787);
    border-radius: 5px;
}
QStatusBar { border-top: 1px solid palette(mid); font-size: 12px; }
"""


_PALETTE_CACHE: Dict[str, QPalette] = {}

_PALETTE_ROLES = (
    "Window", "WindowText", "Base", "AlternateBase", "ToolTipBase", "ToolTipText",
    "PlaceholderText", "Text", "Button", "ButtonText", "BrightText", "Light",
    "Midlight", "Dark", "Mid", "Shadow", "Highlight", "HighlightedText",
    "Link", "LinkVisited",
)

_THEME_COLORS: Dict[str, Dict[str, str]] = {
    "dark": {
        "Window": "#1c2128", "WindowText": "#e6edf3", "Base": "#0d1117",
        "AlternateBase": "#161b22", "ToolTipBase": "#161b22", "ToolTipText": "#e6edf3",
        "PlaceholderText": "#6e7681", "Text": "#e6edf3", "Button": "#21262d",
        "ButtonText": "#e6edf3", "BrightText": "#ffffff", "Light": "#30363d",
        "Midlight": "#2d333b", "Dark": "#161b22", "Mid": "#30363d", "Shadow": "#010409",
        "Highlight": "#1f6feb", "HighlightedText": "#ffffff",
        "Link": "#58a6ff", "LinkVisited": "#bc8cff",
    },
    "light": {
        "Window": "#f6f7f9", "WindowText": "#1f2328", "Base": "#fbfcfd",
        "AlternateBase": "#f1f3f5", "ToolTipBase": "#ffffff", "ToolTipText": "#1f2328",
        "PlaceholderText": "#8c959f", "Text": "#1f2328", "Button": "#fbfcfd",
        "ButtonText": "#1f2328", "BrightText": "#ffffff", "Light": "#ffffff",
        "Midlight": "#eceff2", "Dark": "#c4cad1", "Mid": "#d0d7de", "Shadow": "#8c959f",
        "Highlight": "#1f6feb", "HighlightedText": "#ffffff",
        "Link": "#0969da", "LinkVisited": "#8250df",
    },
}
_DISABLED_TEXT = {"dark": "#6e7681", "light": "#8c959f"}


def _complete_palette(theme: str, src: Optional[QPalette] = None) -> QPalette:
    """A palette with EVERY role of EVERY group set explicitly.

    QApplication.setPalette() resolves roles the palette does not set against the
    previous app palette, so a partial palette lets colors of the old theme bleed
    through. Roles that qdarktheme's own palette does set override ours."""
    key = "dark" if theme == "dark" else "light"
    groups = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
              QPalette.ColorGroup.Disabled)
    pal = QPalette()
    for name in _PALETTE_ROLES:
        role = getattr(QPalette.ColorRole, name, None)
        if role is None:
            continue
        for g in groups:
            pal.setColor(g, role, QColor(_THEME_COLORS[key][name]))
    for name in ("WindowText", "Text", "ButtonText"):
        pal.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, name),
                     QColor(_DISABLED_TEXT[key]))
    if src is not None:
        for name in _PALETTE_ROLES:
            role = getattr(QPalette.ColorRole, name, None)
            if role is None:
                continue
            for g in groups:
                try:
                    if src.isBrushSet(g, role):
                        pal.setBrush(g, role, src.brush(g, role))
                except Exception:
                    pass
    return pal


def theme_palette(theme: str) -> QPalette:
    """Complete palette for a theme, built once and cached."""
    pal = _PALETTE_CACHE.get(theme)
    if pal is None:
        base = None
        if qdarktheme is not None and hasattr(qdarktheme, "load_palette"):
            try:
                base = qdarktheme.load_palette(theme)
            except Exception as e:
                logger.warning("qdarktheme.load_palette failed (%s) — using built-in palette", e)
        pal = _complete_palette(theme, base)
        _PALETTE_CACHE[theme] = pal
    return pal


class MainWindow(QMainWindow):
    discover_requested = Signal()
    _style_ready = False
    _restyle_gen = 0
    _restyle_queue: List[Any] = []
    _restyle_t0 = 0.0

    def __init__(self) -> None:
        super().__init__()
        self._taskbar_icon_handles: List[int] = []
        self.setWindowTitle("Source Compile Tool")
        # The executable's resource icon does not automatically become the
        # Qt window icon. In a PyInstaller build, bundled data lives in
        # _MEIPASS (the _internal directory for onedir builds).
        icon_dir = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR))
        self.setWindowIcon(QIcon(str(icon_dir / "icon.ico")))
        self.resize(1440, 900)

        self._settings = make_settings()
        self._theme: str = str(self._settings.value("theme", "dark") or "dark")

        self._game_model = GameModel()
        self._map_model = MapListModel()
        self._preset_model = PresetModel()

        self._discovery_thread = QThread(self)
        self._discovery = GameDiscoveryController()
        self._discovery.moveToThread(self._discovery_thread)
        self._discovery.finished.connect(self._on_discovery_finished)
        self.discover_requested.connect(self._discovery.run)
        self._discovery_thread.start()

        self._compile = CompileController()
        self._compile.log_line.connect(self._on_log_line)
        self._compile.log_partial.connect(self._on_log_partial)
        self._compile.progress.connect(self._on_progress)
        self._compile.step_start.connect(self._on_step_start)
        self._compile.done_ok.connect(self._on_done_ok)
        self._compile.done_err.connect(self._on_done_err)

        self._build_ui()
        self._apply_theme(self._theme)
        QTimer.singleShot(50, self._kickoff_discovery)
        QTimer.singleShot(600, self._prewarm_theme)

    def _kickoff_discovery(self) -> None:
        self.statusBar().showMessage("Discovering Source Engine games...")
        self.discover_requested.emit()

    def _build_ui(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setObjectName("mainToolbar")
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self._act_compile = QAction(qicon("compile", "#3fb950"), " Compile", self)
        self._act_compile.triggered.connect(self._on_compile_clicked)
        tb.addAction(self._act_compile)
        self._act_cancel = QAction(qicon("cancel", "#f85149"), " Cancel", self)
        self._act_cancel.triggered.connect(self._compile.cancel)
        self._act_cancel.setEnabled(False)
        tb.addAction(self._act_cancel)
        tb.addSeparator()
        self._act_theme = QAction(self)
        self._act_theme.triggered.connect(self._toggle_theme)
        tb.addAction(self._act_theme)

        central = QWidget()
        self.setCentralWidget(central)
        main_lay = QVBoxLayout(central)
        main_lay.setContentsMargins(8, 8, 8, 8)
        main_lay.setSpacing(6)

        self._proc_view = ProcessConfigView(self._preset_model, self._game_model)
        self._log_view = LogView()

        # CompilePal++ style layout:
        #   left column   — games / maps / presets
        #   right top     — compile stages | parameters (side by side)
        #   right bottom  — compile output, spans under BOTH panels above
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        self._game_view = GameSelectorView(self._game_model)
        self._map_view = MapListView(self._map_model)
        self._preset_view = PresetView(self._preset_model)
        self._game_view.refresh.connect(self._kickoff_discovery)
        for view, stretch in ((self._game_view, 2), (self._map_view, 3), (self._preset_view, 2)):
            left_lay.addWidget(self._wrap_group(view), stretch)
        self._splitter.addWidget(left)

        self._right_split = QSplitter(Qt.Orientation.Vertical)
        self._top_split = QSplitter(Qt.Orientation.Horizontal)
        self._top_split.addWidget(self._wrap_group(self._proc_view.stages_panel))
        self._top_split.addWidget(self._wrap_group(self._proc_view.params_panel))
        self._top_split.setSizes([260, 760])
        self._top_split.setStretchFactor(0, 0)
        self._top_split.setStretchFactor(1, 1)
        self._right_split.addWidget(self._top_split)
        self._right_split.addWidget(self._wrap_group(self._log_view))
        self._right_split.setSizes([560, 340])
        self._right_split.setStretchFactor(0, 3)
        self._right_split.setStretchFactor(1, 2)
        self._splitter.addWidget(self._right_split)

        self._splitter.setSizes([360, 1080])
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        main_lay.addWidget(self._splitter, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(4, 4, 4, 4)
        self._step_lbl = QLabel("Idle")
        self._step_lbl.setStyleSheet("color: #8b949e; font-size: 12px;")
        bottom.addWidget(self._step_lbl, 0)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        bottom.addWidget(self._progress, 1)
        self._timer_lbl = QLabel("00:00")
        self._timer_lbl.setStyleSheet(
            "font-family: Consolas, monospace; font-weight: 600; background-color: #30363d; "
            "color: #c9d1d9; padding: 4px 10px; border-radius: 4px;")
        bottom.addWidget(self._timer_lbl, 0)
        main_lay.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._stage_anim_timer = QTimer(self)
        self._stage_anim_timer.setInterval(90)
        self._stage_anim_timer.timeout.connect(self._animate_stage_icon)
        self._stage_anim_kind: Optional[str] = None
        self._stage_anim_frame = 0
        self._t0: Optional[float] = None
        self.statusBar().showMessage("Ready")

        geom = self._settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        state = self._settings.value("windowState")
        if state:
            self.restoreState(state)
        for key, splitter in (("splitter", self._splitter),
                              ("splitter_top", self._top_split),
                              ("splitter_v", self._right_split)):
            raw = self._settings.value(key)
            if raw:
                try:
                    parsed = [int(s) for s in raw]
                    if len(parsed) == 2:
                        splitter.setSizes(parsed)
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def _wrap_group(widget: QWidget) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupPanel")
        frame.setStyleSheet("#groupPanel { background: transparent; "
                            "border: 1px solid palette(mid); border-radius: 8px; }")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(widget)

        effect = QGraphicsOpacityEffect(frame)
        frame.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", frame)
        anim.setDuration(350)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda f=frame: cast(Any, f).setGraphicsEffect(None))
        anim.start()
        return frame

    # ---- discovery ---------------------------------------------------------
    def _on_discovery_finished(self, games: list) -> None:
        self._game_model.replace_games(games)
        cfg = load_dirs_config()
        extra = len(cfg["dirs"])
        msg = f"Discovered {len(games)} game(s)"
        if extra:
            msg += f" (+{extra} custom dir(s) scanned)"
        if not cfg["auto"]:
            msg += " [Steam autoscan off]"
        self.statusBar().showMessage(
            msg if games else
            "No Source games found (add folders via 'Game & tools' or enable Steam autoscan)")

    # ---- compile -----------------------------------------------------------
    def _on_compile_clicked(self) -> None:
        game = self._game_model.selected()
        preset = self._preset_model.selected()
        maps = self._map_model.enabled_paths()
        if not game:
            QMessageBox.warning(self, "Compile", "Select a game first.")
            return
        if not preset or not any(p.enabled for p in preset.processes):
            QMessageBox.warning(self, "Compile",
                                "Select a preset with at least one enabled process.")
            return
        maps = preset.filter_maps(maps)
        if not maps:
            if preset.map_regex:
                QMessageBox.warning(
                    self, "Compile",
                    f'Preset "{preset.name}" only applies to maps matching '
                    f'{preset.map_regex} — add such maps to the queue first.')
            else:
                QMessageBox.warning(self, "Compile", "Add at least one map.")
            return

        self._preset_model.flush()
        self._log_view.clear()
        self._progress.setValue(0)
        self._t0 = time.time()
        self._stage_anim_kind = None
        self._stage_anim_frame = 0
        self._stage_anim_timer.start()
        self._timer.start()
        self._act_compile.setEnabled(False)
        self._act_cancel.setEnabled(True)
        self.statusBar().showMessage("Compiling...")
        self._compile.start(game, preset, maps)

    def _finish_ui(self) -> None:
        self._timer.stop()
        self._stage_anim_timer.stop()
        self._stage_anim_kind = None
        self._proc_view.set_active_stage(None)
        self._act_compile.setEnabled(True)
        self._act_cancel.setEnabled(False)

    def _on_log_line(self, msg: str, sev: int) -> None:
        self._log_view.append(msg, sev)

    def _on_log_partial(self, text: str) -> None:
        self._log_view.set_live(text)

    def _on_progress(self, step: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(int(100 * step / total))

    def _on_step_start(self, name: str) -> None:
        self._step_lbl.setText(name)
        self.statusBar().showMessage(name)
        stage_name = name.split(" — ", 1)[0].split(" – ", 1)[0].strip().lower()
        self._stage_anim_kind = stage_name if stage_name in {"vbsp", "vvis", "vrad"} else None
        self._stage_anim_frame = 0
        self._proc_view.set_active_stage(self._stage_anim_kind)

    def _animate_stage_icon(self) -> None:
        if self._stage_anim_kind is None:
            return
        self._stage_anim_frame += 1
        self._proc_view.set_active_stage(self._stage_anim_kind, self._stage_anim_frame)

    def _on_done_ok(self, elapsed: float) -> None:
        self._finish_ui()
        self._progress.setValue(100)
        self._step_lbl.setText("✓ Complete")
        self.statusBar().showMessage(f"Compile finished in {elapsed:.1f}s")
        QMessageBox.information(self, "Compile finished",
                                f"All steps completed successfully in {elapsed:.1f} seconds.")

    def _on_done_err(self, msg: str, elapsed: float) -> None:
        self._finish_ui()
        if msg == CompileWorker.CANCELLED:
            self._step_lbl.setText("Cancelled")
            self.statusBar().showMessage("Compile cancelled")
            return
        self._step_lbl.setText("✗ Failed")
        self.statusBar().showMessage(f"Compile failed: {msg}")
        QMessageBox.critical(self, "Compile failed",
                             f"Compilation failed after {elapsed:.1f}s:\n\n{msg}")

    def _tick(self) -> None:
        if self._t0 is None:
            return
        m, s = divmod(int(time.time() - self._t0), 60)
        h, m = divmod(m, 60)
        self._timer_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")

    # ---- theme -------------------------------------------------------------
    def _toggle_theme(self) -> None:
        new = "light" if self._theme == "dark" else "dark"
        self._apply_theme(new)
        self._settings.setValue("theme", new)

    def _apply_theme(self, theme: str) -> None:
        self._theme = theme
        _ICON_FG["color"] = "#e6edf3" if theme == "dark" else "#1f2328"
        app_instance = QApplication.instance()
        if not isinstance(app_instance, QApplication):
            return
        app = app_instance

        t0 = time.perf_counter()
        # Re-applying a stylesheet re-parses it and re-polishes every widget
        # (~0.8 s here). So the stylesheet is set exactly once (EXTRA_QSS only
        # uses palette(...) references, i.e. it is theme-independent) and a theme
        # switch is a QPalette swap. Qt caches the *resolved* palette(...) colors
        # per widget, though, so QSS-styled widgets are re-polished too:
        # on-screen ones immediately, off-screen ones in small idle slices.
        self.setUpdatesEnabled(False)
        try:
            pal = theme_palette(theme)
            if not self._style_ready:
                app.setStyle("Fusion")
                app.setPalette(pal)
                app.setStyleSheet(EXTRA_QSS)
                self._style_ready = True
            else:
                app.setPalette(pal)
            for spinbox in self.findChildren(ClickToFocusSpinBox):
                spinbox.refresh_arrow_icons()
            self._restyle_widgets()

            # Keep the elapsed-time badge readable in both palettes.
            if theme == "dark":
                timer_bg, timer_fg = "#30363d", "#c9d1d9"
            else:
                timer_bg, timer_fg = "#e9edf2", "#344054"
            self._timer_lbl.setStyleSheet(
                "font-family: Consolas, monospace; font-weight: 600; "
                f"background-color: {timer_bg}; color: {timer_fg}; "
                "padding: 4px 10px; border-radius: 4px;")

            refresh_neutral_icons()
            self._proc_view.refresh_stage_icons()
            if theme == "dark":
                self._act_theme.setIcon(qicon("theme"))
                self._act_theme.setText(" Switch to light theme")
            else:
                self._act_theme.setIcon(qicon("theme_dark"))
                self._act_theme.setText(" Switch to dark theme")
        finally:
            self.setUpdatesEnabled(True)
        logger.info("Theme '%s' applied in %.0f ms", theme, (time.perf_counter() - t0) * 1000)

    # ---- re-resolve palette(...) colors cached by the stylesheet engine ----
    @staticmethod
    def _repolish(w: QWidget) -> None:
        try:
            st = w.style()
            st.unpolish(w)
            st.polish(w)
            w.update()
        except RuntimeError:  # C++ object already deleted
            pass

    def _restyle_widgets(self) -> None:
        self._restyle_gen += 1
        gen = self._restyle_gen
        widgets = self.findChildren(QWidget)
        widgets.append(self)
        now: List[QWidget] = []
        later: List[QWidget] = []
        for w in widgets:
            try:
                on_screen = w.isVisible() and not w.visibleRegion().isEmpty()
            except RuntimeError:
                continue
            (now if on_screen else later).append(w)
        for w in now:
            self._repolish(w)
        self._restyle_queue = later
        self._restyle_t0 = time.perf_counter()
        if later:
            QTimer.singleShot(0, lambda: self._restyle_step(gen))

    def _restyle_step(self, gen: int) -> None:
        if gen != self._restyle_gen:   # superseded by a newer theme switch
            return
        queue = self._restyle_queue
        deadline = time.perf_counter() + 0.008   # <= ~8 ms per slice, UI stays responsive
        while queue and time.perf_counter() < deadline:
            self._repolish(queue.pop())
        if queue:
            QTimer.singleShot(0, lambda: self._restyle_step(gen))
        else:
            logger.info("Off-screen widgets restyled in background (%.0f ms)",
                        (time.perf_counter() - self._restyle_t0) * 1000)

    # ---- idle pre-warming so the first switch is as fast as the rest -------
    def _prewarm_theme(self) -> None:
        other_color = "#1f2328" if self._theme == "dark" else "#e6edf3"
        theme_palette("dark")
        theme_palette("light")
        keys = {k for w, k in _NEUTRAL if isValid(w)}
        keys |= {k for k in _ICON_MAP if k.startswith("stage_")}
        keys |= {"proc_item", "theme", "theme_dark", "file", "folder", "copy", "param_add"}
        self._prewarm_queue = [(k, other_color) for k in sorted(keys)]
        QTimer.singleShot(0, self._prewarm_step)

    def _prewarm_step(self) -> None:
        for _ in range(3):
            if not self._prewarm_queue:
                return
            key, color = self._prewarm_queue.pop()
            qicon(key, color)
        QTimer.singleShot(0, self._prewarm_step)

    # ---- shutdown ----------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._compile.is_running():
            r = QMessageBox.question(
                self, "Compilation in progress", "A compile is running. Cancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._compile.shutdown()
        self._preset_model.flush()
        self._preset_model.save_state()
        self._map_model.flush()

        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("windowState", self.saveState())
        self._settings.setValue("splitter", self._splitter.sizes())
        self._settings.setValue("splitter_top", self._top_split.sizes())
        self._settings.setValue("splitter_v", self._right_split.sizes())
        self._game_model.save_state()

        if self._discovery_thread.isRunning():
            self._discovery_thread.quit()
            self._discovery_thread.wait(2000)
        event.accept()


# =============================================================================
# SECTION 9 — ENTRY POINT
# =============================================================================
def main() -> int:
    # Give Windows a stable application identity so the taskbar groups the
    # window with this executable's icon instead of using a generic Python/Qt
    # process icon. This must be set before QApplication is created.
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SourceCompileTool")
        except Exception:
            logger.debug("Could not set the Windows AppUserModelID", exc_info=True)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Source Compile Tool")
    app.setOrganizationName("CompilePalX")
    app.setApplicationVersion("2.10.0")
    app_icon_path = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR)) / "icon.ico"
    app.setWindowIcon(QIcon(str(app_icon_path)))

    win = MainWindow()
    app.setWindowIcon(win.windowIcon())
    win.show()
    if IS_WINDOWS:
        _set_windows_taskbar_icon(win, app_icon_path)
    return app.exec()


def _set_windows_taskbar_icon(window: MainWindow, icon_path: Path) -> None:
    """Set both native HWND icons so the Windows taskbar uses our app icon."""
    try:
        user32 = ctypes.windll.user32
        load_image = user32.LoadImageW
        load_image.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        load_image.restype = ctypes.c_void_p
        send_message = user32.SendMessageW
        send_message.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p]
        send_message.restype = ctypes.c_ssize_t

        hwnd = ctypes.c_void_p(int(window.winId()))
        handles = []
        for size, icon_kind in ((16, 0), (32, 1)):
            handle = load_image(None, str(icon_path), 1, size, size, 0x0010)
            if handle:
                handles.append(handle)
                # WM_SETICON: ICON_SMALL=0, ICON_BIG=1.
                send_message(hwnd, 0x0080, icon_kind, handle)

        # Keep HICONs alive for the lifetime of the window.
        window._taskbar_icon_handles = handles
    except Exception:
        logger.debug("Could not set native Windows taskbar icons", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
