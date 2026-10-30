import ctypes
import ctypes.wintypes
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import pystray
from PIL import Image, ImageDraw, ImageTk

from database import ResourceDatabase
from detector.coordinates import CoordinateDetector
from local_resource_db import LocalResourceDB

JOB_ORDER = {
    "Alchimiste": 1, "Bûcheron": 2, "Mineur": 3,
    "Paysan": 4, "Pêcheur": 5, "Divers": 6,
}


# User-editable keyboard configuration. The file is deliberately plain text so
# it can be changed without opening the application settings UI.
CONFIG_FILE = Path(__file__).resolve().parent / "config.txt"
DEFAULT_SETTINGS_KEY = "F9"
DEFAULT_TOGGLE_KEY = "F8"


class HotkeyConfig:
    """Read and validate the two global shortcuts from config.txt."""

    VK_NAMES = {
        **{f"F{i}": 0x6F + i for i in range(1, 13)},
        **{chr(ord("A") + i): 0x41 + i for i in range(26)},
        **{str(i): 0x30 + i for i in range(10)},
        "ESC": 0x1B, "ESCAPE": 0x1B,
        "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
        "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
        "INSERT": 0x2D, "HOME": 0x24, "END": 0x23,
        "PAGEUP": 0x21, "PAGEDOWN": 0x22,
        "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
        "NUM0": 0x60, "NUM1": 0x61, "NUM2": 0x62, "NUM3": 0x63, "NUM4": 0x64,
        "NUM5": 0x65, "NUM6": 0x66, "NUM7": 0x67, "NUM8": 0x68, "NUM9": 0x69,
    }

    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        self.settings_key = DEFAULT_SETTINGS_KEY
        self.toggle_key = DEFAULT_TOGGLE_KEY
        self.settings_vk = self.VK_NAMES[self.settings_key]
        self.toggle_vk = self.VK_NAMES[self.toggle_key]
        self.mtime_ns = None
        self.load()

    @classmethod
    def normalize(cls, value):
        value = str(value or "").strip().upper().replace("-", "").replace(" ", "")
        return value

    @classmethod
    def vk_for(cls, value):
        key = cls.normalize(value)
        return cls.VK_NAMES.get(key)

    def load(self):
        try:
            if not self.path.exists():
                self.path.write_text(
                    "# Raccourcis globaux du Dofus Resource Assistant\n"
                    "# Modifiez les valeurs puis enregistrez le fichier.\n"
                    "# Touches possibles : F1-F12, A-Z, 0-9, ESC, SPACE, ENTER, TAB, etc.\n"
                    "SETTINGS_KEY=F9\n"
                    "TOGGLE_KEY=F8\n",
                    encoding="utf-8",
                )
            values = {}
            for raw in self.path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip().upper()] = self.normalize(value)

            settings = values.get("SETTINGS_KEY", DEFAULT_SETTINGS_KEY)
            toggle = values.get("TOGGLE_KEY", DEFAULT_TOGGLE_KEY)
            settings_vk = self.vk_for(settings)
            toggle_vk = self.vk_for(toggle)
            if settings_vk is None:
                settings, settings_vk = DEFAULT_SETTINGS_KEY, self.VK_NAMES[DEFAULT_SETTINGS_KEY]
            if toggle_vk is None:
                toggle, toggle_vk = DEFAULT_TOGGLE_KEY, self.VK_NAMES[DEFAULT_TOGGLE_KEY]

            # Do not allow one physical key to trigger both actions.
            if settings_vk == toggle_vk:
                settings, settings_vk = DEFAULT_SETTINGS_KEY, self.VK_NAMES[DEFAULT_SETTINGS_KEY]
                toggle, toggle_vk = DEFAULT_TOGGLE_KEY, self.VK_NAMES[DEFAULT_TOGGLE_KEY]

            self.settings_key = settings
            self.toggle_key = toggle
            self.settings_vk = settings_vk
            self.toggle_vk = toggle_vk
            self.mtime_ns = self.path.stat().st_mtime_ns
            return True
        except Exception:
            self.settings_key = DEFAULT_SETTINGS_KEY
            self.toggle_key = DEFAULT_TOGGLE_KEY
            self.settings_vk = self.VK_NAMES[DEFAULT_SETTINGS_KEY]
            self.toggle_vk = self.VK_NAMES[DEFAULT_TOGGLE_KEY]
            return False

    def reload_if_changed(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if self.mtime_ns == mtime:
            return False
        return self.load()


class GlobalHotkeys:
    """Global F8/F9 polling that works even when Dofus owns the focus.

    GetAsyncKeyState reads the physical key state system-wide. Unlike Tk
    bindings, it does not depend on which window is focused, and unlike a
    low-level keyboard hook it is less likely to be blocked by a game/client.
    The keys are never consumed.
    """

    def __init__(self, callback, config):
        self.callback = callback
        self.config = config
        self.previous = {}
        self.user32 = ctypes.windll.user32 if os.name == "nt" else None
        self._sync_keys()

    def _sync_keys(self):
        keys = {self.config.toggle_vk, self.config.settings_vk}
        self.previous = {vk: False for vk in keys}

    def reload_config(self):
        if self.config.reload_if_changed():
            self._sync_keys()
            return True
        return False

    def poll(self):
        if not self.user32:
            return
        for vk in (self.config.toggle_vk, self.config.settings_vk):
            try:
                down = bool(self.user32.GetAsyncKeyState(vk) & 0x8000)
            except Exception:
                down = False
            if down and not self.previous[vk]:
                self.callback(vk)
            self.previous[vk] = down


class ResourceOverlay:
    def __init__(self):
        self.db = ResourceDatabase()

        self.selected_resources = self._read_selected_resources()
        self.resource_distances = self._read_resource_distances()
        self.show_resource_coordinates = str(self.db.get_setting("show_resource_coordinates", self.db.get_setting("show_coordinates", "1"))).lower() in ("1", "true", "yes", "on")

        self.current_coordinates = None
        self.current_results = []
        self.current_map_results = []
        self.settings_window = None
        self._settings_wheel_bind_id = None

        # OCR is deliberately executed outside Tk's main thread. Tesseract can
        # take several hundred milliseconds and must never freeze/blank the UI.
        self._ocr_queue = queue.Queue()
        self._ocr_thread = None
        self._ocr_busy = False
        self._ocr_next_start = 0.0
        self._ocr_started_at = 0.0
        self._ocr_watchdog_timeout = 2.5
        self._last_ocr_error = None
        self._ocr_diagnostic_var = None
        self._live = None
        self._live_catalog_ready = False
        self.resource_vars = {}
        self.resource_distance_vars = {}
        self.resource_category_vars = {}
        self.resource_category_resource_ids = {}
        self._after_id = None
        self._detector_history = []
        self._ocr_reset_generation = 0
        self._closing = False
        self._ocr_stop_event = threading.Event()
        self._resize_active = False
        self._drag_active = False
        self._tray_icon = None
        self._tray_thread = None
        self._tray_quit_requested = threading.Event()
        self.hotkey_config = HotkeyConfig()
        self._hotkey_config_poll_at = 0.0

        self.root = tk.Tk()
        self.root.title("Dofus Resource Assistant")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # Avoid Tk/Windows layered alpha: it can render a borderless window as
        # an opaque black surface on some Windows/Tk configurations.
        self.root.configure(bg="#11161c")
        # Keep the overlay visually translucent while remaining a normal, fully interactive Tk window.
        # This deliberately does not use any click-through/layered-window mechanism.
        self.root.attributes("-alpha", 0.82)

        x = self._read_int_setting("overlay_x", 30)
        y = self._read_int_setting("overlay_y", 180)
        # Default overlay: narrower than the previous 400 px, but tall enough
        # to reserve a real footer for the F8/F9 hints and OCR status.
        w = self._read_int_setting("overlay_width", 320)
        h = self._read_int_setting("overlay_height", 360)
        # V68 used 400x320 as its factory size.  If that old factory value is
        # still stored, migrate it once to the new, more usable default.  Any
        # other size is considered a deliberate user choice and is preserved.
        try:
            stored_w = self.db.get_setting("overlay_width", None)
            stored_h = self.db.get_setting("overlay_height", None)
            if str(stored_w) == "400" and str(stored_h) == "320":
                w, h = 320, 360
        except Exception:
            pass
        self.root.geometry(f"{max(1, w)}x{max(1, h)}+{x}+{y}")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._last_saved_size = None
        self._geometry_save_after = None
        self.root.bind("<Configure>", self._on_configure)

        self._build_ui()

        # Local map data is also used to reject impossible OCR coordinates.
        self._live = LocalResourceDB()
        self._live_catalog_ready = self._live.ready()
        self._ensure_default_resource_distances()

        self.root.deiconify()
        self.root.update_idletasks()
        self._force_topmost()

        self.detector = None
        self._create_detector()
        if self._live_catalog_ready:
            self.debug_label.config(text=self._live.status_text())
        else:
            self.debug_label.config(text="Base locale absente — la base incluse est introuvable")

        self.hotkeys = GlobalHotkeys(self._on_hotkey, self.hotkey_config)
        self._start_tray()
        self.root.after(20, self._poll_hotkeys)
        self.root.after(100, self._poll_tray_quit)
        self.root.after(40, self.update)
        self.root.after(250, self._keep_topmost)

    def _create_detector(self):
        """Create the OCR detector from saved settings, with safe defaults."""
        region = {
            "left": self._read_int_setting("ocr_left", 3),
            "top": self._read_int_setting("ocr_top", 70),
            "width": self._read_int_setting("ocr_width", 91),
            "height": self._read_int_setting("ocr_height", 32),
        }
        default_tw, default_th = 240, 135
        default_tl = max(0, (self.root.winfo_screenwidth() - default_tw) // 2)
        default_tt = max(0, (self.root.winfo_screenheight() - default_th) // 2)
        transition_region = {
            "left": self._read_int_setting("transition_left", default_tl),
            "top": self._read_int_setting("transition_top", default_tt),
            "width": self._read_int_setting("transition_width", default_tw),
            "height": self._read_int_setting("transition_height", default_th),
        }

        # Migrate the legacy factory OCR rectangle to the user-validated Dofus HUD
        # rectangle shown by the preview: left=3, top=70, width=91, height=32.
        # Deliberate custom values are preserved.
        if region == {"left": 0, "top": 70, "width": 95, "height": 30}:
            region = {"left": 3, "top": 70, "width": 91, "height": 32}
            try:
                self.db.set_settings({
                    "ocr_left": 3, "ocr_top": 70, "ocr_width": 91, "ocr_height": 32
                })
            except Exception:
                pass

        # Older releases used a very large 520x90 OCR zone.  The level is not
        # useful to us, so migrate an untouched legacy default to a compact
        # coordinate-only zone.  Custom user values are preserved.
        if region["width"] == 520 and region["height"] == 90:
            region["width"] = 180
            region["height"] = 38
        region["width"] = max(20, region["width"])
        region["height"] = max(20, region["height"])

        # Let the detector find the installed Tesseract automatically.
        # If the user previously saved an explicit path, prefer it.
        command = self.db.get_setting("tesseract_cmd", "")
        validator = self._live.has_map if self._live and self._live.ready() else None
        self.detector = CoordinateDetector(
            region=region,
            tesseract_cmd=str(command).strip() or None,
            coordinate_validator=validator,
            transition_region=transition_region,
        )

    # ---------- window behavior ----------

    def _save_overlay_geometry(self):
        try:
            self.db.set_settings({
                "overlay_x": self.root.winfo_x(),
                "overlay_y": self.root.winfo_y(),
                "overlay_width": self.root.winfo_width(),
                "overlay_height": self.root.winfo_height(),
            })
            self._last_saved_size = (self.root.winfo_width(), self.root.winfo_height())
        except tk.TclError:
            pass

    def _schedule_geometry_save(self):
        if self._geometry_save_after is not None:
            try:
                self.root.after_cancel(self._geometry_save_after)
            except tk.TclError:
                pass
        self._geometry_save_after = self.root.after(500, self._save_overlay_geometry)

    def _on_hotkey(self, key):
        if key == self.hotkey_config.toggle_vk:
            self.toggle_visibility()
        elif key == self.hotkey_config.settings_vk:
            self.show_settings()

    def _poll_hotkeys(self):
        if self._closing:
            return
        if hasattr(self, "hotkeys"):
            now = time.monotonic()
            if now >= self._hotkey_config_poll_at:
                self._hotkey_config_poll_at = now + 0.5
                if self.hotkeys.reload_config():
                    self._update_hotkey_hint()
            self.hotkeys.poll()
        try:
            self.root.after(20, self._poll_hotkeys)
        except tk.TclError:
            pass

    def _force_topmost(self):
        """Keep the single overlay above Dofus without changing input behavior."""
        if self._closing:
            return
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass

    def _keep_topmost(self):
        if self._closing:
            return
        self._force_topmost()
        try:
            self.root.after(750, self._keep_topmost)
        except tk.TclError:
            pass

    def _on_configure(self, event=None):
        if self._closing:
            return
        try:
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            size = (width, height)

            # Configure-time work is deliberately cheap: resizing can generate
            # dozens of events per second. Only touch widgets when a breakpoint
            # or the actual width has changed.
            if width != getattr(self, "_last_layout_width", None):
                self._last_layout_width = width

                if hasattr(self, "quit_button"):
                    compact_quit = width < 120
                    if compact_quit != getattr(self, "_compact_quit", None):
                        self._compact_quit = compact_quit
                        if compact_quit:
                            self.quit_button.config(text="×", padx=2, pady=0, width=1)
                        else:
                            self.quit_button.config(text="Quitter", padx=9, pady=2, width=0)

                if hasattr(self, "title_label"):
                    show_title = width >= 150
                    if show_title != getattr(self, "_show_title", None):
                        self._show_title = show_title
                        self.title_label.pack_forget()
                        if show_title:
                            self.title_label.pack(side="left", padx=12)

                if hasattr(self, "status_label"):
                    show_status = width >= 170
                    if show_status != getattr(self, "_show_status", None):
                        self._show_status = show_status
                        self.status_label.pack_forget()
                        if show_status:
                            self.status_label.pack(
                                side="right", padx=(0, 8), before=self.quit_button
                            )

                if hasattr(self, "footer_hint"):
                    show_hint = width >= 190
                    if show_hint != getattr(self, "_show_footer_hint", None):
                        self._show_footer_hint = show_hint
                        self.footer_hint.pack_forget()
                        if show_hint:
                            self.footer_hint.pack(side="left", padx=12)

                if hasattr(self, "debug_label"):
                    show_debug = width >= 300
                    if show_debug != getattr(self, "_show_debug", None):
                        self._show_debug = show_debug
                        self.debug_label.pack_forget()
                        if show_debug:
                            self.debug_label.pack(
                                side="left", fill="x", expand=True, padx=(8, 8)
                            )

                if hasattr(self, "current_map_label"):
                    self.current_map_label.config(wraplength=max(20, width - 30))
                for label in getattr(self, "direction_labels", {}).values():
                    label.config(wraplength=max(20, width - 55))

            if hasattr(self, "header") and hasattr(self, "footer"):
                compact = height < 72
                if compact != getattr(self, "_compact_vertical", None):
                    self._compact_vertical = compact
                    self.header.config(height=24 if compact else 34)
                    self.footer.config(height=22 if compact else 30)

            if size != getattr(self, "_last_saved_size", None):
                self._last_saved_size = size
                self._schedule_geometry_save()
        except tk.TclError:
            pass

    # ---------- UI ----------

    def _build_ui(self):
        """Build the compact overlay as one normal interactive Tk window."""
        self.frame = tk.Frame(self.root, bg="#11161c", highlightthickness=0)
        self.frame.pack(fill="both", expand=True)

        header = tk.Frame(self.frame, bg="#11161c", height=34, highlightthickness=0, cursor="fleur")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        self.header = header

        title = tk.Label(
            header, text="RESSOURCES À PROXIMITÉ", fg="#ffffff", bg="#11161c",
            font=("Segoe UI", 10, "bold"), anchor="w", cursor="fleur"
        )
        title.pack(side="left", padx=12)
        self.title_label = title

        self.quit_button = tk.Button(
            header, text="Quitter", command=self.close,
            fg="#ffffff", bg="#252c34", activeforeground="#ffffff",
            activebackground="#303944", relief="flat", bd=0,
            highlightthickness=0, font=("Segoe UI", 8, "bold"),
            padx=9, pady=2, cursor="hand2"
        )
        self.quit_button.pack(side="right", padx=7, pady=5)

        self.status_label = tk.Label(
            header, text="OCR", fg="#7f8a96", bg="#11161c",
            font=("Segoe UI", 8), anchor="e", cursor="fleur"
        )
        self.status_label.pack(side="right", padx=(0, 8))

        for widget in (header, title, self.status_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        # Fixed 3-row layout: header and footer never compete with the resource
        # area for space. The middle section is a plain Canvas used only as a
        # clipping viewport: there is deliberately no scrollbar and no wheel
        # scrolling. If the result list is taller than the available area,
        # Tk clips the excess content instead of pushing the footer away.
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        content = tk.Frame(self.frame, bg="#11161c", highlightthickness=0)
        content.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 3))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.content_frame = content

        content_canvas = tk.Canvas(
            content, bg="#11161c", highlightthickness=0, borderwidth=0,
        )
        content_body = tk.Frame(content_canvas, bg="#11161c")
        content_window_id = content_canvas.create_window(
            (0, 0), window=content_body, anchor="nw"
        )
        content_canvas.grid(row=0, column=0, sticky="nsew")
        self._results_canvas = content_canvas
        self._results_body = content_body

        # Keep the virtual content width synchronized with the viewport.
        # The canvas itself provides the required overflow-hidden behavior.
        content_canvas.bind(
            "<Configure>",
            lambda e: content_canvas.itemconfigure(
                content_window_id, width=e.width
            ),
        )

        self.coordinate_label = tk.Label(
            content_body, text="Carte : lecture OCR…", fg="#aeb7c4", bg="#11161c",
            font=("Segoe UI", 9), anchor="w"
        )
        self.coordinate_label.pack(fill="x", pady=(0, 3))

        self.current_map_label = tk.Label(
            content_body, text="Ici : lecture des ressources…", fg="#f0c75e", bg="#11161c",
            font=("Segoe UI", 9, "bold"), anchor="nw", justify="left", wraplength=290
        )
        self.current_map_label.pack(fill="x", pady=(0, 4))

        self.results_frame = tk.Frame(content_body, bg="#11161c", highlightthickness=0)
        self.results_frame.pack(fill="x", expand=False, pady=(1, 0))

        self.direction_labels = {}
        for direction in ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖"):
            row = tk.Frame(self.results_frame, bg="#11161c")
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=direction, width=2, anchor="w", fg="#f0c75e", bg="#11161c",
                font=("Segoe UI Symbol", 10, "bold")
            ).pack(side="left", padx=(0, 5))
            label = tk.Label(
                row, text="", anchor="nw", justify="left", fg="#e9edf2",
                wraplength=290, bg="#11161c", font=("Segoe UI", 8),
            )
            label.pack(side="left", fill="x", expand=True)
            self.direction_labels[direction] = label

        self.direction_labels["↑"].config(text="Lecture OCR…")

        footer = tk.Frame(self.frame, bg="#11161c", height=30, highlightthickness=0, cursor="fleur")
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        footer.configure(height=30)
        self.footer = footer

        self.resize_grip = tk.Label(
            footer, text="◢", fg="#65717e", bg="#11161c",
            font=("Segoe UI Symbol", 9), cursor="size_nw_se", width=2
        )
        self.resize_grip.pack(side="right", padx=(0, 5), pady=5)
        self.resize_grip.bind("<ButtonPress-1>", self._start_resize)
        self.resize_grip.bind("<B1-Motion>", self._resize_overlay)
        self.resize_grip.bind("<ButtonRelease-1>", self._end_resize)

        footer_hint = tk.Label(
            footer, text="",
            fg="#697582", bg="#11161c", font=("Segoe UI", 7), cursor="fleur"
        )
        self.footer_hint = footer_hint
        footer_hint.pack(side="left", padx=12)
        self._update_hotkey_hint()
        self.debug_label = tk.Label(
            footer, text="Overlay visible — initialisation OCR…",
            fg="#aeb7c4", bg="#11161c", font=("Consolas", 7), anchor="w", cursor="fleur"
        )
        self.debug_label.pack(side="left", fill="x", expand=True, padx=(8, 8))

        for widget in (footer, footer_hint, self.debug_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

    def _update_hotkey_hint(self):
        if not hasattr(self, "footer_hint"):
            return
        try:
            self.footer_hint.config(
                text=f"{self.hotkey_config.settings_key} : réglages  •  "
                     f"{self.hotkey_config.toggle_key} : afficher"
            )
        except tk.TclError:
            pass

    # ---------- overlay dragging ----------

    def _start_drag(self, event):
        self._drag_active = True
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        return "break"

    def _drag(self, event):
        if not hasattr(self, "_drag_origin"):
            return "break"
        ox, oy, wx, wy = self._drag_origin
        nx = wx + event.x_root - ox
        ny = wy + event.y_root - oy
        self.root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height()}+{nx}+{ny}")
        return "break"

    def _end_drag(self, event=None):
        if hasattr(self, "_drag_origin"):
            del self._drag_origin
        self._drag_active = False
        self._save_overlay_geometry()
        return "break"

    # ---------- manual resize ----------

    def _start_resize(self, event):
        try:
            self._resize_active = True
            self._resize_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())
            self.resize_grip.grab_set_global()
        except tk.TclError:
            try:
                self.resize_grip.grab_set()
            except tk.TclError:
                pass
        return "break"

    def _resize_overlay(self, event):
        if not getattr(self, "_resize_active", False) or not hasattr(self, "_resize_origin"):
            return "break"
        try:
            ox, oy, ow, oh = self._resize_origin
            new_w = max(1, ow + event.x_root - ox)
            new_h = max(1, oh + event.y_root - oy)
            x, y = self.root.winfo_x(), self.root.winfo_y()

            # Do not force a synchronous update on every mouse event. Tk can
            # receive a very high number of motion events while dragging; the
            # next geometry update is coalesced to one per frame-ish.
            self._pending_resize_geometry = (new_w, new_h, x, y)
            if not getattr(self, "_resize_job", None):
                self._resize_job = self.root.after(16, self._apply_pending_resize)
        except tk.TclError:
            pass
        return "break"

    def _apply_pending_resize(self):
        self._resize_job = None
        geometry = getattr(self, "_pending_resize_geometry", None)
        self._pending_resize_geometry = None
        if not geometry or not getattr(self, "_resize_active", False) or self._closing:
            return
        try:
            w, h, x, y = geometry
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    def _end_resize(self, event=None):
        if hasattr(self, "_resize_origin"):
            del self._resize_origin
        self._resize_active = False
        if getattr(self, "_resize_job", None):
            try:
                self.root.after_cancel(self._resize_job)
            except tk.TclError:
                pass
            self._resize_job = None
        if getattr(self, "_pending_resize_geometry", None):
            geometry = self._pending_resize_geometry
            self._pending_resize_geometry = None
            try:
                w, h, x, y = geometry
                self.root.geometry(f"{w}x{h}+{x}+{y}")
            except tk.TclError:
                pass
        try:
            self.resize_grip.grab_release()
        except tk.TclError:
            pass
        self._save_overlay_geometry()
        return "break"

    # ---------- settings ----------

    def _read_int_setting(self, key, default):
        try:
            return int(self.db.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default


    def _read_selected_resources(self):
        saved = self.db.get_setting("selected_resources", None)
        # Missing setting = first install / legacy data: all resources selected.
        # An explicitly saved empty value means the user selected none.
        if saved is None:
            return None
        if not str(saved).strip():
            return []
        ids = []
        for value in str(saved).split(","):
            value = value.strip()
            if value:
                ids.append(value)
        return ids


    def _read_resource_distances(self):
        saved = self.db.get_setting("resource_distances", "")
        result = {}
        if saved:
            for item in str(saved).split(","):
                if ":" not in item:
                    continue
                rid, value = item.split(":", 1)
                try:
                    result[str(rid).strip()] = max(0, min(5, int(value)))
                except ValueError:
                    pass
        return result

    def _ensure_default_resource_distances(self):
        """Use distance 1 for a first install and for newly added resources."""
        if not (self._live and self._live_catalog_ready):
            return
        try:
            resources = self._live.catalog_for_ui()
            for row in resources:
                rid = str(row["id"])
                if rid not in self.resource_distances:
                    self.resource_distances[rid] = 1
            # Keep the default in memory. It will be persisted together with
            # the other settings when the user presses "Appliquer". This avoids
            # doing a SQLite write during every first startup.
        except Exception:
            # A missing/corrupt local database must not prevent the overlay from starting.
            pass

    def get_all_resources(self):
        if self._live and self._live_catalog_ready:
            return self._live.catalog_for_ui()
        return []

    def show_settings(self, event=None):
        """Open the settings window.

        The settings page is scrollable so the action buttons remain reachable
        on short screens.  Keep all widget references local until the page is
        fully built: an earlier version assigned the resource canvas before it
        existed, causing show_settings() to abort halfway through construction.
        """
        if self.settings_window is not None:
            try:
                if self.settings_window.winfo_exists():
                    # The settings shortcut is a true toggle: pressing it again
                    # closes the window instead of reopening/re-focusing it.
                    self._close_settings()
                    return
            except tk.TclError:
                pass
            self.settings_window = None

        win = tk.Toplevel(self.root)
        self.settings_window = win
        # Build the complete window while hidden. This prevents the brief
        # uncentered/partially-painted flash visible on some multi-monitor
        # Windows + Tk configurations.
        try:
            win.withdraw()
        except tk.TclError:
            pass
        win.title("Dofus Resource Assistant — Réglages")
        win.configure(bg="#171b21")
        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Settings.TLabelframe", background="#222831", borderwidth=1, relief="solid")
        style.configure("Settings.TLabelframe.Label", background="#222831", foreground="#f2c75c", font=("Segoe UI", 10, "bold"))
        style.configure("Settings.TButton", font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.configure("Settings.TCheckbutton", background="#222831", foreground="#e7ebf0", font=("Segoe UI", 9))
        style.configure("Settings.TCombobox", padding=3)
        # Tk classic widgets otherwise fall back to the Windows light theme,
        # which made parts of the settings page look washed out or unreadable.
        win.option_add("*Label.background", "#171b21")
        win.option_add("*Label.foreground", "#dce2e9")
        win.option_add("*Checkbutton.background", "#222831")
        win.option_add("*Checkbutton.foreground", "#e7ebf0")
        win.option_add("*Entry.background", "#11161c")
        win.option_add("*Entry.foreground", "#f2f4f7")
        win.option_add("*Entry.insertBackground", "#ffffff")
        win.option_add("*Entry.selectBackground", "#3f5a45")
        win.option_add("*Entry.selectForeground", "#ffffff")

        screen_h = win.winfo_screenheight()
        screen_w = win.winfo_screenwidth()
        settings_h = min(780, max(560, screen_h - 40))
        settings_w = min(920, max(760, screen_w - 40))
        win.geometry(f"{settings_w}x{settings_h}+0+0")
        win.minsize(760, 560)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        outer = tk.Frame(win, bg="#171b21")
        outer.pack(fill="both", expand=True)

        # The action bar is deliberately outside the scrollable page. This keeps
        # the OCR test/reset/apply buttons permanently reachable even on small
        # screens, while the long resource list remains scrollable.
        action_bar = tk.Frame(outer, bg="#20262f", relief="flat", bd=0)

        page_canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, bg="#171b21")
        page_scrollbar = ttk.Scrollbar(outer, orient="vertical", command=page_canvas.yview)
        page = tk.Frame(page_canvas, padx=14, pady=12, bg="#171b21")
        page_id = page_canvas.create_window((0, 0), window=page, anchor="nw")

        page.bind(
            "<Configure>",
            lambda e: page_canvas.configure(scrollregion=page_canvas.bbox("all")),
        )
        page_canvas.bind(
            "<Configure>",
            lambda e: page_canvas.itemconfigure(page_id, width=e.width),
        )
        page_canvas.configure(yscrollcommand=page_scrollbar.set)
        page_canvas.pack(side="left", fill="both", expand=True)
        page_scrollbar.pack(side="right", fill="y")

        self._settings_wheel_canvas = page_canvas

        # Resource list is created BEFORE the mouse-wheel dispatcher.  The V48
        # code referenced `canvas` here before assigning it, which raised a
        # NameError and left a half-created settings window behind.
        tk.Label(page, text="Réglages", bg="#171b21", fg="#f2c75c", font=("Segoe UI", 18, "bold"), anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(page, text="Configure l’affichage, l’OCR et les ressources détectées.", bg="#171b21", fg="#9aa5b3", font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(0, 12))

        resource_frame = tk.LabelFrame(page, text="Ressources par métier", padx=10, pady=8, bg="#222831", fg="#f2f4f7")
        resource_frame.pack(fill="x", pady=(0, 8))
        resource_frame.configure(height=285)
        resource_frame.pack_propagate(False)

        canvas = tk.Canvas(resource_frame, highlightthickness=0, bg="#222831")
        scrollbar = ttk.Scrollbar(resource_frame, orient="vertical", command=canvas.yview)
        scroll = tk.Frame(canvas, bg="#222831")
        window_id = canvas.create_window((0, 0), window=scroll, anchor="nw")
        scroll.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._settings_resource_canvas = canvas

        def resource_wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        canvas.bind("<MouseWheel>", resource_wheel, add="+")

        def wheel(event):
            target = page_canvas
            try:
                widget = win.winfo_containing(event.x_root, event.y_root)
                while widget is not None:
                    if widget is canvas:
                        target = canvas
                        break
                    if widget is win:
                        break
                    widget = getattr(widget, "master", None)
            except tk.TclError:
                pass
            target.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        # Bind only while this settings window exists, and retain the binding
        # id so closing it cannot disturb unrelated Tk mouse-wheel bindings.
        self._settings_wheel_bind_id = win.bind("<MouseWheel>", wheel, add="+")

        intro = tk.LabelFrame(page, text="Ressources à afficher", padx=10, pady=8, bg="#222831", fg="#f2f4f7")
        # Put the intro before the resource list visually.
        intro.pack_forget()
        intro.pack(fill="x", pady=(0, 8), before=resource_frame)
        tk.Label(
            intro,
            text="Coche une ressource puis choisis sa distance maximale.\n0 = carte actuelle uniquement • 1 = cartes voisines • jusqu'à 5 cartes.",
            justify="left", anchor="w",
        ).pack(fill="x")

        self.resource_vars = {}
        self.resource_distance_vars = {}
        self.resource_category_vars = {}
        self.resource_category_resource_ids = {}
        resources = sorted(
            self.get_all_resources(),
            key=lambda r: (
                JOB_ORDER.get(r.get("job") or r.get("category") or "Autre", 99),
                int(r.get("level") or 0), r.get("name", "")
            ),
        )
        categories = {}
        for row in resources:
            category = row.get("job") or row.get("category") or "Autre"
            categories.setdefault(category, []).append(row)

        for category, category_resources in sorted(
            categories.items(), key=lambda item: JOB_ORDER.get(item[0], 99)
        ):
            expanded = tk.BooleanVar(value=True)
            selected = tk.BooleanVar(value=all(
                self.selected_resources is None or str(r["id"]) in self.selected_resources
                for r in category_resources
            ))
            self.resource_category_vars[category] = selected
            self.resource_category_resource_ids[category] = [str(r["id"]) for r in category_resources]
            header = tk.Frame(scroll, bg="#222831")
            header.pack(fill="x", pady=(8, 2), padx=(0, 14))
            body = tk.Frame(scroll, bg="#222831")
            body.pack(fill="x")

            category_button = tk.Button(
                header, text="▼", width=2,
                relief="flat", bd=0, bg="#222831", fg="#f0c75e",
                activebackground="#394452", activeforeground="#ffffff",
                cursor="hand2"
            )
            # Use an explicit callback with all widget references captured at
            # creation time. This avoids relying on a nested callback whose
            # closure can become ambiguous while the category loop continues.
            category_button.configure(
                command=lambda cat=category, b=body, h=header, v=expanded, btn=category_button:
                    self._toggle_category_visibility(cat, b, h, v, btn)
            )
            category_button.pack(side="left")
            tk.Checkbutton(
                header, text=category, variable=selected,
                command=lambda cat=category, var=selected: self.select_category(cat, var.get()),
                bg="#222831", fg="#f0c75e", activebackground="#222831",
                activeforeground="#ffffff", selectcolor="#394452",
                font=("Segoe UI", 9, "bold")
            ).pack(side="left", padx=(2, 4))
            for row in category_resources:
                rid = str(row["id"])
                name = row["name"]
                level = int(row.get("level") or 0)
                line = tk.Frame(body, bg="#222831")
                line.pack(fill="x", pady=2, padx=(28, 14))
                line.columnconfigure(0, weight=1)
                line.columnconfigure(1, weight=0, minsize=68)
                line.columnconfigure(2, weight=0, minsize=52)
                var = tk.BooleanVar(
                    value=(self.selected_resources is None or rid in self.selected_resources)
                )
                self.resource_vars[rid] = var
                tk.Checkbutton(
                    line, text=f"Niv. {level} — {name}", variable=var,
                    command=lambda cat=category: self._sync_category_selection(cat),
                    anchor="w", bg="#222831",
                    activebackground="#394452", activeforeground="#ffffff", fg="#e7ebf0",
                    selectcolor="#394452", indicatoron=True, padx=0, pady=0
                ).grid(row=0, column=0, sticky="ew", padx=(0, 14))

                distance = self.resource_distances.get(rid, 1)
                dvar = tk.StringVar(value=str(distance))
                self.resource_distance_vars[rid] = dvar
                tk.Label(line, text="Distance", bg="#222831", fg="#9fa9b7",
                         font=("Segoe UI", 8), width=8, anchor="e").grid(row=0, column=1, padx=(0, 8))
                ttk.Combobox(
                    line, textvariable=dvar, values=["0", "1", "2", "3", "4", "5"],
                    state="readonly", width=3
                ).grid(row=0, column=2, padx=(0, 4), sticky="e")

        display_box = tk.LabelFrame(page, text="Affichage", padx=10, pady=8, bg="#222831", fg="#f2f4f7")
        display_box.pack(fill="x", pady=(0, 8))
        self.show_resource_coordinates_var = tk.BooleanVar(
            value=str(self.db.get_setting("show_resource_coordinates", self.db.get_setting("show_coordinates", "1"))).lower() in ("1", "true", "yes", "on")
        )
        tk.Checkbutton(
            display_box, text="Afficher les coordonnées de la ressource sur l’overlay",
            variable=self.show_resource_coordinates_var, bg="#222831", fg="#e7ebf0",
            activebackground="#222831", activeforeground="#ffffff",
            selectcolor="#394452", anchor="w"
        ).pack(fill="x")

        data_box = tk.LabelFrame(page, text="Données locales", padx=10, pady=9, bg="#222831", fg="#f2f4f7")
        data_box.pack(fill="x", pady=(0, 8))
        self.data_status_var = tk.StringVar(value=self._live.status_text() if self._live else "")
        tk.Label(data_box, textvariable=self.data_status_var, anchor="w", justify="left", fg="#dce2e9", bg="#222831", font=("Segoe UI", 9, "bold"), wraplength=820).pack(fill="x")
        tk.Label(data_box, text="Lecture locale — aucune synchronisation pendant le jeu", anchor="w", justify="left", fg="#8f9aa8", bg="#222831", font=("Segoe UI", 8)).pack(fill="x", pady=(4, 0))

        position = tk.LabelFrame(page, text="Position de l'overlay", padx=10, pady=7, bg="#222831", fg="#f2f4f7")
        position.pack(fill="x", pady=(0, 7))
        tk.Label(position, text="X", bg="#222831", fg="#dce2e9").grid(row=0, column=0)
        self.overlay_x_var = tk.StringVar(value=str(self.root.winfo_x()))
        tk.Entry(position, textvariable=self.overlay_x_var, width=8).grid(row=0, column=1, padx=5)
        tk.Label(position, text="Y", bg="#222831", fg="#dce2e9").grid(row=0, column=2, padx=(12, 0))
        self.overlay_y_var = tk.StringVar(value=str(self.root.winfo_y()))
        tk.Entry(position, textvariable=self.overlay_y_var, width=8).grid(row=0, column=3, padx=5)
        tk.Label(position, text="Largeur", bg="#222831", fg="#dce2e9").grid(row=1, column=0, sticky="w", pady=(7,0))
        self.overlay_width_var = tk.StringVar(value=str(self.root.winfo_width()))
        tk.Entry(position, textvariable=self.overlay_width_var, width=8).grid(row=1, column=1, padx=5, pady=(7,0))
        tk.Label(position, text="Hauteur", bg="#222831", fg="#dce2e9").grid(row=1, column=2, sticky="w", pady=(7,0))
        self.overlay_height_var = tk.StringVar(value=str(self.root.winfo_height()))
        tk.Entry(position, textvariable=self.overlay_height_var, width=8).grid(row=1, column=3, padx=5, pady=(7,0))
        tk.Label(position, text="Tu peux aussi redimensionner avec le coin inférieur droit.", fg="#aab3bf").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(7,0)
        )

        ocr = tk.LabelFrame(page, text="OCR des coordonnées", padx=14, pady=12, bg="#222831", fg="#f2f4f7")
        ocr.pack(fill="x", pady=(0, 9))
        for col in range(4):
            ocr.columnconfigure(col, weight=1 if col in (1, 3) else 0)
        self.tesseract_var = tk.StringVar(value=self.detector.tesseract_cmd if self.detector else "")
        tk.Label(ocr, text="Tesseract", bg="#222831", fg="#dce2e9", anchor="w").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 10)
        )
        tk.Entry(ocr, textvariable=self.tesseract_var).grid(
            row=0, column=1, columnspan=3, padx=(0, 2), sticky="ew", pady=(0, 10)
        )
        region = self.detector.region if self.detector else {}
        self.ocr_left_var = tk.StringVar(value=str(region.get("left", 3)))
        self.ocr_top_var = tk.StringVar(value=str(region.get("top", 70)))
        self.ocr_width_var = tk.StringVar(value=str(region.get("width", 91)))
        self.ocr_height_var = tk.StringVar(value=str(region.get("height", 32)))
        field_kw = dict(bg="#222831", fg="#dce2e9", anchor="w")
        entry_kw = dict(width=8, justify="center")
        tk.Label(ocr, text="Gauche", **field_kw).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(2, 6))
        tk.Entry(ocr, textvariable=self.ocr_left_var, **entry_kw).grid(row=1, column=1, sticky="w", padx=(0, 18), pady=(2, 6))
        tk.Label(ocr, text="Haut", **field_kw).grid(row=1, column=2, sticky="w", padx=(8, 10), pady=(2, 6))
        tk.Entry(ocr, textvariable=self.ocr_top_var, **entry_kw).grid(row=1, column=3, sticky="w", pady=(2, 6))
        tk.Label(ocr, text="Largeur", **field_kw).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(2, 6))
        tk.Entry(ocr, textvariable=self.ocr_width_var, **entry_kw).grid(row=2, column=1, sticky="w", padx=(0, 18), pady=(2, 6))
        tk.Label(ocr, text="Hauteur", **field_kw).grid(row=2, column=2, sticky="w", padx=(8, 10), pady=(2, 6))
        tk.Entry(ocr, textvariable=self.ocr_height_var, **entry_kw).grid(row=2, column=3, sticky="w", pady=(2, 6))
        self.ocr_status_var = tk.StringVar(value=(f"Tesseract : {self.detector.tesseract_cmd}" if self.detector else "Tesseract non disponible"))
        tk.Label(ocr, textvariable=self.ocr_status_var, fg="#aab3bf", anchor="w", justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=(5, 0))
        self._ocr_diagnostic_var = tk.StringVar(value="Diagnostic temps réel : en attente…")
        tk.Button(ocr, text="Sélectionner la zone à l’écran…", command=self.select_ocr_zone, relief="flat", bg="#303844", fg="#f2f4f7", activebackground="#3d4856", activeforeground="#ffffff", padx=12, pady=6, cursor="hand2").grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(ocr, text="Glisse un rectangle autour des coordonnées (en haut à gauche) dans Dofus.", fg="#9aa5b3", bg="#222831", font=("Segoe UI", 8), anchor="w", justify="left", wraplength=580).grid(row=5, column=0, columnspan=4, sticky="w", pady=(5, 0))
        tk.Label(
            ocr, textvariable=self._ocr_diagnostic_var, bg="#171b21", fg="#c7d0da",
            anchor="w", justify="left", font=("Consolas", 8), wraplength=590,
            padx=8, pady=7
        ).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(7, 0))

        transition = tk.LabelFrame(page, text="Zone de transition", padx=14, pady=12, bg="#222831", fg="#f2f4f7")
        transition.pack(fill="x", pady=(0, 9))
        tr = getattr(self.detector, "transition_region", {}) if self.detector else {}
        self.transition_left_var = tk.StringVar(value=str(tr.get("left", self.db.get_setting("transition_left", 0))))
        self.transition_top_var = tk.StringVar(value=str(tr.get("top", self.db.get_setting("transition_top", 0))))
        self.transition_width_var = tk.StringVar(value=str(tr.get("width", self.db.get_setting("transition_width", 240))))
        self.transition_height_var = tk.StringVar(value=str(tr.get("height", self.db.get_setting("transition_height", 135))))
        tk.Label(transition, text="Gauche", **field_kw).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(2, 6))
        tk.Entry(transition, textvariable=self.transition_left_var, **entry_kw).grid(row=0, column=1, sticky="w", padx=(0, 18), pady=(2, 6))
        tk.Label(transition, text="Haut", **field_kw).grid(row=0, column=2, sticky="w", padx=(8, 10), pady=(2, 6))
        tk.Entry(transition, textvariable=self.transition_top_var, **entry_kw).grid(row=0, column=3, sticky="w", pady=(2, 6))
        tk.Label(transition, text="Largeur", **field_kw).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(2, 6))
        tk.Entry(transition, textvariable=self.transition_width_var, **entry_kw).grid(row=1, column=1, sticky="w", padx=(0, 18), pady=(2, 6))
        tk.Label(transition, text="Hauteur", **field_kw).grid(row=1, column=2, sticky="w", padx=(8, 10), pady=(2, 6))
        tk.Entry(transition, textvariable=self.transition_height_var, **entry_kw).grid(row=1, column=3, sticky="w", pady=(2, 6))
        tk.Button(
            transition, text="Sélectionner la zone à l’écran…",
            command=self.select_transition_zone, relief="flat", bg="#303844", fg="#f2f4f7",
            activebackground="#3d4856", activeforeground="#ffffff",
            padx=12, pady=6, cursor="hand2"
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(
            transition,
            text="Choisir un rectangle au milieu de l'écran de jeu. Cette capture sert uniquement à détecter la transition de carte.",
            fg="#9aa5b3", bg="#222831", font=("Segoe UI", 8), anchor="w",
            justify="left", wraplength=580
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(5, 0))

        # Fixed action bar: two compact rows so no button is clipped on narrower screens.
        action_bar.pack(side="bottom", fill="x", padx=8, pady=6, before=page_canvas)
        action_row1 = tk.Frame(action_bar, bg="#20262f")
        action_row1.pack(fill="x", padx=4, pady=(4, 2))
        action_row2 = tk.Frame(action_bar, bg="#20262f")
        action_row2.pack(fill="x", padx=4, pady=(2, 4))
        btn_kw = dict(font=("Segoe UI", 9, "bold"), bg="#303844", fg="#f2f4f7", activebackground="#3d4856", activeforeground="#ffffff", relief="flat", bd=0, padx=9, pady=5, cursor="hand2")
        for parent, text, cmd in ((action_row1, "Tester l'OCR", self.test_ocr), (action_row1, "Aperçu zone OCR", self.preview_ocr), (action_row2, "Réinitialiser la détection", self.reset_ocr_detection)):
            tk.Button(parent, text=text, command=cmd, **btn_kw).pack(side="left", padx=3, fill="x", expand=True)
        tk.Button(action_row2, text="Appliquer", command=self.apply_settings, bg="#c49a3a", fg="#171b21", activebackground="#d7ae4c", activeforeground="#171b21", relief="flat", bd=0, font=("Segoe UI", 9, "bold"), padx=16, pady=5, cursor="hand2").pack(side="right", padx=3)

        # Small footer note kept inside the scrollable settings page.
        version_footer = tk.Frame(page, bg="#171b21")
        version_footer.pack(fill="x", pady=(8, 18))
        tk.Label(
            version_footer, text="version 1.0.7", bg="#171b21", fg="#7f8a96",
            font=("Segoe UI", 8), anchor="center"
        ).pack(fill="x")
        tk.Label(
            version_footer, text="vibe codée avec ChatGPT (version gratuite)", bg="#171b21", fg="#65717e",
            font=("Segoe UI", 8, "italic"), anchor="center"
        ).pack(fill="x", pady=(3, 0))

        win.update_idletasks()
        page_canvas.configure(scrollregion=page_canvas.bbox("all"))
        page_canvas.yview_moveto(0)

        # Only show the fully built window, then force its native Win32 position
        # after Windows has created/mapped the real HWND. Repeating this a few
        # times handles delayed DPI/monitor placement changes without any flash.
        try:
            win.deiconify()
        except tk.TclError:
            pass
        self._center_settings_window(win)
        try:
            win.lift()
            win.focus_force()
        except tk.TclError:
            pass
        for delay in (40, 160, 400):
            try:
                win.after(delay, lambda w=win: self._center_settings_window(w))
            except tk.TclError:
                pass


    def _center_settings_window(self, win):
        """Center settings on the monitor containing the overlay's real HWND.

        Tk's screen/geometry coordinates can be wrong on Windows when two
        monitors use different scaling factors. Find the actual native HWND by
        its title, ask Windows which monitor contains the overlay, and position
        the settings HWND directly with SetWindowPos.
        """
        try:
            if not win.winfo_exists():
                return
            win.update_idletasks()

            if os.name != "nt":
                width = max(1, win.winfo_width())
                height = max(1, win.winfo_height())
                x = max(0, (win.winfo_screenwidth() - width) // 2)
                y = max(0, (win.winfo_screenheight() - height) // 2)
                win.geometry(f"{width}x{height}+{x}+{y}")
                return

            user32 = ctypes.windll.user32
            MONITOR_DEFAULTTONEAREST = 2
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.wintypes.LONG),
                    ("top", ctypes.wintypes.LONG),
                    ("right", ctypes.wintypes.LONG),
                    ("bottom", ctypes.wintypes.LONG),
                ]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.wintypes.DWORD),
                    ("rcMonitor", RECT),
                    ("rcWork", RECT),
                    ("dwFlags", ctypes.wintypes.DWORD),
                ]

            # Get the overlay's actual native HWND. For Tk this is more reliable
            # than mixing winfo_x/winfo_screenwidth logical pixels.
            root_hwnd = int(self.root.winfo_id())
            monitor = user32.MonitorFromWindow(root_hwnd, MONITOR_DEFAULTTONEAREST)

            # If Tk's id is not accepted by MonitorFromWindow, fall back to the
            # overlay's current native rectangle.
            if not monitor:
                overlay_rect = RECT()
                if user32.GetWindowRect(root_hwnd, ctypes.byref(overlay_rect)):
                    monitor = user32.MonitorFromRect(ctypes.byref(overlay_rect), MONITOR_DEFAULTTONEAREST)

            if not monitor:
                return

            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return

            # Locate the settings HWND after it has been mapped. FindWindowW is
            # deliberately used instead of relying on winfo_id(), which can
            # refer to a Tk wrapper differently across Tk/Windows builds.
            title = win.title()
            hwnd = int(user32.FindWindowW(None, title))
            if not hwnd:
                # The window may not be mapped yet; retry on the next scheduled
                # pass rather than placing it at Tk's default left-hand position.
                return

            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                width = max(1, int(rect.right - rect.left))
                height = max(1, int(rect.bottom - rect.top))
            else:
                width = max(1, int(win.winfo_width()))
                height = max(1, int(win.winfo_height()))

            left = int(info.rcWork.left)
            top = int(info.rcWork.top)
            right = int(info.rcWork.right)
            bottom = int(info.rcWork.bottom)
            work_w = max(1, right - left)
            work_h = max(1, bottom - top)

            x = left + max(0, (work_w - width) // 2)
            y = top + max(0, (work_h - height) // 2)
            x = max(left, min(x, right - width))
            y = max(top, min(y, bottom - height))

            user32.SetWindowPos(
                hwnd, 0, int(x), int(y), 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            )

        except (tk.TclError, OSError, ValueError):
            pass

    def _close_settings(self):
        win = self.settings_window
        self.settings_window = None
        try:
            if win is not None and win.winfo_exists():
                bind_id = self._settings_wheel_bind_id
                if bind_id:
                    try:
                        win.unbind("<MouseWheel>", bind_id)
                    except tk.TclError:
                        pass
                win.destroy()
        except tk.TclError:
            pass
        self._settings_wheel_canvas = None
        self._settings_resource_canvas = None
        self._settings_wheel_bind_id = None

    def _toggle_category_visibility(self, category, body, header, expanded, button):
        """Expand/collapse one profession in the settings resource list."""
        try:
            new_state = not bool(expanded.get())
            expanded.set(new_state)
            if new_state:
                # Restore the body directly after its own header; never append
                # it to the end of the scroll frame.
                body.pack(fill="x", after=header)
                button.configure(text="▼")
            else:
                body.pack_forget()
                button.configure(text="▶")

            # Tkinter's canvas does not always recompute the scrollable extent
            # immediately after pack/pack_forget. Force geometry processing so
            # the collapsed/expanded state is visible and the scrollbar stays
            # correct.
            body.master.update_idletasks()
            canvas = self._settings_resource_canvas
            if canvas is not None and canvas.winfo_exists():
                canvas.configure(scrollregion=canvas.bbox("all"))
        except tk.TclError:
            pass

    def select_category(self, category, value):
        """Select or deselect every resource belonging to one profession."""
        for rid in self.resource_category_resource_ids.get(category, []):
            var = self.resource_vars.get(rid)
            if var is not None:
                var.set(bool(value))
        category_var = self.resource_category_vars.get(category)
        if category_var is not None:
            category_var.set(bool(value))

    def _sync_category_selection(self, category):
        """Keep a profession checkbox aligned with its individual resources."""
        ids = self.resource_category_resource_ids.get(category, [])
        if not ids:
            return
        selected = all(self.resource_vars[rid].get() for rid in ids if rid in self.resource_vars)
        category_var = self.resource_category_vars.get(category)
        if category_var is not None:
            category_var.set(selected)

    def reset_ocr_detection(self):
        """Hard-reset the OCR and immediately wait for a fresh coordinate."""
        if self.detector is None:
            return

        self._ocr_reset_generation += 1
        self._last_ocr_error = None

        # Replace the detector object before resetting it. A previous OCR worker
        # may still be finishing in the background; keeping a separate detector
        # guarantees that its late result cannot mutate the freshly reset state.
        old_detector = self.detector
        try:
            region = dict(old_detector.region)
            command = old_detector.tesseract_cmd
            validator = self._live.has_map if self._live and self._live.ready() else None
            self.detector = CoordinateDetector(
                region=region,
                tesseract_cmd=command,
                coordinate_validator=validator,
                transition_region=getattr(old_detector, "transition_region", region),
            )
            self.detector.reset_detection()
            self._detector_history.append(old_detector)
        except Exception as exc:
            # If recreating Tesseract fails, fall back to resetting the existing
            # detector rather than leaving the button without any OCR state.
            self.detector = old_detector
            self.detector.reset_detection()
            self.debug_label.config(text=f"Réinitialisation OCR : {exc}")

        # A manual reset really means "forget the current map". Do not retain
        # the previous coordinate or its resource results while the new OCR
        # reading is being established.
        self.current_coordinates = None
        self.current_results = []
        self.current_map_results = []
        self._ocr_next_start = 0.0

        self.coordinate_label.config(text="Carte : lecture OCR…")
        self.status_label.config(text="Initialisation OCR…", fg="#e5b35c")
        self.debug_label.config(text="OCR réinitialisé — attente d'une nouvelle lecture")
        for direction in self.direction_labels:
            self.direction_labels[direction].config(text="Lecture OCR…")
        if hasattr(self, "ocr_status_var"):
            self.ocr_status_var.set(f"Tesseract : {self.detector.tesseract_cmd} — détection réinitialisée")

    def _select_zone(self, kind="ocr"):
        """Let the user draw the OCR rectangle directly on the screen.

        The selection uses a restrained double outline, small corner handles and
        a compact size badge so it stays visible without obscuring Dofus.
        """
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[0]
                shot = sct.grab(mon)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
            selector = tk.Toplevel(self.settings_window or self.root)
            selector.overrideredirect(True)
            selector.attributes("-topmost", True)
            selector.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
            canvas = tk.Canvas(selector, bg="black", highlightthickness=0, cursor="crosshair")
            canvas.pack(fill="both", expand=True)
            photo = ImageTk.PhotoImage(image)
            selector._photo = photo
            canvas.create_image(0, 0, image=photo, anchor="nw")
            # A high-contrast instruction strip instead of dimming the full
            # screen: Dofus remains readable while the selection stays obvious.
            banner = canvas.create_rectangle(18, 16, 690, 62, fill="#20252d", outline="#63d39b", width=2)
            banner_title = canvas.create_text(34, 27, text=("Sélection de la zone OCR" if kind == "ocr" else "Sélection de la zone de transition"), anchor="nw", fill="#ffffff", font=("Segoe UI", 13, "bold"))
            banner_sub = canvas.create_text(34, 47, text=("Cliquez-glissez autour des coordonnées • Échap = annuler" if kind == "ocr" else "Cliquez-glissez la zone de transition • Échap = annuler"), anchor="nw", fill="#c7d5cd", font=("Segoe UI", 9))
            canvas.tag_raise(banner)
            canvas.tag_raise(banner_title)
            canvas.tag_raise(banner_sub)
            box = {"start": None, "outer": None, "inner": None, "badge": None, "badge_text": None, "handles": []}

            def clear_box():
                for key in ("outer", "inner", "badge", "badge_text"):
                    if box.get(key):
                        canvas.delete(box[key])
                        box[key] = None
                for item in box.get("handles", []):
                    canvas.delete(item)
                box["handles"] = []

            def draw_selection(x0, y0, x1, y1):
                left, right = sorted((x0, x1))
                top, bottom = sorted((y0, y1))
                clear_box()
                # High-contrast neutral/green outline; no blue.
                box["outer"] = canvas.create_rectangle(left, top, right, bottom, outline="#d8e5dd", width=2)
                box["inner"] = canvas.create_rectangle(left, top, right, bottom, outline="#63d39b", width=1, dash=(6, 4))
                hs = 6
                for hx, hy in ((left, top), (right, top), (left, bottom), (right, bottom)):
                    box["handles"].append(canvas.create_rectangle(hx-hs, hy-hs, hx+hs, hy+hs, fill="#e7eee9", outline="#63d39b", width=1))
                w = abs(x1 - x0)
                h = abs(y1 - y0)
                text = f"{w} × {h} px"
                # Keep the dimensions outside the selected rectangle.
                bx = (left + right) / 2
                badge_w = max(72, min(150, 18 + len(text) * 7))
                if top >= 48:
                    by = top - 18
                else:
                    by = min(canvas.winfo_height() - 18, bottom + 18)
                bx = max(badge_w + 6, min(canvas.winfo_width() - badge_w - 6, bx))
                box["badge"] = canvas.create_rectangle(
                    bx-badge_w, by-11, bx+badge_w, by+11,
                    fill="#11161c", outline="#63d39b", width=1
                )
                box["badge_text"] = canvas.create_text(
                    bx, by, text=text, fill="#dce8df",
                    font=("Segoe UI", 8, "bold")
                )
                for item in box["handles"] + [box["outer"], box["inner"], box["badge"], box["badge_text"]]:
                    canvas.tag_raise(item)

            def press(e):
                box["start"] = (e.x, e.y)
                clear_box()
                draw_selection(e.x, e.y, e.x, e.y)

            def move(e):
                if box["start"]:
                    draw_selection(box["start"][0], box["start"][1], e.x, e.y)

            def release(e):
                if not box["start"]:
                    return
                x0, y0 = box["start"]
                x1, y1 = e.x, e.y
                left = int(min(x0, x1) + mon["left"])
                top = int(min(y0, y1) + mon["top"])
                width = abs(x1 - x0)
                height = abs(y1 - y0)
                if width >= 20 and height >= 20:
                    vars_map = {
                        "ocr": (self.ocr_left_var, self.ocr_top_var, self.ocr_width_var, self.ocr_height_var),
                        "transition": (self.transition_left_var, self.transition_top_var, self.transition_width_var, self.transition_height_var),
                    }
                    target_vars = vars_map[kind]
                    target_vars[0].set(str(left))
                    target_vars[1].set(str(top))
                    target_vars[2].set(str(width))
                    target_vars[3].set(str(height))
                    selector.destroy()
                else:
                    box["start"] = None
                    clear_box()
                    warning_id = canvas.create_text(
                        selector.winfo_width() // 2, selector.winfo_height() // 2,
                        text="Zone trop petite — minimum 20 × 20 px",
                        fill="#ffffff", font=("Segoe UI", 12, "bold")
                    )
                    selector.after(1200, lambda: canvas.delete(warning_id))

            def cancel(event=None):
                try:
                    selector.destroy()
                except tk.TclError:
                    pass

            canvas.bind("<ButtonPress-1>", press)
            canvas.bind("<B1-Motion>", move)
            canvas.bind("<ButtonRelease-1>", release)
            selector.bind("<Escape>", cancel)
            selector.focus_force()
        except Exception as exc:
            messagebox.showerror("Sélection OCR", str(exc), parent=self.settings_window)


    def select_ocr_zone(self):
        self._select_zone("ocr")

    def select_transition_zone(self):
        self._select_zone("transition")

    def preview_ocr(self):
        """Show the exact OCR crop and preprocessing used by the detector."""
        try:
            region = self._get_region_from_vars()
            self._replace_detector(region, self.tesseract_var.get().strip() or None, self._get_transition_region_from_vars())
            if hasattr(self.detector, "show_preview"):
                self.detector.show_preview(parent=self.settings_window)
            else:
                # Keep this button useful even if an older detector is loaded.
                coordinates, text = self.detector.read_coordinates()
                messagebox.showinfo(
                    "Aperçu zone OCR",
                    f"Zone : gauche={region['left']} haut={region['top']} "
                    f"largeur={region['width']} hauteur={region['height']}\n\n"
                    f"OCR : {coordinates or 'aucune'}\n{text or '(vide)'}",
                    parent=self.settings_window,
                )
        except Exception as exc:
            messagebox.showerror("Aperçu OCR", str(exc), parent=self.settings_window)

    def test_ocr(self):
        try:
            region = self._get_region_from_vars()
            self._replace_detector(region, self.tesseract_var.get().strip() or None)
            coordinates, text = self.detector.read_coordinates()
            preview = text.replace("\n", " | ").strip()
            if len(preview) > 500:
                preview = preview[:500] + "…"
            messagebox.showinfo(
                "Test OCR",
                f"Coordonnées détectées : {coordinates or 'aucune'}\n\n"
                f"Texte OCR :\n{preview or '(vide)'}",
                parent=self.settings_window,
            )
            self.ocr_status_var.set(
                f"Tesseract : {self.detector.tesseract_cmd}"
            )
        except Exception as exc:
            messagebox.showerror("Test OCR", str(exc), parent=self.settings_window)

    def _get_region_from_vars(self):
        values = (
            self.ocr_left_var.get(),
            self.ocr_top_var.get(),
            self.ocr_width_var.get(),
            self.ocr_height_var.get(),
        )
        left, top, width, height = map(int, values)
        if width < 20 or height < 20:
            raise ValueError("La zone OCR est trop petite.")
        return {
            "left": max(0, left),
            "top": max(0, top),
            "width": width,
            "height": height,
        }

    def _get_transition_region_from_vars(self):
        values = (
            self.transition_left_var.get(),
            self.transition_top_var.get(),
            self.transition_width_var.get(),
            self.transition_height_var.get(),
        )
        left, top, width, height = map(int, values)
        if width < 20 or height < 20:
            raise ValueError("La zone de transition est trop petite.")
        return {
            "left": max(0, left),
            "top": max(0, top),
            "width": width,
            "height": height,
        }

    def _replace_detector(self, region, command=None, transition_region=None):
        # Preserve the confirmed map when settings are tested/applied. Recreating
        # the detector used to reset last_coordinates, which meant a single bad
        # OCR frame immediately after "Test OCR" could become the new origin and
        # make the resource search jump to the wrong map.
        old = self.detector
        validator = self._live.has_map if self._live and self._live.ready() else None
        if transition_region is None:
            transition_region = getattr(old, "transition_region", region) if old is not None else region
        self.detector = CoordinateDetector(
            region=region,
            tesseract_cmd=command,
            coordinate_validator=validator,
            transition_region=transition_region,
        )
        if old is not None:
            self.detector.last_coordinates = old.last_coordinates
            self.detector.initializing = old.last_coordinates is None
            self.detector.candidate_coordinates = None
            self.detector.candidate_count = 0
            self.detector.last_ocr_text = old.last_ocr_text
            self._detector_history.append(old)

    def apply_settings(self):
        try:
            overlay_x = int(self.overlay_x_var.get())
            overlay_y = int(self.overlay_y_var.get())
            overlay_width = max(1, int(self.overlay_width_var.get()))
            overlay_height = max(1, int(self.overlay_height_var.get()))
            region = self._get_region_from_vars()
            transition_region = self._get_transition_region_from_vars()
        except ValueError:
            messagebox.showerror(
                "Erreur",
                "Position et zones OCR/transition doivent être des nombres valides.",
                parent=self.settings_window,
            )
            return

        selected = [str(rid) for rid, var in self.resource_vars.items() if var.get()]
        resource_distances = {}
        try:
            for rid, var in self.resource_distance_vars.items():
                resource_distances[str(rid)] = max(0, min(5, int(var.get())))
        except (ValueError, TypeError):
            messagebox.showerror("Erreur", "Les distances doivent être comprises entre 0 et 5.", parent=self.settings_window)
            return

        try:
            self._replace_detector(
                region,
                self.tesseract_var.get().strip() or None,
                transition_region=transition_region,
            )
        except Exception as exc:
            messagebox.showerror(
                "Tesseract",
                str(exc),
                parent=self.settings_window,
            )
            return

        self.selected_resources = selected
        self.resource_distances = resource_distances
        self.show_resource_coordinates = bool(self.show_resource_coordinates_var.get())

        self.db.set_settings({
            "selected_resources": ",".join(selected),
            "resource_distances": ",".join(f"{rid}:{dist}" for rid, dist in resource_distances.items()),
            "overlay_x": overlay_x,
            "overlay_y": overlay_y,
            "overlay_width": overlay_width,
            "overlay_height": overlay_height,
            **{f"ocr_{key}": value for key, value in region.items()},
            **{f"transition_{key}": value for key, value in transition_region.items()},
            "show_resource_coordinates": int(self.show_resource_coordinates_var.get()),
            "tesseract_cmd": self.detector.tesseract_cmd,
        })

        self.root.geometry(
            f"{overlay_width}x{overlay_height}+{overlay_x}+{overlay_y}"
        )
        self._close_settings()

        if self.current_coordinates is not None:
            self.current_map_results = self.search_current_map_resources(self.current_coordinates)
            self.current_results = self.search_resources(self.current_coordinates)
            self.display_results(self.current_coordinates, self.current_results)

    # ---------- resource search/display ----------

    def search_current_map_resources(self, coordinates):
        """Resources on the current map, respecting the per-resource selection."""
        if coordinates is None or not (self._live and self._live_catalog_ready):
            return []
        x, y = coordinates
        return self._live.resources_for_map(
            x, y,
            selected_ids=self.selected_resources,
        )

    def search_resources(self, coordinates):
        """Search neighbouring maps using each resource's own configured distance.

        Distance 0 means current map only; distance 1 means the four adjacent
        maps. Greater distances use real cardinal movement cost (Manhattan
        distance), so diagonally offset maps require multiple map changes.
        No global min/max level or legacy priority settings are used.
        """
        if coordinates is None or not (self._live and self._live_catalog_ready):
            return []
        x, y = coordinates
        return self._live.resources_by_resource_distance(
            x, y,
            self.resource_distances,
            selected_ids=self.selected_resources,
        )

    def _coordinate_text(self, coordinates, stale=False):
        if coordinates is None:
            return "Carte : lecture OCR…"
        suffix = " !" if stale else ""
        return f"Carte : {coordinates[0]}, {coordinates[1]}{suffix}"

    def display_results(self, coordinates, results):
        for label in self.direction_labels.values():
            label.config(text="")

        if coordinates is None:
            self.coordinate_label.config(text="Carte : lecture OCR…")
            self.current_map_label.config(text="Ici : lecture des ressources…")
            self.status_label.config(text="OCR…", fg="#e5b35c")
            self.debug_label.config(text="Tesseract : lecture en cours…")
            self.direction_labels["↑"].config(text="Détection des coordonnées…")
            return

        x, y = coordinates
        self.coordinate_label.config(text=self._coordinate_text(coordinates), fg="#f2f4f7")
        self.status_label.config(text="OCR OK", fg="#8bcf7b")
        self.debug_label.config(text="OCR stable — carte confirmée")

        def fmt_items(items):
            if not items:
                return ""
            job_order = {
                "Alchimiste": 1, "Bûcheron": 2, "Mineur": 3,
                "Paysan": 4, "Pêcheur": 5, "Divers": 6,
            }
            ordered = sorted(
                items,
                key=lambda r: (
                    job_order.get(r.get("category") or r.get("job") or "Autre", 99),
                    int(r.get("level") or 0),
                    r["name"],
                ),
            )
            parts = []
            for item in ordered:
                count = max(1, int(item.get("count", 1)))
                label = f"x{count} {item['name']}"
                if item.get("priority") and int(item.get("distance", 0)) > 1:
                    label += f" ({int(item['distance'])} cartes)"
                if getattr(self, "show_resource_coordinates", True) and item.get("x") is not None and item.get("y") is not None:
                    label += f" [{int(item['x'])}, {int(item['y'])}]"
                parts.append(label)
            return " • ".join(parts)

        current_text = fmt_items(self.current_map_results)
        self.current_map_label.config(
            text=f"Ici : {current_text}" if current_text else "Ici : aucune ressource"
        )

        grouped = {d: [] for d in self.direction_labels}
        for result in results:
            direction = result["direction"]
            if direction in grouped:
                grouped[direction].append(result)

        any_result = False
        for direction, items in grouped.items():
            if items:
                any_result = True
                self.direction_labels[direction].config(text=fmt_items(items))

        if not any_result:
            if self._live and self._live_catalog_ready:
                self.direction_labels["↑"].config(
                    text="Aucune ressource dans la distance configurée"
                )
            else:
                self.direction_labels["↑"].config(text="Aucune ressource dans la distance configurée")

    # ---------- OCR loop ----------

    def _update_live_ocr_diagnostics(self):
        """Refresh the compact live diagnostic panel every Tk cycle."""
        if self._ocr_diagnostic_var is None:
            return
        detector = self.detector
        if detector is None:
            self._ocr_diagnostic_var.set("Diagnostic temps réel : détecteur OCR indisponible")
            return
        try:
            d = detector.debug_snapshot()
            def coord(value):
                return f"{value[0]},{value[1]}" if value else "—"
            def num(value, digits=0):
                return "—" if value is None else f"{value:.{digits}f}"
            elapsed = d.get("cycle_elapsed_ms", 0.0)
            cycle_ms = d.get("last_cycle_ms", 0.0)
            transition = "OUI" if d.get("transition") else "NON"
            worker = "EN COURS" if self._ocr_busy else "LIBRE"
            text = d.get("last_ocr_text") or ""
            text = " ".join(text.replace("\n", " | ").split())
            if len(text) > 65:
                text = text[:65] + "…"
            known = d.get("candidate_known")
            known_text = "OUI" if known is True else "NON" if known is False else "—"
            detect_coord = coord(d.get("detect_coordinates"))
            detect_changed = d.get("detect_changed")
            detect_changed_text = "OUI" if detect_changed is True else "NON" if detect_changed is False else "—"
            detect_success = d.get("detect_success")
            detect_success_text = "OUI" if detect_success is True else "NON" if detect_success is False else "—"
            raw_text = " ".join(str(d.get("ocr_raw_text") or "").replace("\n", " | ").split())
            if len(raw_text) > 80:
                raw_text = raw_text[:80] + "…"
            normalized_text = " ".join(str(d.get("ocr_normalized_text") or "").replace("\n", " | ").split())
            if len(normalized_text) > 80:
                normalized_text = normalized_text[:80] + "…"
            parsed_text = coord(d.get("parsed_coordinates"))
            parse_reason = str(d.get("parse_reason") or "—")
            if len(parse_reason) > 70:
                parse_reason = parse_reason[:70] + "…"
            chosen_text = coord(d.get("chosen_coordinates"))
            chosen_reason = str(d.get("candidate_selection_reason") or "—")
            if len(chosen_reason) > 85:
                chosen_reason = chosen_reason[:85] + "…"
            read_return_text = coord(d.get("read_return_coordinates"))
            read_success = d.get("read_return_success")
            read_success_text = "OUI" if read_success is True else "NON" if read_success is False else "—"
            self._ocr_diagnostic_var.set(
                "Diagnostic temps réel\n"
                f"Cycle #{d.get('cycle', 0)} | Worker : {worker} | Étape : {d.get('stage', '—')} | durée : {elapsed:.0f} ms\n"
                f"Dernier cycle : {cycle_ms:.0f} ms | dernier Tesseract : {num(d.get('last_ocr_ms'))} ms | transition : {transition}\n"
                f"Carte confirmée : {coord(d.get('last_coordinates'))} | Candidate : {coord(d.get('candidate_coordinates') or d.get('last_candidate'))} | distance : {num(d.get('candidate_distance'))}\n"
                f"Base locale : {known_text} | confirmations : {d.get('candidate_count_live', d.get('candidate_count', 0))}/{d.get('required_confirmations', '—')} | décision : {d.get('decision', '—')}\n"
                f"detect() → {detect_coord} | changed : {detect_changed_text} | success : {detect_success_text} | retour : {num(d.get('detect_return_ms'))} ms\n"
                f"OCR brut : {raw_text or 'aucun'}\n"
                f"Normalisé : {normalized_text or 'aucun'} | Extrait : {parsed_text} | Parsing : {parse_reason}\n"
                f"Sélection : {chosen_text} | Pourquoi : {chosen_reason}\n"
                f"Retour read_coordinates() : {read_return_text} | success : {read_success_text}\n"
                f"Motif : {d.get('reason', '—')} | OCR : {text or 'aucun texte'}"
            )
        except Exception as exc:
            self._ocr_diagnostic_var.set(f"Diagnostic temps réel : erreur d'affichage — {type(exc).__name__}: {exc}")

    def _start_ocr_worker(self):
        now = time.monotonic()
        if self.detector is None or self._ocr_busy:
            return
        if now < self._ocr_next_start:
            return

        detector = self.detector
        reset_generation = self._ocr_reset_generation
        self._ocr_busy = True
        # OCR runs in the background; keep confirmed resources visible instead
        # of showing a perpetual “lecture des ressources” state.
        if self.current_coordinates is None:
            self.status_label.config(text="Initialisation OCR…", fg="#e5b35c")

        def worker():
            try:
                if self._ocr_stop_event.is_set():
                    return
                coordinates, changed, raw_text = detector.detect()
                if self._ocr_stop_event.is_set():
                    return
                self._ocr_queue.put(
                    ("result", detector, reset_generation, coordinates, changed, raw_text)
                )
            except Exception as exc:
                try:
                    detector._debug_update(
                        stage="Worker OCR en erreur",
                        decision="ERREUR",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
                self._ocr_queue.put(("error", detector, reset_generation, exc))

        self._ocr_thread = threading.Thread(
            target=worker,
            name="DofusOCR",
            daemon=True,
        )
        # Do not immediately launch another worker as soon as this one finishes.
        # The detector itself also throttles OCR, but avoiding the worker launch
        # altogether removes needless thread/capture churn and keeps the overlay
        # responsive.
        self._ocr_next_start = now + getattr(detector, "OCR_INTERVAL", 0.70)
        self._ocr_started_at = now
        self._ocr_thread.start()

    def _poll_ocr_queue(self):
        got_item = False
        try:
            while True:
                item = self._ocr_queue.get_nowait()
                got_item = True
                kind = item[0]

                if kind == "error":
                    _, detector, generation, exc = item
                    if detector is self.detector and generation == self._ocr_reset_generation:
                        self._last_ocr_error = exc
                        self.debug_label.config(
                            text=f"OCR erreur : {type(exc).__name__}: {exc}"
                        )
                        self.status_label.config(
                            text="OCR erreur",
                            fg="#e58c8c",
                        )
                        # Keep the last confirmed map visible. A transient
                        # screenshot/Tesseract failure must not erase it.
                        if self.current_coordinates is not None:
                            self.coordinate_label.config(
                                text=f"Carte : {self.current_coordinates[0]}, {self.current_coordinates[1]}"
                            )
                            self.direction_labels["↑"].config(
                                text="OCR temporairement indisponible — dernière carte conservée"
                            )
                        else:
                            self.coordinate_label.config(text="Carte : lecture OCR…")
                            self.direction_labels["↑"].config(
                                text="OCR temporairement indisponible"
                            )

                else:
                    _, detector, generation, coordinates, changed, raw_text = item
                    if detector is self.detector and generation == self._ocr_reset_generation:
                        self._last_ocr_error = None

                        if coordinates is not None:
                            first_coordinate = self.current_coordinates is None
                            transition = bool(raw_text and raw_text.startswith("Transition de carte détectée"))
                            ocr_ok = bool(getattr(detector, "last_read_success", False))

                            if changed or first_coordinate:
                                self.current_coordinates = coordinates
                                # Resource searches happen ONLY when the confirmed
                                # map changes. Re-reading the same OCR value no
                                # longer causes a redraw/search cycle.
                                self.current_map_results = self.search_current_map_resources(coordinates)
                                self.current_results = self.search_resources(coordinates)
                                self.display_results(
                                    self.current_coordinates,
                                    self.current_results,
                                )

                            if transition:
                                self.status_label.config(
                                    text="Changement de carte…",
                                    fg="#e5b35c",
                                )
                                self.coordinate_label.config(
                                    text=f"Carte : {coordinates[0]}, {coordinates[1]} !",
                                    fg="#e5b35c",
                                )
                                self.debug_label.config(text="Écran de transition détecté — dernière carte conservée")
                            elif not ocr_ok and self.current_coordinates is not None:
                                # Failed OCR: preserve the last confirmed map/resources
                                # and make the stale coordinate explicit with a small '!'.
                                self.coordinate_label.config(
                                    text=f"Carte : {self.current_coordinates[0]}, {self.current_coordinates[1]} !",
                                    fg="#e5b35c",
                                )
                                self.status_label.config(
                                    text="Dernière carte conservée",
                                    fg="#e5b35c",
                                )
                                raw = (raw_text or "").replace("\n", " | ").strip()
                                self.debug_label.config(
                                    text=(f"OCR indisponible — dernière carte conservée | {raw[:70]}" if raw else "OCR indisponible — dernière carte conservée")
                                )
                            else:
                                self.coordinate_label.config(
                                    text=self._coordinate_text(coordinates),
                                    fg="#f2f4f7",
                                )
                                if changed:
                                    self.status_label.config(
                                        text="Carte détectée",
                                        fg="#8bcf7b",
                                    )
                        else:
                            if self.current_coordinates is None:
                                self.status_label.config(
                                    text="OCR…",
                                    fg="#e5b35c",
                                )
                            else:
                                # A transient unreadable OCR frame is normal:
                                # keep the confirmed map/resources untouched.
                                self.status_label.config(
                                    text="Carte confirmée",
                                    fg="#8bcf7b",
                                )
                            raw = (raw_text or "").replace("\n", " | ").strip()
                            self.debug_label.config(
                                text=(f"OCR brut : {raw[:90]}" if raw else "OCR brut : aucun texte")
                            )
                            if raw:
                                if len(raw) > 42:
                                    raw = raw[:42] + "…"
                                self.direction_labels["↑"].config(
                                    text=f"Lecture OCR : {raw}"
                                )
                            else:
                                self.direction_labels["↑"].config(
                                    text="Lecture OCR : aucun texte"
                                )

        except queue.Empty:
            pass

        if got_item:
            # Exactly one worker is ever in flight (see _start_ocr_worker's
            # busy guard). A queue item therefore means that worker just
            # reported back, so it is now safe to let the next one start.
            self._ocr_busy = False
            self._ocr_started_at = 0.0
        elif (
            self._ocr_busy
            and self._ocr_started_at
            and (time.monotonic() - self._ocr_started_at) > self._ocr_watchdog_timeout
        ):
            # Watchdog safety net: previously this method unconditionally
            # cleared _ocr_busy on every 60ms tick, regardless of whether the
            # worker had actually finished. That silently defeated the "only
            # one OCR worker at a time" guard and let a new background
            # thread/Tesseract subprocess/screen capture stack up every
            # cycle, which is a major source of the reported freezes and of
            # the OCR display getting stuck. We now only clear the flag when
            # a result genuinely arrives — and, as a safety net, if a worker
            # goes silent for longer than _ocr_watchdog_timeout (a hung
            # capture/Tesseract call), so a single bad cycle can never
            # permanently block all future OCR cycles.
            self._ocr_busy = False
            self._ocr_started_at = 0.0

    # ---------- local dataset ----------


    def update(self):
        if self._closing:
            return
        # Never call Tesseract synchronously from Tk's event loop.
        self._poll_ocr_queue()
        self._update_live_ocr_diagnostics()

        # Do not forcibly invalidate a running Python worker. A Python thread
        # cannot be killed safely; starting a second worker while the first one
        # is still alive would create overlapping screen captures/Tesseract
        # processes and make the overlay heavier. Tesseract itself already has
        # a subprocess timeout, so the normal worker lifecycle is sufficient.

        if self.detector is None:
            self.status_label.config(text="OCR absent", fg="#e5b35c")
        elif not self._ocr_busy:
            self._start_ocr_worker()

        try:
            self._after_id = self.root.after(60, self.update)
        except tk.TclError:
            self._after_id = None

    def _make_tray_image(self):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=12, fill=(32, 37, 45, 255))
        draw.rectangle((18, 16, 46, 48), fill=(240, 199, 94, 255))
        draw.rectangle((23, 21, 41, 43), fill=(32, 37, 45, 255))
        return image

    def _start_tray(self):
        if os.name != "nt":
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Afficher", lambda icon, item: self.root.after(0, self.toggle_visibility)),
                pystray.MenuItem("Réglages", lambda icon, item: self.root.after(0, self.show_settings)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quitter", lambda icon, item: self._request_tray_quit()),
            )
            self._tray_icon = pystray.Icon(
                "DofusResourceAssistant",
                self._make_tray_image(),
                "Dofus Resource Assistant",
                menu,
            )
            self._tray_thread = threading.Thread(
                target=self._tray_icon.run, name="TrayIcon", daemon=True
            )
            self._tray_thread.start()
        except Exception:
            self._tray_icon = None

    def _request_tray_quit(self):
        self._tray_quit_requested.set()

    def _poll_tray_quit(self):
        if self._closing:
            return
        if self._tray_quit_requested.is_set():
            self._tray_quit_requested.clear()
            self.close()
            return
        try:
            self.root.after(100, self._poll_tray_quit)
        except tk.TclError:
            pass

    def toggle_visibility(self, event=None):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.update_idletasks()
            self._force_topmost()
        else:
            self.root.withdraw()

    def close(self):
        if self._closing:
            return
        self._closing = True
        self._ocr_stop_event.set()

        for attr in ("_after_id", "_geometry_save_after", "_wheel_show_after"):
            callback_id = getattr(self, attr, None)
            if callback_id is not None:
                try:
                    self.root.after_cancel(callback_id)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

        self._close_settings()

        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

        worker = self._ocr_thread
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

        for detector in [self.detector, *self._detector_history]:
            try:
                if detector:
                    detector.close()
            except Exception:
                pass

        try:
            if self._live:
                self._live.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ResourceOverlay().run()
