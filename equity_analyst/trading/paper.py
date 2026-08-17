"""SQLite-backed paper-trading book.

Ported from `indian-stock-signal-ai`'s SQLModel implementation onto the engine's
own sqlite layer, so the book lives in the same database as the analysis and can
be queried alongside it.

**Paper only.** There is no broker adapter here and no code path that could
place a real order. Fills are simulated at the price you pass in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..db import utc_now


@dataclass
class Fill:
    ticker: str
    side: str
    qty: float
    price: float
    realized_pnl: float = 0.0
    strategy: str = ""


class PaperBook:
    """Cash, positions and orders against an :class:`~equity_analyst.db.Database`."""

    def __init__(self, db: Any, starting_cash: float):
        self.db = db
        self.starting_cash = starting_cash
        self._ensure_account()

    # -- account -----------------------------------------------------
    def _ensure_account(self) -> None:
        row = self.db.dicts("SELECT * FROM paper_account WHERE id = 1")
        if not row:
            self.db.insert("paper_account", {
                "id": 1,
                "cash": self.starting_cash,
                "starting_cash": self.starting_cash,
                "created_at": utc_now(),
            })

    @property
    def cash(self) -> float:
        return float(self.db.scalar("SELECT cash FROM paper_account WHERE id = 1") or 0.0)

    def _set_cash(self, value: float) -> None:
        self.db.conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (value,))
        self.db.conn.commit()

    # -- positions ---------------------------------------------------
    def positions(self) -> List[Dict[str, Any]]:
        return self.db.dicts("SELECT * FROM paper_position ORDER BY ticker")

    def position(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.db.dicts("SELECT * FROM paper_position WHERE ticker = ?", (ticker,))
        return rows[0] if rows else None

    def orders(self) -> List[Dict[str, Any]]:
        return self.db.dicts("SELECT * FROM paper_order ORDER BY id")

    # -- trading -----------------------------------------------------
    def place(
        self, ticker: str, side: str, qty: float, price: float, strategy: str = ""
    ) -> Fill:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must both be positive")
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        pos = self.position(ticker)
        realized = 0.0

        if side == "buy":
            cost = qty * price
            if cost > self.cash + 1e-6:
                raise ValueError(
                    f"insufficient cash: need {cost:,.2f}, have {self.cash:,.2f}"
                )
            self._set_cash(self.cash - cost)
            if pos:
                new_qty = pos["qty"] + qty
                self.db.conn.execute(
                    "UPDATE paper_position SET qty = ?, avg_price = ? WHERE ticker = ?",
                    (
                        new_qty,
                        (pos["qty"] * pos["avg_price"] + qty * price) / new_qty,
                        ticker,
                    ),
                )
            else:
                self.db.insert("paper_position", {
                    "ticker": ticker, "qty": qty, "avg_price": price,
                    "strategy": strategy, "opened_at": utc_now(),
                })
        else:
            if not pos or pos["qty"] <= 0:
                raise ValueError(f"no position in {ticker} to sell")
            qty = min(qty, pos["qty"])
            realized = (price - pos["avg_price"]) * qty
            self._set_cash(self.cash + qty * price)
            remaining = pos["qty"] - qty
            if remaining <= 1e-9:
                self.db.conn.execute("DELETE FROM paper_position WHERE ticker = ?", (ticker,))
            else:
                self.db.conn.execute(
                    "UPDATE paper_position SET qty = ? WHERE ticker = ?", (remaining, ticker)
                )

        self.db.conn.commit()
        self.db.insert("paper_order", {
            "ticker": ticker, "side": side, "qty": qty, "price": price,
            "strategy": strategy, "status": "filled",
            "realized_pnl": realized, "created_at": utc_now(),
        })
        return Fill(ticker, side, qty, price, round(realized, 2), strategy)


def portfolio_snapshot(book: PaperBook, marks: Dict[str, float]) -> Dict[str, Any]:
    """Value the book. ``marks`` maps ticker -> last price.

    A position with no mark is held at cost and flagged, rather than being
    dropped from the equity total or silently marked at zero.
    """
    rows, market_value, unrealised = [], 0.0, 0.0
    stale: List[str] = []

    for pos in book.positions():
        mark = marks.get(pos["ticker"])
        if mark is None:
            mark = pos["avg_price"]
            stale.append(pos["ticker"])
        value = pos["qty"] * mark
        pnl = (mark - pos["avg_price"]) * pos["qty"]
        market_value += value
        unrealised += pnl
        rows.append({
            "ticker": pos["ticker"],
            "qty": pos["qty"],
            "avg_price": round(pos["avg_price"], 2),
            "last": round(mark, 2),
            "value": round(value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pct": (
                round((mark / pos["avg_price"] - 1) * 100, 2) if pos["avg_price"] else 0.0
            ),
            "strategy": pos.get("strategy") or "",
            "marked_at_cost": pos["ticker"] in stale,
        })

    cash = book.cash
    equity = cash + market_value
    realized = sum(o.get("realized_pnl") or 0.0 for o in book.orders())
    return {
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "starting_cash": round(book.starting_cash, 2),
        "total_pnl": round(equity - book.starting_cash, 2),
        "total_pnl_pct": (
            round((equity / book.starting_cash - 1) * 100, 2) if book.starting_cash else 0.0
        ),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealised, 2),
        "positions": rows,
        "stale_marks": stale,
    }
