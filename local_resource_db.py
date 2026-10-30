"""Read-only local, map-precise resource database.

The database is generated once from the supplied dofus-sqlite maps.sqlite plus
DofusDB's resource catalogue. After preparation the overlay performs no network
requests and only reads SQLite.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "resource_map.sqlite"

class LocalResourceDB:
    def __init__(self, path=None):
        self.path = Path(path or DB_PATH)
        self.conn = None
        self._open()

    def _open(self):
        if self.path.is_file():
            c = None
            try:
                # The packaged dataset is immutable at runtime. Opening it in
                # SQLite read-only mode prevents accidental writes and avoids
                # creating journal/WAL files beside the bundled database.
                uri = self.path.resolve().as_uri() + "?mode=ro"
                c = sqlite3.connect(uri, uri=True, check_same_thread=False)
                c.row_factory = sqlite3.Row
                # Validate the expected schema before declaring the DB ready.
                c.execute("SELECT 1 FROM maps LIMIT 1")
                c.execute("SELECT 1 FROM resources LIMIT 1")
                c.execute("SELECT 1 FROM map_resources LIMIT 1")
                self.conn = c
            except Exception:
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass

    def status_text(self):
        if not self.conn:
            return "Base locale absente — la base incluse est introuvable"
        try:
            nmap = self.conn.execute("SELECT COUNT(*) FROM maps").fetchone()[0]
            nres = self.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
            nlinks = self.conn.execute("SELECT COUNT(*) FROM map_resources").fetchone()[0]
            return f"Base locale : {nmap} cartes • {nres} ressources • {nlinks} emplacements"
        except Exception:
            return "Base locale invalide — la base incluse est invalide"

    def ready(self):
        return self.conn is not None

    def has_map(self, x, y):
        if not self.conn:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM maps WHERE x=? AND y=? LIMIT 1",
            (int(x), int(y)),
        ).fetchone()
        return row is not None

    def catalog_for_ui(self):
        if not self.conn:
            return []
        return [dict(r) for r in self.conn.execute(
            """SELECT id, name, category, level
               FROM resources
               ORDER BY CASE category
                   WHEN 'Alchimiste' THEN 1
                   WHEN 'Bûcheron' THEN 2
                   WHEN 'Mineur' THEN 3
                   WHEN 'Paysan' THEN 4
                   WHEN 'Pêcheur' THEN 5
                   WHEN 'Divers' THEN 6
                   ELSE 99 END,
                   level, name"""
        )]

    def _selected_clause(self, selected_ids):
        if selected_ids is None:
            return "", []
        vals = [str(x) for x in selected_ids]
        if not vals:
            return " AND 1=0", []
        return " AND r.id IN (" + ",".join("?" for _ in vals) + ")", vals

    def resources_for_map(self, x, y, min_level=0, max_level=None, selected_ids=None):
        if not self.conn:
            return []
        extra, extra_params = self._selected_clause(selected_ids)
        sql = """SELECT m.x, m.y, r.id resource_id, r.name, r.category, r.level,
                         COALESCE(SUM(mr.quantity), COUNT(DISTINCT mr.cell_id)) count
                  FROM maps m
                  JOIN map_resources mr ON mr.map_id=m.id
                  JOIN resources r ON r.id=mr.resource_id
                  WHERE m.x=? AND m.y=? AND r.level>=?"""
        params = [int(x), int(y), int(min_level)]
        if max_level is not None:
            sql += " AND r.level<=?"
            params.append(int(max_level))
        sql += extra
        sql += " GROUP BY r.id,r.name,r.category,r.level ORDER BY r.level, r.name"
        rows = self.conn.execute(sql, params + extra_params).fetchall()
        return [dict(r, direction="ici", approximate=False) for r in rows]

    def resources_by_resource_distance(self, x, y, resource_distances, selected_ids=None):
        """Search maps reachable by real cardinal movement only.

        Dofus movement is cardinal: one map north/south/east/west costs one
        map step. Therefore a map diagonally offset by (1, 1) is two steps
        away, not one. Results are consequently filtered using Manhattan
        distance and grouped into the direction of the dominant cardinal axis.
        A diagonal result is never displayed as a one-step diagonal neighbour.
        """
        if not self.conn or not resource_distances:
            return []
        selected = [str(v) for v in (selected_ids if selected_ids is not None else resource_distances.keys())]
        if not selected:
            return []
        x, y = int(x), int(y)
        max_distance = min(5, max(0, max(int(resource_distances.get(rid, 1)) for rid in selected)))
        if max_distance < 0:
            return []

        placeholders = ','.join('?' for _ in selected)
        min_x, max_x = x - max_distance, x + max_distance
        min_y, max_y = y - max_distance, y + max_distance
        rows = self.conn.execute(
            f"""SELECT m.x, m.y, r.id resource_id, r.name, r.category, r.level,
                         COALESCE(SUM(mr.quantity), COUNT(DISTINCT mr.cell_id)) count
                  FROM maps m
                  JOIN map_resources mr ON mr.map_id=m.id
                  JOIN resources r ON r.id=mr.resource_id
                  WHERE m.x BETWEEN ? AND ? AND m.y BETWEEN ? AND ?
                    AND r.id IN ({placeholders})
                  GROUP BY m.x,m.y,r.id,r.name,r.category,r.level""",
            [min_x, max_x, min_y, max_y] + selected,
        ).fetchall()

        out = []
        for row in rows:
            dx, dy = int(row['x']) - x, int(row['y']) - y
            # Reachability follows the actual movement graph: each cardinal
            # map change costs one step, so distance is Manhattan distance.
            distance = abs(dx) + abs(dy)
            if distance > int(resource_distances.get(str(row['resource_id']), 1)):
                continue

            if dx == 0 and dy == 0:
                direction = 'ici'
            elif dx == 0:
                direction = '↓' if dy > 0 else '↑'
            elif dy == 0:
                direction = '→' if dx > 0 else '←'
            elif dx > 0 and dy < 0:
                direction = '↗'
            elif dx > 0 and dy > 0:
                direction = '↘'
            elif dx < 0 and dy > 0:
                direction = '↙'
            else:
                direction = '↖'

            out.append({
                'x': row['x'], 'y': row['y'],
                'resource_id': row['resource_id'], 'name': row['name'],
                'category': row['category'], 'level': row['level'],
                'count': row['count'], 'direction': direction,
                'distance': distance, 'priority': distance > 1,
            })
        out.sort(key=lambda r: (r['distance'], r['level'], r['name']))
        return out

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn=None
