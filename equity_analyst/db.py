"""SQLite runtime for the §2 model.

Thin on purpose: it creates the schema, loads staged rows, records provenance,
and hands back ``sqlite3.Row`` results. All analytical arithmetic belongs in
``sql/schema.sql`` views or in explicit queries the report prints back out --
never hidden in a helper here.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "sql", "schema.sql")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Owns the connection and the staged loads."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            if os.path.exists(path):
                os.remove(path)  # a run always rebuilds from source
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- lifecycle -------------------------------------------------------
    def _create_schema(self) -> None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            self.schema_sql = fh.read()
        self.conn.executescript(self.schema_sql)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reads -----------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def dicts(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query(sql, params)]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # -- writes ----------------------------------------------------------
    def insert(self, table: str, row: Dict[str, Any]) -> None:
        self.insert_many(table, [row])

    def insert_many(self, table: str, rows: Iterable[Dict[str, Any]]) -> int:
        rows = [r for r in rows if r]
        if not rows:
            return 0
        # Union the keys so heterogeneous provider payloads still load; every
        # row is normalised to the same column list with NULLs for gaps.
        columns: List[str] = []
        for r in rows:
            for k in r:
                if k not in columns:
                    columns.append(k)
        valid = self._columns_of(table)
        columns = [c for c in columns if c in valid]
        if not columns:
            return 0
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        payload = [tuple(r.get(c) for c in columns) for r in rows]
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def _columns_of(self, table: str) -> set:
        return {r["name"] for r in self.query(f"PRAGMA table_info({table})")}

    # -- run bookkeeping -------------------------------------------------
    def start_run(
        self, run_id: str, ticker: str, market: str, horizon_years: int, version: str
    ) -> None:
        self.insert(
            "analysis_run",
            {
                "run_id": run_id,
                "ticker": ticker,
                "market": market,
                "horizon_years": horizon_years,
                "created_at": utc_now(),
                "engine_version": version,
            },
        )

    def record_assumptions(self, run_id: str, rows: List[Dict[str, Any]]) -> None:
        self.insert_many("assumption", [dict(r, run_id=run_id) for r in rows])

    def record_coverage(
        self,
        run_id: str,
        domain: str,
        field: str,
        present: bool,
        source: Optional[str] = None,
        source_tier: Optional[str] = None,
        as_of: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        self.insert(
            "data_coverage",
            {
                "run_id": run_id,
                "domain": domain,
                "field": field,
                "present": int(bool(present)),
                "source": source,
                "source_tier": source_tier,
                "as_of": as_of,
                "note": note,
            },
        )
