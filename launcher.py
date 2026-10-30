import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "startup_error.log"

os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    from overlay import ResourceOverlay
    ResourceOverlay().run()
except BaseException:
    error = traceback.format_exc()
    try:
        LOG.write_text(error, encoding="utf-8")
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Dofus Resource Assistant n'a pas pu démarrer.\n\n"
                "Le détail est disponible dans startup_error.log.",
                "Dofus Resource Assistant",
                0x10,
            )
        except Exception:
            pass

    # Do not re-raise under pythonw.exe: an exception window is otherwise
    # invisible and the process simply disappears.
    sys.exit(1)
