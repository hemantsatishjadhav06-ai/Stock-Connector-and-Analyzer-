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
WAREHOUSE_PATH = os.path.join(os.path.dirname(__file__), "sql", "warehouse.sql")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Owns the connection and the staged loads."""

    def __init__(
        self,
        path: str = ":memory:",
        reset: bool = True,
        warehouse: bool = False,
        timeout: float = 30.0,
        cross_thread: bool = False,
    ):
        """Open the SQL layer.

        ``reset=True`` (the default for a one-shot CLI run) rebuilds the
        per-company tables from source, so a run can never quietly inherit a
        stale figure. ``warehouse=True`` additionally applies ``warehouse.sql``
        -- the company index, alias and cache tables that must SURVIVE runs --
        and forces ``reset=False``, because deleting the file would throw away
        every other company in the warehouse.
        """
        self.path = path
        self.warehouse = warehouse
        if warehouse:
            reset = False
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            if reset and os.path.exists(path):
                os.remove(path)
        # A timeout matters once a web request and the refresh scheduler can
        # touch the same file: without it a concurrent write fails instantly
        # instead of waiting for the lock.
        #
        # cross_thread lifts sqlite3's same-thread guard. It is ONLY safe
        # because the web layer serialises every touch of this connection
        # behind a single lock (see web/server.py, Site.lock) -- a threaded
        # HTTP server hands each request to a different thread, so without the
        # lock this flag would trade a clear error for silent corruption.
        self.conn = sqlite3.connect(
            path, timeout=timeout, check_same_thread=not cross_thread
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # WAL lets the server keep reading while a refresh writes.
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA busy_timeout = 30000")
        self._create_schema()

    # -- lifecycle -------------------------------------------------------
    def _create_schema(self) -> None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            self.schema_sql = fh.read()
        self.conn.executescript(self.schema_sql)
        if self.warehouse:
            with open(WAREHOUSE_PATH, "r", encoding="utf-8") as fh:
                warehouse_sql = fh.read()
            self.conn.executescript(warehouse_sql)
            self.schema_sql = self.schema_sql + "\n" + warehouse_sql
        self.conn.commit()

    def clear_company(self, ticker: str) -> None:
        """Drop one company's staged rows without touching the warehouse.

        A refresh must replace a company's data, not append to it: without
        this, a restated fiscal year would leave the superseded row in place
        and both would flow into the analysis.
        """
        for table in (
            "income_statement", "balance_sheet", "cash_flow", "price_daily",
            "shareholding", "ratios_reported", "news_item", "market_snapshot",
        ):
            self.conn.execute(f"DELETE FROM {table} WHERE ticker = ?", (ticker,))
        self.conn.execute("DELETE FROM company WHERE ticker = ?", (ticker,))
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
