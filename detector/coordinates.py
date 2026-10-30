import os
import re
import shutil
import time
import threading
from pathlib import Path

import mss
import pytesseract
from PIL import Image, ImageOps, ImageTk

OCR_REGION = {
    # Default crop validated on the Dofus HUD: coordinate line only, without level.
    "left": int(os.getenv("DOFUS_OCR_LEFT", "3")),
    "top": int(os.getenv("DOFUS_OCR_TOP", "70")),
    "width": int(os.getenv("DOFUS_OCR_WIDTH", "91")),
    "height": int(os.getenv("DOFUS_OCR_HEIGHT", "32")),
}
OCR_INTERVAL = float(os.getenv("DOFUS_OCR_INTERVAL", "0.70"))
REQUIRED_CONFIRMATIONS = int(os.getenv("DOFUS_OCR_CONFIRMATIONS", "2"))
TESSERACT_TIMEOUT = float(os.getenv("DOFUS_OCR_TIMEOUT", "0.55"))
FAST_ACCEPT_KNOWN_NEIGHBOR = os.getenv("DOFUS_OCR_FAST_NEIGHBOR", "1") != "0"
TRANSITION_DARK_RATIO = float(os.getenv("DOFUS_MAP_TRANSITION_DARK_RATIO", "0.82"))
TRANSITION_BRIGHTNESS = float(os.getenv("DOFUS_MAP_TRANSITION_BRIGHTNESS", "45"))
# Release uses the SAME spatial signal as entry (dark_ratio), not an absolute
# mean-brightness threshold. A mean-brightness release condition created a
# "dead zone": any normal but dimly lit map (dungeon, night map, a scene
# partly covered by chat/UI) can sit between TRANSITION_BRIGHTNESS and the
# old release threshold forever, permanently freezing OCR after a single
# transition until the user manually clicked "Réinitialiser la détection".
TRANSITION_RELEASE_DARK_RATIO = float(os.getenv("DOFUS_MAP_TRANSITION_RELEASE_DARK_RATIO", "0.35"))
TRANSITION_COOLDOWN = float(os.getenv("DOFUS_MAP_TRANSITION_COOLDOWN", "0.30"))
# Safety net: a real Dofus map transition never lasts this long. If the probe
# ever misreads the scene (e.g. a persistently dark UI element sits under the
# probe), this guarantees OCR resumes on its own instead of staying stuck
# until a manual reset.
TRANSITION_MAX_DURATION = float(os.getenv("DOFUS_MAP_TRANSITION_MAX_DURATION", "4.0"))
# Background luminance (0-255) above which the HUD capture is treated as a
# "light" background and the stricter/dual-signal glyph mask is used instead
# of the plain near-white filter tuned for dark maps.
LIGHT_BACKGROUND_LUMA = float(os.getenv("DOFUS_OCR_LIGHT_BG_LUMA", "130"))


class TesseractNotFound(RuntimeError):
    pass


def find_tesseract(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.getenv("TESSERACT_CMD")
    if env:
        candidates.append(env)
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)

    local = os.getenv("LOCALAPPDATA", "")
    program_files = os.getenv("PROGRAMFILES", "")
    program_files_x86 = os.getenv("PROGRAMFILES(X86)", "")
    # The installer keeps Tesseract next to the application so a clean PC
    # does not need WinGet, PATH changes, or administrator rights.
    try:
        candidates.append(str(Path(__file__).resolve().parent.parent / "tesseract" / "tesseract.exe"))
    except OSError:
        pass

    candidates.extend([
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(local, "Programs", "Tesseract-OCR", "tesseract.exe"),
        os.path.join(local, "Tesseract-OCR", "tesseract.exe"),
        os.path.join(program_files, "Tesseract-OCR", "tesseract.exe"),
        os.path.join(program_files_x86, "Tesseract-OCR", "tesseract.exe"),
    ])

    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_path in (
                    r"SOFTWARE\Tesseract-OCR",
                    r"SOFTWARE\WOW6432Node\Tesseract-OCR",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR",
                ):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            for value_name in ("Path", "InstallPath", "InstallLocation"):
                                try:
                                    install = winreg.QueryValueEx(key, value_name)[0]
                                    candidates.append(os.path.join(str(install), "tesseract.exe"))
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except ImportError:
            pass

        for root in (os.getenv("ChocolateyToolsLocation"), os.getenv("ProgramData"), os.getenv("LOCALAPPDATA")):
            if not root:
                continue
            try:
                for candidate in Path(root).glob("**/tesseract.exe"):
                    if len(str(candidate)) < 320:
                        candidates.append(str(candidate))
            except (OSError, RuntimeError):
                pass

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.expandvars(os.path.expanduser(str(candidate))).strip('"')
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        if Path(candidate).is_file():
            return str(candidate)
    return None


class CoordinateDetector:
    """OCR tuned for Dofus' fixed coordinate line over an animated background."""

    def __init__(self, region=None, tesseract_cmd=None, coordinate_validator=None, transition_region=None):
        self.region = dict(region or OCR_REGION)
        self.transition_region = dict(transition_region or self.region)
        self.tesseract_cmd = find_tesseract(tesseract_cmd)
        self.coordinate_validator = coordinate_validator
        if not self.tesseract_cmd:
            raise TesseractNotFound(
                "Tesseract introuvable. Indiquez le chemin de tesseract.exe dans les réglages OCR."
            )
        pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        # mss objects are kept per worker thread. The background OCR
        # worker reused an mss instance created on the Tk/main thread, which
        # can hang or fail silently on Windows.
        self._mss_local = threading.local()
        self._ocr_in_progress = False
        self.last_coordinates = None
        self.candidate_coordinates = None
        self.candidate_count = 0
        self.last_ocr_text = ""
        self.last_read_time = 0.0
        self.last_read_success = False
        self.initializing = True
        self.transition_active = False
        self.transition_started_at = 0.0
        self.transition_last_dark_at = 0.0
        self.post_transition_reads = 0
        # Live diagnostic state consumed by the overlay. Values are intentionally
        # simple/atomic so the OCR worker can update them while Tk displays them.
        self._debug_lock = threading.Lock()
        self._debug = {
            "stage": "En attente",
            "cycle": 0,
            "cycle_started_at": 0.0,
            "last_cycle_ms": 0.0,
            "last_ocr_ms": 0.0,
            "last_ocr_text": "",
            "last_candidate": None,
            "candidate_distance": None,
            "candidate_known": None,
            "candidate_support": None,
            "candidate_count": 0,
            "required_confirmations": REQUIRED_CONFIRMATIONS,
            "decision": "—",
            "reason": "—",
            "background_luma": None,
            "transition": False,
            "detect_coordinates": None,
            "detect_changed": None,
            "detect_success": None,
            "detect_return_ms": 0.0,
            "ocr_raw_text": "",
            "ocr_normalized_text": "",
            "parsed_coordinates": None,
            "parse_reason": "—",
            "chosen_coordinates": None,
            "candidate_selection_reason": "—",
            "read_return_coordinates": None,
            "read_return_success": None,
        }
        self._ocr_config_values = self._build_ocr_configs()

    def _debug_update(self, **values):
        with self._debug_lock:
            self._debug.update(values)

    def debug_snapshot(self):
        """Return a consistent snapshot for the live OCR diagnostics panel."""
        with self._debug_lock:
            data = dict(self._debug)
        started = data.get("cycle_started_at") or 0.0
        data["cycle_elapsed_ms"] = ((time.monotonic() - started) * 1000.0) if started else 0.0
        data["transition"] = bool(self.transition_active)
        data["ocr_busy"] = bool(getattr(self, "_ocr_in_progress", False))
        data.setdefault("transition_dark_ratio", None)
        data.setdefault("transition_mean_luma", None)
        data["last_coordinates"] = self.last_coordinates
        data["last_read_success"] = bool(self.last_read_success)
        data["candidate_coordinates"] = self.candidate_coordinates
        data["candidate_count_live"] = self.candidate_count
        return data

    def reset_detection(self):
        """Fully restart coordinate detection from the next OCR frame.

        This is an intentional hard reset: the previous coordinate is not kept
        as a recovery anchor and no previous candidate/confirmation state is
        reused. The next successful OCR reading is therefore treated exactly
        like the first reading after application startup.
        """
        self.last_coordinates = None
        self.candidate_coordinates = None
        self.candidate_count = 0
        self.last_ocr_text = ""
        self.last_read_time = 0.0
        self.last_read_success = False
        self.initializing = True
        self.transition_active = False
        self.transition_started_at = 0.0
        self.transition_last_dark_at = 0.0
        self.post_transition_reads = 0
        self._debug_update(
            stage="Réinitialisation",
            decision="—",
            reason="Détection réinitialisée",
            detect_coordinates=None,
            detect_changed=None,
            detect_success=None,
            detect_return_ms=0.0,
            candidate_known=None,
            ocr_raw_text="",
            ocr_normalized_text="",
            parsed_coordinates=None,
            parse_reason="—",
            chosen_coordinates=None,
            candidate_selection_reason="—",
            read_return_coordinates=None,
            read_return_success=None,
        )

    def _screen_transition_state(self):
        """Detect a Dofus map transition from the user-defined transition zone."""
        try:
            sct = self._get_sct()
            shot = sct.grab(self.transition_region)
            dark = 0
            lum_sum = 0
            for yy in range(0, shot.height, 2):
                for xx in range(0, shot.width, 2):
                    r, g, b = shot.pixel(xx, yy)
                    lum = (77 * r + 150 * g + 29 * b) >> 8
                    lum_sum += lum
                    if lum <= TRANSITION_BRIGHTNESS:
                        dark += 1
            samples = max(1, ((shot.width + 1) // 2) * ((shot.height + 1) // 2))
            dark_ratio = dark / samples
            mean_lum = lum_sum / samples
            now = time.monotonic()
            dark_now = (
                dark_ratio >= TRANSITION_DARK_RATIO
                and mean_lum <= TRANSITION_BRIGHTNESS * 1.20
            )

            self._debug_update(transition_dark_ratio=dark_ratio, transition_mean_luma=mean_lum)

            # The safety-net timeout must be checked every cycle a transition
            # is active, regardless of the current dark_now reading. It used
            # to live only inside the "not dark_now" branch below, so a
            # transition zone that stays continuously dark — e.g. redrawn
            # over a permanently dark UI element instead of the actual black
            # screen-transition flash — took the "dark_now" early return on
            # every single cycle and never reached the timeout check at all.
            # OCR then froze on "Transition de carte détectée — OCR suspendu"
            # forever, even after the user redrew the zone, because the new
            # zone was just as permanently dark. TRANSITION_MAX_DURATION was
            # documented as an unconditional guarantee above; it needs to be
            # evaluated unconditionally too.
            if self.transition_active and (now - self.transition_started_at) >= TRANSITION_MAX_DURATION:
                self.transition_active = False
                self._debug_update(
                    stage="Transition terminée",
                    transition=False,
                    reason="Délai de sécurité atteint — vérifiez le placement de la zone de transition",
                )
                self.post_transition_reads = 0
                self.candidate_coordinates = None
                self.candidate_count = 0
                return False

            if dark_now:
                self._debug_update(stage="Transition détectée", transition=True, reason="Zone de transition majoritairement noire")
                self.transition_last_dark_at = now
                if not self.transition_active:
                    self.transition_active = True
                    self.transition_started_at = now
                    self.candidate_coordinates = None
                    self.candidate_count = 0
                return True

            if self.transition_active:
                released = dark_ratio <= TRANSITION_RELEASE_DARK_RATIO
                if released:
                    self.transition_active = False
                    self._debug_update(stage="Transition terminée", transition=False, reason="Zone de transition libérée")
                    self.post_transition_reads = 0
                    self.candidate_coordinates = None
                    self.candidate_count = 0
                    return False
                if now - self.transition_last_dark_at < TRANSITION_COOLDOWN:
                    return True
            return self.transition_active
        except Exception:
            return self.transition_active

    def is_transition_active(self):
        return self._screen_transition_state()

    def _get_sct(self):
        sct = getattr(self._mss_local, "instance", None)
        if sct is None:
            sct = mss.mss()
            self._mss_local.instance = sct
        return sct

    def capture(self):
        screenshot = self._get_sct().grab(self.region)
        return Image.frombytes("RGB", screenshot.size, screenshot.rgb)

    @staticmethod
    def _estimate_background_luma(rgb_image):
        """Approximate the brightness of the map behind the HUD text.

        The glyphs only cover a small fraction of this tiny crop, so the
        median luminance is a robust background estimate: it isn't skewed by
        the handful of very bright (white fill) or very dark (outline)
        pixels that belong to the text itself.
        """
        gray = rgb_image.convert("L")
        values = sorted(gray.getdata())
        if not values:
            return 128.0
        return float(values[len(values) // 2])

    @staticmethod
    def _channel_mask(src, min_value=None, max_value=None, max_spread=35, dark=False):
        """Binary mask of pixels matching a brightness/saturation band.

        ``dark=False`` keeps near-white pixels (``min channel >= min_value``);
        ``dark=True`` keeps near-black pixels (``max channel <= max_value``).
        Both modes also require low saturation (``max-min <= max_spread``) so
        that colourful/textured map pixels are excluded even when they
        happen to be bright or dark enough on their own.
        """
        out = Image.new("L", src.size, 0)
        src_px = src.load()
        dst_px = out.load()
        for y in range(src.height):
            for x in range(src.width):
                r, g, b = src_px[x, y]
                mn = min(r, g, b)
                mx = max(r, g, b)
                if (mx - mn) > max_spread:
                    continue
                if dark:
                    if mx <= max_value:
                        dst_px[x, y] = 255
                else:
                    if mn >= min_value:
                        dst_px[x, y] = 255
        return out

    def _targeted_coordinate_mask(self, image, background_luma=None):
        """Keep the Dofus HUD glyph pixels, adapting to a light or dark map.

        The coordinate text is always a near-white fill with a dark outline.
        Against a dark background (dungeon, water, night map) the white fill
        alone stands out clearly, so a fairly permissive near-white/
        low-saturation filter is enough — this is the mode the detector has
        always used.

        Against a LIGHT background (snow, sand, pale stone, a bright sky),
        plenty of terrain pixels are also bright and low-saturation, so that
        same loose filter used to paint large parts of the background white
        too and made Tesseract hallucinate digits — a very bright, fairly
        uniform tile (fresh snow, a pale wall) can even be picked up
        entirely, since nothing then distinguishes it from the glyph fill on
        brightness alone. On a light background we therefore rely on the
        glyph's dark outline instead: unlike the white fill, it cannot be
        confused with a bright background no matter how uniform that
        background is. The outline is thin, so it is dilated to reconnect
        strokes into solid, readable glyph shapes; the near-white fill is
        then only ADDED where it overlaps that outline-derived shape (never
        used on its own), which keeps the light-background mode fully immune
        to the failure mode above.
        """
        src = image.convert("RGB")
        if background_luma is None:
            background_luma = self._estimate_background_luma(src)

        if background_luma >= LIGHT_BACKGROUND_LUMA:
            # On a light map, the useful signal is the *bright fill* of the
            # coordinate glyphs.  Using FIND_EDGES here is tempting because
            # the glyphs have a dark outline, but it turns every anti-aliased
            # border into thin disconnected contours. Tesseract then sees
            # several unrelated blobs (for example ``-31,-874`` instead of
            # ``-31,-57``).
            #
            # The actual HUD fill is close to white (roughly 235-255), while
            # the light terrain behind it is normally below that level. A
            # fixed threshold is appropriate because this is a fixed-size
            # 91x32 HUD capture and the Dofus font is rendered at a stable
            # scale. Keeping the whole bright fill intact gives Tesseract
            # complete digit shapes rather than outlines.
            gray = src.convert("L")
            return gray.point(lambda p: 255 if p >= 230 else 0)

        return self._channel_mask(src, min_value=140, max_spread=35)


    @staticmethod
    def _parse_coordinates_details(text):
        """Parse OCR text and expose every parsing step for diagnostics.

        Returns:
            (coordinates, normalized_text, reason)
        """
        raw = "" if text is None else str(text)
        if not raw.strip():
            return None, "", "OCR vide"

        cleaned = (
            raw.replace(";", ",")
            .replace("，", ",")
            .replace("．", ".")
            .replace("−", "-")
            .replace("–", "-")
            .replace("—", "-")
            .replace("_", "-")
        )
        # Keep the normalized representation readable in the live diagnostic.
        normalized = " ".join(cleaned.replace("\n", " | ").split())

        # First choice: explicit X,Y or X.Y pair.
        explicit_patterns = (
            r"(?<!\d)([+-]?\d{1,3})\s*[,\.]\s*([+-]?\d{1,3})(?!\d)",
            r"(?<!\d)([+-]?\d{1,3})\s+([+-]?\d{1,3})(?!\d)",
        )
        for priority, pattern in enumerate(explicit_patterns):
            for match in re.finditer(pattern, cleaned):
                try:
                    x, y = int(match.group(1)), int(match.group(2))
                except ValueError:
                    continue
                if -200 <= x <= 200 and -200 <= y <= 200:
                    separator = "virgule" if priority == 0 else "espace"
                    return (x, y), normalized, f"paire X/Y trouvée ({separator})"

        # Fallback when punctuation is damaged or lost.
        signed_numbers = re.findall(r"[-+]?\d{1,3}", cleaned)
        valid = []
        for idx in range(len(signed_numbers) - 1):
            try:
                x, y = int(signed_numbers[idx]), int(signed_numbers[idx + 1])
            except ValueError:
                continue
            if -200 <= x <= 200 and -200 <= y <= 200:
                valid.append((idx, x, y))
        if valid:
            _, x, y = valid[0]
            return (x, y), normalized, "paire X/Y trouvée par nombres signés"

        if signed_numbers:
            return None, normalized, (
                f"nombres détectés ({', '.join(signed_numbers[:6])}) "
                "mais aucune paire X/Y valide"
            )
        return None, normalized, "aucun nombre entier exploitable"

    @staticmethod
    def extract_coordinates(text):
        """Extract coordinates using the detector's own parser."""
        coordinates, _, _ = CoordinateDetector._parse_coordinates_details(text)
        return coordinates

    @staticmethod
    def _build_ocr_configs():
        """Small deterministic configs for numeric coordinate OCR."""
        base = (
            "-c tessedit_char_whitelist=-0123456789,. "
            "-c load_system_dawg=0 -c load_freq_dawg=0 "
            "-c user_defined_dpi=300"
        )
        return (
            f"--oem 3 --psm 7 {base}",
            f"--oem 3 --psm 8 {base}",
        )

    def _prepare_coordinate_mask(self, src):
        mask = self._targeted_coordinate_mask(src)
        # The source is tiny (typically 91x32). Fourfold enlargement preserves
        # the pixel-font edges without creating the huge CPU cost of 6x/8x.
        prepared = ImageOps.expand(mask, border=2, fill=0)
        return prepared.resize(
            (prepared.width * 4, prepared.height * 4),
            Image.Resampling.LANCZOS,
        )

    def _targeted_coordinate_ocr(self, src):
        """Use the smallest useful set of OCR passes for the current background."""
        w, h = src.size
        if h < 12 or w < 60:
            return [], ""

        results = []
        texts = []
        background_luma = self._estimate_background_luma(src)

        # On light maps the primary mask keeps the bright glyph fill intact.
        # A slightly lower threshold is kept as a cheap recovery for maps
        # where anti-aliasing makes the white fill a little darker.
        masks = [(self._targeted_coordinate_mask(src, background_luma), 4)]
        if background_luma >= LIGHT_BACKGROUND_LUMA:
            gray = src.convert("L")
            masks.append((gray.point(lambda p: 255 if p >= 220 else 0), 2))

        for mask, weight in masks:
            prepared = ImageOps.expand(mask, border=2, fill=0)
            prepared = prepared.resize(
                (prepared.width * 4, prepared.height * 4),
                Image.Resampling.LANCZOS,
            )
            config_index = 1 if background_luma >= LIGHT_BACKGROUND_LUMA else 0
            text = self._ocr_image(prepared, self._ocr_config_values[config_index])
            if not text:
                continue
            texts.append(text)
            coord = self.extract_coordinates(text)
            if coord is not None:
                results.append((coord, text, weight))
                # The primary pass is normally enough. If it produced a
                # candidate, let the database/geometry scoring decide whether
                # it is plausible rather than spending another Tesseract call.
                if weight >= 4:
                    return results, "\n".join(texts)

        # Final recovery on the original luminance image. This is deliberately
        # last: on bright terrain the raw grayscale image contains much more
        # background than text, but it remains useful for unusual captures.
        gray = ImageOps.grayscale(src)
        gray = ImageOps.autocontrast(gray)
        gray = gray.resize((gray.width * 4, gray.height * 4), Image.Resampling.LANCZOS)
        text = self._ocr_image(gray, self._ocr_config_values[0])
        if text:
            texts.append(text)
            coord = self.extract_coordinates(text)
            if coord is not None:
                results.append((coord, text, 1))

        return results, "\n".join(texts)

    def show_preview(self, parent=None):
        """Show a visual preview of the exact configured OCR region.

        The preview intentionally uses the same capture and preprocessing as
        the detector, so what is displayed is what Tesseract actually receives.
        """
        import tkinter as tk

        src = self.capture().convert("RGB")
        background_luma = self._estimate_background_luma(src)
        light_background = background_luma >= LIGHT_BACKGROUND_LUMA
        mask = self._targeted_coordinate_mask(src, background_luma=background_luma)

        # Make small HUD captures readable without changing the source image.
        scale = max(3, min(8, 720 // max(1, src.width)))
        raw = src.resize((src.width * scale, src.height * scale), Image.Resampling.NEAREST)
        processed = mask.resize((mask.width * scale, mask.height * scale), Image.Resampling.NEAREST).convert("RGB")

        config_index = 1 if light_background else 0
        config = self._ocr_config_values[config_index]
        prepared = ImageOps.expand(mask, border=3, fill=0)
        prepared = prepared.resize((prepared.width * 6, prepared.height * 6), Image.Resampling.LANCZOS)
        text = self._ocr_image(prepared, config)
        coordinates = self.extract_coordinates(text)

        win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        win.title("Aperçu zone OCR")
        win.resizable(False, False)
        if parent is not None:
            try:
                win.transient(parent)
            except tk.TclError:
                pass

        outer = tk.Frame(win, padx=10, pady=10)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text=f"Zone capturée : {src.width} × {src.height} px", anchor="w").pack(fill="x")
        fond_label = "clair" if light_background else "sombre"
        tk.Label(
            outer,
            text=f"Fond détecté : {fond_label} (luminosité {background_luma:.0f}/255) — filtre adapté appliqué",
            anchor="w",
        ).pack(fill="x")

        previews = tk.Frame(outer)
        previews.pack(fill="x", pady=(8, 8))

        raw_photo = ImageTk.PhotoImage(raw)
        processed_photo = ImageTk.PhotoImage(processed)

        raw_box = tk.LabelFrame(previews, text="Capture réelle", padx=4, pady=4)
        raw_box.pack(side="left", padx=(0, 6))
        tk.Label(raw_box, image=raw_photo, bd=0).pack()

        processed_box = tk.LabelFrame(previews, text="Prétraitement OCR", padx=4, pady=4)
        processed_box.pack(side="left", padx=(6, 0))
        tk.Label(processed_box, image=processed_photo, bd=0).pack()

        result = text.replace("\n", " | ").strip() or "(vide)"
        tk.Label(outer, text=f"OCR : {result}", anchor="w", justify="left", wraplength=900).pack(fill="x")
        tk.Label(outer, text=f"Coordonnées : {coordinates or 'aucune'}", anchor="w").pack(fill="x", pady=(3, 8))
        tk.Button(outer, text="Fermer", command=win.destroy).pack(anchor="e")

        # Keep the PhotoImage objects alive for the lifetime of the window.
        win._preview_images = (raw_photo, processed_photo)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        win.focus_force()
        return win

    def _ocr_image(self, image, config):
        """Run one tightly scoped OCR call and expose live timing to diagnostics."""
        started = time.monotonic()
        self._ocr_in_progress = True
        self._debug_update(stage="Tesseract en cours")
        try:
            return pytesseract.image_to_string(
                image, lang="eng", config=config, timeout=TESSERACT_TIMEOUT
            ).strip()
        except Exception:
            self._debug_update(stage="Tesseract terminé", reason="Tesseract a renvoyé une erreur/exception")
            return ""
        finally:
            elapsed = (time.monotonic() - started) * 1000.0
            self._ocr_in_progress = False
            self._debug_update(last_ocr_ms=elapsed)

    def _candidate_distance(self, a, b):
        if a is None or b is None:
            return None
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    @staticmethod
    def _is_cardinal_neighbor(a, b):
        if a is None or b is None:
            return False
        dx = abs(a[0] - b[0])
        dy = abs(a[1] - b[1])
        return (dx == 1 and dy == 0) or (dx == 0 and dy == 1)

    def _choose_candidate(self, results, previous=None):
        """Score OCR candidates using independent-pass support + map geometry.

        The diagnostic records not only the parsed coordinate, but also the
        exact candidate-selection outcome. This is intentionally diagnostic:
        the acceptance/rejection rules themselves are unchanged.
        """
        if not results:
            self._debug_update(
                chosen_coordinates=None,
                candidate_selection_reason="Aucun résultat OCR parsé",
            )
            return None

        buckets = {}
        for coord, text, weight in results:
            item = buckets.setdefault(coord, {"support": 0, "weight": 0, "texts": []})
            item["support"] += 1
            item["weight"] += weight
            item["texts"].append(text)

        scored = []
        for coord, item in buckets.items():
            score = item["weight"] * 1.0 + item["support"] * 1.25
            known = False
            if self.coordinate_validator is not None:
                try:
                    known = bool(self.coordinate_validator(*coord))
                except Exception:
                    known = False
            if known:
                score += 1.0

            distance = self._candidate_distance(coord, previous)
            if previous is not None:
                if coord == previous:
                    score += 4.0
                elif self._is_cardinal_neighbor(coord, previous):
                    score += 7.0
                elif distance == 1:
                    score += 1.0
                elif distance is not None:
                    score -= min(8.0, distance * 0.75)
                if distance is not None and distance > 1 and not known:
                    score -= 4.0

            scored.append((score, item["support"], item["weight"], known, distance, coord))

        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        best = scored[0]
        best_score, support, weight, known, distance, coord = best

        self._debug_update(
            chosen_coordinates=None,
            candidate_selection_reason=(
                f"Meilleur candidat {coord} | score {best_score:.2f} | "
                f"passes {support} | poids {weight} | "
                f"base={'OUI' if known else 'NON'} | distance={distance if distance is not None else '—'}"
            ),
        )

        if previous is not None:
            # A single far-away parse used to be vetoed outright here whenever
            # it was absent from the local database or seen by only one OCR
            # pass (support < 2). In practice `known=False` for any legitimate
            # map without a recorded resource, and a single pass is the normal
            # case (a second pass only runs on low confidence) — so this
            # veto fired on essentially every cycle and permanently prevented
            # such a map from ever being confirmed, no matter how many times
            # it was read identically. The cross-cycle confirmation counter in
            # detect() already requires several consistent identical reads
            # before accepting an unvalidated far candidate (a stricter
            # threshold than for known/neighbouring maps) — that is the right
            # place to guard against a fluke OCR misread. This function only
            # needs to flag the uncertainty for the diagnostic, not veto it.
            if distance is not None and distance > 1 and (not known or support < 2):
                reason = (
                    "Candidat distant retenu sous réserve : "
                    + ("absent de la base locale" if not known else "un seul résultat OCR")
                    + " — confirmation renforcée déléguée à detect()"
                )
                self._debug_update(
                    chosen_coordinates=coord,
                    candidate_selection_reason=reason,
                )
                return coord
        else:
            if support < 1:
                self._debug_update(
                    chosen_coordinates=None,
                    candidate_selection_reason="Aucun support OCR",
                )
                return None

        self._debug_update(
            chosen_coordinates=coord,
            candidate_selection_reason=(
                f"Candidat accepté par le sélecteur | score {best_score:.2f}"
            ),
        )
        return coord

    def read_coordinates(self, previous=None):
        """Read coordinates with a light primary path and selective recovery."""
        if previous is None:
            previous = self.last_coordinates

        self._debug_update(
            stage="Test transition",
            decision="—",
            reason="—",
            chosen_coordinates=None,
            candidate_selection_reason="—",
            read_return_coordinates=None,
            read_return_success=None,
        )
        if self.is_transition_active():
            self._debug_update(stage="OCR suspendu", transition=True, decision="REJET", reason="Transition active")
            self.last_ocr_text = "Transition de carte détectée — OCR suspendu"
            self.last_read_success = False
            self._debug_update(
                read_return_coordinates=self.last_coordinates,
                read_return_success=False,
                stage="Lecture OCR interrompue par transition",
            )
            return self.last_coordinates, self.last_ocr_text

        image = self.capture()
        self._debug_update(stage="Prétraitement OCR")
        results, text = self._targeted_coordinate_ocr(image)
        parsed_coordinates, normalized_text, parse_reason = self._parse_coordinates_details(text)
        self._debug_update(
            stage="OCR terminé",
            last_ocr_text=text,
            ocr_raw_text=text or "",
            ocr_normalized_text=normalized_text,
            parsed_coordinates=parsed_coordinates,
            parse_reason=parse_reason,
            background_luma=self._estimate_background_luma(image),
        )

        preliminary = self._choose_candidate(results, previous=previous)
        suspicious = preliminary is None
        if preliminary is not None and previous is not None:
            suspicious = self._candidate_distance(preliminary, previous) > 1

        # Expensive second pass only for a failed/implausible reading.
        if suspicious:
            prepared = self._prepare_coordinate_mask(image)
            text2 = self._ocr_image(prepared, self._ocr_config_values[1])
            if text2:
                text = (text + "\n" + text2).strip()
                parsed2, normalized2, reason2 = self._parse_coordinates_details(text2)
                self._debug_update(
                    stage="Second OCR / parsing",
                    ocr_raw_text=text,
                    ocr_normalized_text=normalized2,
                    parsed_coordinates=parsed2,
                    parse_reason=f"Second OCR : {reason2}",
                )
                coord = parsed2
                if coord is not None:
                    results.append((coord, text2, 3))

        self.last_ocr_text = text
        final_parsed, final_normalized, final_reason = self._parse_coordinates_details(text)
        self._debug_update(
            ocr_raw_text=text or "",
            ocr_normalized_text=final_normalized,
            parsed_coordinates=final_parsed,
            parse_reason=final_reason,
        )
        chosen = self._choose_candidate(results, previous=previous)
        if chosen is None:
            self._debug_update(stage="Candidat rejeté", decision="REJET", reason="Aucun candidat OCR accepté", last_candidate=None)
            self.last_read_success = False
            self._debug_update(
                read_return_coordinates=None,
                read_return_success=False,
                stage="Aucun candidat accepté",
                reason=self._debug.get("candidate_selection_reason", "Aucun candidat OCR accepté"),
            )
            return None, self.last_ocr_text
        self._debug_update(stage="Candidat OCR trouvé", last_candidate=chosen, decision="À valider", reason="Lecture OCR exploitable")

        if previous is not None:
            chosen = self._correct_lost_sign(chosen, previous)
            chosen = self._neighbor_correction(chosen, self.last_ocr_text, previous)
        self.last_read_success = True
        self._debug_update(
            stage="read_coordinates() retourne une coordonnée",
            read_return_coordinates=chosen,
            read_return_success=True,
            chosen_coordinates=chosen,
            candidate_selection_reason="Coordonnée choisie puis corrections locales appliquées",
        )
        return chosen, self.last_ocr_text

    @staticmethod
    def _correct_lost_sign(coordinates, previous):
        """Correct a very likely OCR sign loss during a normal map move.

        Dofus moves normally change one axis by one map. Tesseract can drop the
        leading '-' on a coordinate (e.g. -22,-60 -> -22,60). A signed
        Chebyshev distance then makes the OCR result look 121 maps away. We
        only auto-correct the unambiguous local case: one axis is unchanged,
        the other differs only by its sign, and the absolute value is within
        two maps. We deliberately do not correct two-axis changes, so genuine
        teleports/jumps remain possible.
        """
        if coordinates is None or previous is None:
            return coordinates

        px, py = previous
        x, y = coordinates

        # Lost sign on Y: X stayed exactly the same and |Y| is a normal move.
        if (x == px and y != py and py != 0 and y != 0
                and ((py < 0) != (y < 0)) and abs(abs(y) - abs(py)) <= 2):
            return x, -abs(y) if py < 0 else abs(y)

        # Lost sign on X: Y stayed exactly the same and |X| is a normal move.
        if (y == py and x != px and px != 0 and x != 0
                and ((px < 0) != (x < 0)) and abs(abs(x) - abs(px)) <= 2):
            return -abs(x) if px < 0 else abs(x), y

        return coordinates

    def _neighbor_correction(self, coordinates, raw_text, previous):
        """Recover a nearby real map when OCR corrupts one or two glyphs.

        A bad frame can turn e.g. ``-22,-59`` into ``-22,39`` (the 5 is
        misread and the leading minus is lost).  The old confidence system
        could then eventually accept that wrong coordinate and the resource
        search was performed around a completely different map.

        When a previous confirmed map exists, the only plausible maps for a
        normal movement are its 8 immediate neighbours.  Compare the OCR
        reading with those signed coordinates and use the closest textual
        match, but only when the match is clearly better than the raw result.
        This is a correction, not a hard rejection: larger jumps and
        teleports still go through the normal confirmation logic.
        """
        if coordinates is None or previous is None:
            return coordinates

        px, py = previous
        rx, ry = coordinates

        # Never reinterpret a result that is already an immediate neighbour.
        # It is more important to preserve genuine movement than to over-correct.
        if max(abs(rx - px), abs(ry - py)) <= 1:
            return coordinates

        # Extract just the first two signed integer tokens from the OCR result.
        cleaned = (raw_text or '').replace('−', '-').replace('–', '-').replace('—', '-')
        tokens = re.findall(r'[-+]?\d{1,3}', cleaned)
        if len(tokens) < 2:
            return coordinates

        raw_tokens = [tokens[0], tokens[1]]

        # Weighted textual distance.  A missing '-' is cheap because it is a
        # common OCR failure; digit substitutions cost one.
        def token_cost(a, b):
            if a == b:
                return 0.0
            if a.lstrip('+-') == b.lstrip('+-'):
                return 0.15  # sign only
            aa, bb = a.lstrip('+-'), b.lstrip('+-')
            cost = abs(len(aa) - len(bb)) * 1.0
            # Common Tesseract confusions on this HUD font get a slightly
            # lower penalty. In particular 3/5 is a frequent confusion on
            # the yellow animated background and helps recover -59 from 39.
            confusion = {
                frozenset(("3", "5")): 0.55,
                frozenset(("5", "6")): 0.75,
                frozenset(("3", "8")): 0.80,
                frozenset(("5", "8")): 0.80,
                frozenset(("1", "7")): 0.80,
                frozenset(("0", "6")): 0.80,
                frozenset(("8", "9")): 0.80,
            }
            for ca, cb in zip(aa, bb):
                if ca == cb:
                    continue
                cost += confusion.get(frozenset((ca, cb)), 1.0)
            if a[:1] in '+-' and b[:1] in '+-' and a[:1] != b[:1]:
                cost += 0.9
            elif a[:1] in '+-' and b[:1] not in '+-':
                cost += 0.35
            elif b[:1] in '+-' and a[:1] not in '+-':
                cost += 0.35
            return cost

        def candidate_cost(cand):
            cx, cy = cand
            return token_cost(raw_tokens[0], str(cx)) + token_cost(raw_tokens[1], str(cy))

        candidates = [(px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)]

        if not candidates:
            return coordinates

        ranked = sorted((candidate_cost(c), c) for c in candidates)
        best_cost, best = ranked[0]
        second_cost = ranked[1][0] if len(ranked) > 1 else float('inf')

        # Require a clear textual winner. This prevents an ambiguous OCR result
        # from being silently forced onto an arbitrary neighbouring map.
        if best_cost <= 1.80 and (second_cost - best_cost >= 0.35 or len(ranked) == 1):
            return best
        return coordinates

    def _debug_detect_return(self, coordinates, changed, success, started_at):
        """Record the exact public result returned by detect()."""
        self._debug_update(
            detect_coordinates=coordinates,
            detect_changed=bool(changed),
            detect_success=bool(success),
            detect_return_ms=(time.monotonic() - started_at) * 1000.0,
        )

    def detect(self):
        now = time.monotonic()
        with self._debug_lock:
            self._debug["cycle"] += 1
            cycle = self._debug["cycle"]
            self._debug["cycle_started_at"] = now
            self._debug["stage"] = "Démarrage du cycle"
            self._debug["decision"] = "—"
            self._debug["reason"] = "—"
            self._debug["detect_coordinates"] = None
            self._debug["detect_changed"] = None
            self._debug["detect_success"] = None
            self._debug["detect_return_ms"] = 0.0
            self._debug["ocr_raw_text"] = ""
            self._debug["ocr_normalized_text"] = ""
            self._debug["parsed_coordinates"] = None
            self._debug["parse_reason"] = "—"

        previous = self.last_coordinates
        if now - self.last_read_time < OCR_INTERVAL:
            self._debug_update(
                stage="Cycle ignoré (intervalle)",
                decision="IGNORÉ",
                reason=f"Intervalle OCR {OCR_INTERVAL:.2f}s",
            )
            result_coordinates = self.last_coordinates
            result_changed = False
            result_success = bool(self.last_read_success)
            self._debug_detect_return(result_coordinates, result_changed, result_success, now)
            return result_coordinates, result_changed, self.last_ocr_text

        self.last_read_time = now

        self._debug_update(stage="Appel read_coordinates()")
        coordinates, text = self.read_coordinates(previous=self.last_coordinates)
        self._debug_update(
            stage="Retour read_coordinates()",
            last_ocr_text=text,
        )

        if coordinates is None:
            self.last_read_success = False
            self._debug_update(
                stage="Aucune coordonnée",
                decision="CONSERVÉE",
                reason="Lecture OCR non exploitable",
                last_cycle_ms=(time.monotonic() - now) * 1000.0,
            )
            result_coordinates = self.last_coordinates
            result_changed = False
            result_success = False
            self._debug_detect_return(result_coordinates, result_changed, result_success, now)
            return result_coordinates, result_changed, text

        # Tesseract sometimes drops the leading '-' on exactly one coordinate.
        # Resolve that ambiguity from the last confirmed map before applying
        # the normal confidence/confirmation logic.
        if self.last_coordinates is not None:
            coordinates = self._correct_lost_sign(coordinates, self.last_coordinates)
            coordinates = self._neighbor_correction(
                coordinates, text, self.last_coordinates
            )

        # Explicitly expose whether the OCR candidate exists in the local DB,
        # including when it is the current map. This makes "Base locale : —"
        # unambiguous in the live diagnostic.
        candidate_known = None
        if self.coordinate_validator is not None:
            try:
                candidate_known = bool(self.coordinate_validator(*coordinates))
            except Exception:
                candidate_known = False

        self._debug_update(
            last_candidate=coordinates,
            candidate_coordinates=coordinates,
            candidate_known=candidate_known,
            stage="Validation de la coordonnée",
        )

        required_confirmations = REQUIRED_CONFIRMATIONS
        if getattr(self, "post_transition_reads", 0) < 2 and previous is not None:
            required_confirmations = max(required_confirmations, 3)
            self.post_transition_reads += 1

        far_candidate = False
        distance = None

        if self.last_coordinates is not None:
            px, py = self.last_coordinates
            x, y = coordinates
            dx = abs(x - px)
            dy = abs(y - py)
            distance = max(dx, dy)
            far_candidate = distance > 1

            sign_flip_x = px != 0 and x != 0 and ((px < 0) != (x < 0))
            sign_flip_y = py != 0 and y != 0 and ((py < 0) != (y < 0))

            if distance <= 1:
                required_confirmations = REQUIRED_CONFIRMATIONS
            elif distance <= 3:
                required_confirmations = max(REQUIRED_CONFIRMATIONS, 3)
            else:
                required_confirmations = max(REQUIRED_CONFIRMATIONS, 4)

            if (sign_flip_x and dx > 1) or (sign_flip_y and dy > 1):
                required_confirmations = max(required_confirmations, 4)

            self._debug_update(
                candidate_distance=distance,
                required_confirmations=required_confirmations,
                candidate_known=candidate_known,
            )

            # A distant candidate absent from the local database is NOT proof
            # of a bad OCR read: the bundled database only lists maps with at
            # least one known gatherable resource (see local_resource_db.py),
            # so a perfectly legitimate map without any recorded resource is
            # indistinguishable here from a genuine misread. Hard-rejecting it
            # forever (as in earlier versions) kept candidate_count stuck at 0
            # indefinitely, even after dozens of identical, correct OCR
            # reads — the map could never be confirmed until the user hit
            # "Réinitialiser la détection". We now only raise the bar: an
            # unvalidated far candidate must be read identically several more
            # times in a row than a validated one before it is accepted, but
            # it is no longer permanently vetoed. Execution falls through to
            # the normal confirmation-counting logic below.
            if far_candidate and not candidate_known:
                required_confirmations = max(required_confirmations, REQUIRED_CONFIRMATIONS + 4)
                self._debug_update(
                    stage="Candidate distante inconnue",
                    candidate_known=False,
                    candidate_support=None,
                    decision="EN ATTENTE",
                    reason="Carte distante absente de la base locale — confirmation renforcée requise",
                    required_confirmations=required_confirmations,
                    last_cycle_ms=(time.monotonic() - now) * 1000.0,
                )

            # Known cardinal neighbours can be accepted immediately.
            if (FAST_ACCEPT_KNOWN_NEIGHBOR and distance == 1
                    and self.coordinate_validator is not None):
                try:
                    if self.coordinate_validator(x, y):
                        self.last_coordinates = coordinates
                        self.candidate_coordinates = None
                        self.candidate_count = 0
                        self.initializing = False
                        self.post_transition_reads = 2
                        self.last_read_success = True
                        self._debug_update(
                            stage="Voisin connu accepté",
                            candidate_known=True,
                            decision="ACCEPTÉ",
                            reason="Voisin de la carte courante présent dans la base",
                            last_cycle_ms=(time.monotonic() - now) * 1000.0,
                        )
                        result_coordinates = coordinates
                        result_changed = True
                        result_success = True
                        self._debug_detect_return(result_coordinates, result_changed, result_success, now)
                        return result_coordinates, result_changed, text
                except Exception as exc:
                    self._debug_update(
                        stage="Validation voisin en erreur",
                        reason=f"Erreur validation voisin : {type(exc).__name__}",
                    )

        # Startup: accept a known first map immediately.
        if self.initializing and self.last_coordinates is None:
            if candidate_known:
                self.last_coordinates = coordinates
                self.candidate_coordinates = None
                self.candidate_count = 0
                self.initializing = False
                self.last_read_success = True
                self._debug_update(
                    stage="Initialisation acceptée",
                    candidate_known=True,
                    decision="ACCEPTÉ",
                    reason="Première coordonnée connue",
                    last_cycle_ms=(time.monotonic() - now) * 1000.0,
                )
                result_coordinates = coordinates
                result_changed = True
                result_success = True
                self._debug_detect_return(result_coordinates, result_changed, result_success, now)
                return result_coordinates, result_changed, text

        # Same map: OCR is valid, but the overlay has no reason to redraw the map.
        if coordinates == self.last_coordinates:
            self.candidate_coordinates = None
            self.candidate_count = 0
            if self.last_coordinates is not None:
                self.initializing = False
            self.last_read_success = True
            self._debug_update(
                stage="Coordonnée inchangée",
                candidate_known=candidate_known,
                decision="CONSERVÉE",
                reason="Même carte confirmée",
                last_cycle_ms=(time.monotonic() - now) * 1000.0,
            )
            result_coordinates = self.last_coordinates
            result_changed = False
            result_success = True
            self._debug_detect_return(result_coordinates, result_changed, result_success, now)
            return result_coordinates, result_changed, text

        # New coordinate: require repeated confirmation unless it is a known
        # fast-accepted neighbour handled above.
        if self.candidate_coordinates != coordinates:
            self.candidate_coordinates = coordinates
            self.candidate_count = 1
            self.last_read_success = True
            self._debug_update(
                stage="Nouvelle candidate",
                candidate_known=candidate_known,
                decision="EN ATTENTE",
                reason="Première confirmation",
                candidate_count=1,
                required_confirmations=required_confirmations,
                last_cycle_ms=(time.monotonic() - now) * 1000.0,
            )
            result_coordinates = self.last_coordinates
            result_changed = False
            result_success = True
            self._debug_detect_return(result_coordinates, result_changed, result_success, now)
            return result_coordinates, result_changed, text

        self.candidate_count += 1
        self.last_read_success = True
        self._debug_update(
            candidate_known=candidate_known,
            candidate_count=self.candidate_count,
            required_confirmations=required_confirmations,
        )

        if self.candidate_count >= required_confirmations:
            self.last_coordinates = coordinates
            self.candidate_coordinates = None
            self.candidate_count = 0
            self.initializing = False
            self.post_transition_reads = 2
            self._debug_update(
                stage="Nouvelle carte confirmée",
                candidate_known=candidate_known,
                decision="ACCEPTÉ",
                reason="Nombre de confirmations atteint",
                candidate_count=required_confirmations,
                last_cycle_ms=(time.monotonic() - now) * 1000.0,
            )
            result_coordinates = coordinates
            result_changed = True
            result_success = True
            self._debug_detect_return(result_coordinates, result_changed, result_success, now)
            return result_coordinates, result_changed, text

        self._debug_update(
            stage="Confirmation en cours",
            candidate_known=candidate_known,
            decision="EN ATTENTE",
            reason=f"Confirmation {self.candidate_count}/{required_confirmations}",
            last_cycle_ms=(time.monotonic() - now) * 1000.0,
        )
        result_coordinates = self.last_coordinates
        result_changed = False
        result_success = True
        self._debug_detect_return(result_coordinates, result_changed, result_success, now)
        return result_coordinates, result_changed, text

    def close(self):
        # Close the mss instance belonging to the calling thread. The OCR
        # worker is daemonized and normally finishes before shutdown.
        try:
            sct = getattr(self._mss_local, "instance", None)
            if sct is not None:
                sct.close()
                self._mss_local.instance = None
        except Exception:
            pass

    def run_test(self):
        print(f"Tesseract : {self.tesseract_cmd}")
        print(f"Zone OCR : {self.region}")
        print("Détection des coordonnées — Ctrl+C pour arrêter.")
        try:
            while True:
                start = time.monotonic()
                coordinates, changed, text = self.detect()
                print(
                    ("[NOUVELLE CARTE] " if changed else "Carte actuelle : "),
                    coordinates if coordinates else "non détectée",
                    sep="",
                )
                if not coordinates and text:
                    print("OCR:", text.replace("\n", " | ")[:300])
                time.sleep(max(0, OCR_INTERVAL - (time.monotonic() - start)))
        finally:
            self.close()


if __name__ == "__main__":
    CoordinateDetector().run_test()
