import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "data" / "dofus.db"
SCHEMA = PROJECT_ROOT / "schema.sql"


class ResourceDatabase:
    """Small persistent store for user interface settings.

    Map/resource data is provided by LocalResourceDB from the bundled
    read-only dataset. This database intentionally stores only mutable user
    preferences so the two data sources cannot get out of sync.
    """

    def __init__(self, filename=DEFAULT_DB):
        self.filename = Path(filename)
        if not self.filename.is_absolute():
            self.filename = PROJECT_ROOT / self.filename
        self.filename.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(str(self.filename))
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self):
        self.connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.connection.commit()

    def get_setting(self, key, default=None):
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else row["value"]

    def set_setting(self, key, value, commit=True):
        self.connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )
        if commit:
            self.connection.commit()

    def set_settings(self, values):
        """Persist several settings in one transaction."""
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                [(str(k), str(v)) for k, v in values.items()],
            )

    def close(self):
        self.connection.close()
