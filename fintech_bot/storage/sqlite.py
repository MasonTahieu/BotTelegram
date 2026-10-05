"""Single-process SQLite persistence for the local prototype."""

import json
import sqlite3
from dataclasses import fields
from datetime import date, datetime, timezone
from pathlib import Path

from fintech_bot.domain import Candle, FinancialSnapshot, Instrument, Signal, SignalSide


class SqliteRepository:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                event_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                session TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                source TEXT NOT NULL,
                is_demo INTEGER NOT NULL,
                side TEXT NOT NULL,
                reason TEXT NOT NULL,
                indicators_json TEXT NOT NULL,
                evaluated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                PRIMARY KEY (chat_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                event_key TEXT NOT NULL REFERENCES signals(event_key),
                chat_id TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY (event_key, chat_id)
            );
            CREATE TABLE IF NOT EXISTS instruments (
                symbol TEXT PRIMARY KEY, exchange TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candles (
                source TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                closed_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (source, symbol, timeframe, closed_at)
            );
            CREATE TABLE IF NOT EXISTS financials (
                source TEXT NOT NULL, symbol TEXT NOT NULL, period TEXT NOT NULL,
                published_on TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (source, symbol, period, published_on)
            );
            CREATE TABLE IF NOT EXISTS preferences (
                chat_id TEXT PRIMARY KEY, mode TEXT NOT NULL, timeframe TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT NOT NULL REFERENCES signals(event_key), chat_id TEXT NOT NULL,
                next_try REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
                PRIMARY KEY (event_key, chat_id)
            );
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, next_try);
        """)
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(signals)")}
        for column, definition in {
            "closed_at": "TEXT", "timeframe": "TEXT NOT NULL DEFAULT '1d'",
            "confirmed": "INTEGER NOT NULL DEFAULT 1", "exchange": "TEXT NOT NULL DEFAULT ''",
            "observed_at": "TEXT",
        }.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE signals ADD COLUMN {column} {definition}")
        preference_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(preferences)")}
        for column, definition in {"early_alerts": "INTEGER NOT NULL DEFAULT 1",
                                   "financial_filter": "TEXT NOT NULL DEFAULT '{}'"}.items():
            if column not in preference_columns:
                self.connection.execute(f"ALTER TABLE preferences ADD COLUMN {column} {definition}")
        # Runtime is daily-only. Migrate preferences in place so existing
        # subscriptions and per-user alert/filter settings remain untouched.
        self.connection.execute("UPDATE preferences SET timeframe='1d' WHERE timeframe!='1d'")
        self.connection.execute("""UPDATE outbox SET status='cancelled' WHERE status='pending'
            AND event_key IN (SELECT event_key FROM signals WHERE timeframe!='1d')""")
        self.connection.execute("CREATE TABLE IF NOT EXISTS runtime_state (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.commit()
        self.connection.execute("""CREATE INDEX IF NOT EXISTS signals_lookup
            ON signals(source, symbol, timeframe, strategy_id, session DESC, closed_at DESC, evaluated_at DESC)""")

    def close(self) -> None:
        self.connection.close()

    def save_signal(self, signal: Signal) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT INTO signals (event_key, symbol, session, strategy_id, source, is_demo,
                    side, reason, indicators_json, evaluated_at, closed_at, timeframe, confirmed, exchange, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    source = excluded.source,
                    reason = excluded.reason,
                    indicators_json = excluded.indicators_json,
                    evaluated_at = excluded.evaluated_at,
                    observed_at = excluded.observed_at
            """, (
                signal.key, signal.symbol, signal.session.isoformat(), signal.strategy_id,
                signal.source, int(signal.is_demo), signal.side.value, signal.reason,
                json.dumps(signal.indicators, allow_nan=False),
                datetime.now(timezone.utc).isoformat(),
                signal.closed_at.isoformat() if signal.closed_at else None,
                signal.timeframe, int(signal.confirmed), signal.exchange,
                signal.observed_at.isoformat() if signal.observed_at else None,
            ))

    def latest_signal(self, symbol: str, strategy_id: str, source: str, timeframe: str = "1d") -> Signal | None:
        row = self.connection.execute("""
            SELECT * FROM signals WHERE symbol = ? AND strategy_id = ? AND source = ? AND timeframe = ?
            ORDER BY session DESC, closed_at DESC, evaluated_at DESC LIMIT 1
        """, (symbol, strategy_id, source, timeframe)).fetchone()
        if row is None:
            return None
        return self._read_signal(row)

    def latest_action_signal(self, symbol, strategy_id, first_session, last_session, *, is_demo):
        row = self.connection.execute("""
            WITH revisions AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY session, COALESCE(closed_at, ''), confirmed
                    ORDER BY evaluated_at DESC, rowid DESC
                ) AS revision FROM signals
                WHERE symbol=? AND strategy_id=? AND timeframe='1d' AND is_demo=?
                    AND confirmed=1 AND session BETWEEN ? AND ?
            )
            SELECT * FROM revisions WHERE revision=1 AND side IN ('BUY', 'SELL')
            ORDER BY session DESC, closed_at DESC LIMIT 1
        """, (symbol, strategy_id, int(is_demo), first_session.isoformat(),
              last_session.isoformat())).fetchone()
        return self._read_signal(row) if row else None

    @staticmethod
    def _read_signal(row) -> Signal:
        return Signal(
            symbol=row["symbol"], session=date.fromisoformat(row["session"]),
            side=SignalSide(row["side"]), strategy_id=row["strategy_id"],
            reason=row["reason"], indicators=json.loads(row["indicators_json"]),
            source=row["source"], is_demo=bool(row["is_demo"]),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            timeframe=row["timeframe"], confirmed=bool(row["confirmed"]), exchange=row["exchange"],
            observed_at=datetime.fromisoformat(row["observed_at"]) if row["observed_at"] else None,
        )

    def signal_by_key(self, key: str) -> Signal:
        return self._read_signal(self.connection.execute("SELECT * FROM signals WHERE event_key = ?", (key,)).fetchone())

    def signals_on(self, day: date, strategy_id: str, source: str | None = None,
                   timeframe: str = "1d", *, is_demo: bool):
        # A correction to the SAME bar can retract a signal. A later bar must not
        # erase an earlier confirmed event. Exclude previews from the daily ledger.
        source_clause = " AND source=?" if source is not None else ""
        parameters = [day.isoformat(), strategy_id, timeframe, int(is_demo)]
        if source is not None:
            parameters.append(source)
        rows = self.connection.execute(f"""
            WITH revisions AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY symbol, session, COALESCE(closed_at, ''), confirmed
                    ORDER BY evaluated_at DESC, rowid DESC
                ) AS revision FROM signals
                WHERE session=? AND strategy_id=? AND timeframe=? AND is_demo=? AND confirmed=1
                    {source_clause}
            )
            SELECT * FROM revisions WHERE revision=1 AND side IN ('BUY', 'SELL')
            ORDER BY closed_at DESC, symbol
        """, parameters)
        return [self._read_signal(row) for row in rows]

    @staticmethod
    def _json(value) -> str:
        # These models contain immutable scalar fields; avoid deepcopy of every datetime.
        payload = {field.name: getattr(value, field.name) for field in fields(value)}
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=lambda item: item.isoformat())

    def save_instruments(self, instruments: list[Instrument]) -> None:
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO instruments VALUES (?, ?, ?)",
                                        [(item.symbol, item.exchange, self._json(item)) for item in instruments])

    def save_candles(self, source: str, candles: list[Candle]) -> None:
        with self.connection:
            self.connection.executemany("""
                INSERT INTO candles VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, timeframe, closed_at) DO UPDATE SET payload=excluded.payload
                WHERE candles.payload != excluded.payload
            """, [(source, item.symbol, item.timeframe, item.timestamp.isoformat(), self._json(item)) for item in candles])

    def load_candles(self, source: str, symbol: str, timeframe: str) -> list[Candle]:
        result = []
        for row in self.connection.execute("""SELECT payload FROM candles
                WHERE source=? AND symbol=? AND timeframe=? ORDER BY closed_at""", (source, symbol, timeframe)):
            data = json.loads(row[0])
            data["session"] = date.fromisoformat(data["session"])
            for field in ("closed_at", "observed_at"):
                data[field] = datetime.fromisoformat(data[field]) if data[field] else None
            result.append(Candle(**data))
        return result

    def save_financials(self, source: str, snapshot: FinancialSnapshot) -> None:
        with self.connection:
            self.connection.execute("""INSERT INTO financials VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, period, published_on) DO UPDATE SET payload=excluded.payload
                WHERE financials.payload != excluded.payload""",
                                    (source, snapshot.symbol, snapshot.period, snapshot.published_on.isoformat(), self._json(snapshot)))

    def preference(self, chat_id: str, default_mode="watchlist", default_timeframe="1d") -> tuple[str, str]:
        row = self.connection.execute("SELECT mode, timeframe FROM preferences WHERE chat_id=?", (chat_id,)).fetchone()
        return (row["mode"], "1d") if row else (default_mode, "1d")

    def set_preference(self, chat_id: str, mode: str, timeframe: str) -> None:
        if mode not in {"all", "watchlist", "off"} or timeframe != "1d":
            raise ValueError("Tùy chọn thông báo không hợp lệ.")
        with self.connection:
            self.connection.execute("""INSERT INTO preferences(chat_id,mode,timeframe) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode,timeframe=excluded.timeframe""",
                (chat_id, mode, timeframe))

    def early_alerts(self, chat_id):
        row = self.connection.execute("SELECT early_alerts FROM preferences WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row[0]) if row else True

    def set_early_alerts(self, chat_id, enabled):
        with self.connection:
            self.connection.execute("UPDATE preferences SET early_alerts=? WHERE chat_id=?", (int(enabled), chat_id))

    def financial_filter(self, chat_id):
        row = self.connection.execute("SELECT financial_filter FROM preferences WHERE chat_id=?", (chat_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_financial_filter(self, chat_id, rules):
        with self.connection:
            self.connection.execute("UPDATE preferences SET financial_filter=? WHERE chat_id=?",
                                    (json.dumps(rules, allow_nan=False), chat_id))

    def get_state(self, name, default=None):
        row = self.connection.execute("SELECT value FROM runtime_state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, name, value):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO runtime_state VALUES (?,?)", (name, json.dumps(value)))

    def recipients(self, symbol: str, timeframe: str) -> list[str]:
        if timeframe != "1d":
            return []
        return [row[0] for row in self.connection.execute("""
            SELECT p.chat_id FROM preferences p WHERE p.timeframe=? AND
            (p.mode='all' OR (p.mode='watchlist' AND EXISTS
                (SELECT 1 FROM subscriptions s WHERE s.chat_id=p.chat_id AND s.symbol=?)))
        """, (timeframe, symbol))]

    def enqueue(self, signal: Signal, chat_id: str, now: float, expires_at: float) -> None:
        if self.was_delivered(signal.key, chat_id) or self.was_logical_delivered(signal, chat_id):
            return
        with self.connection:
            self.connection.execute("""INSERT INTO outbox(event_key,chat_id,next_try,expires_at) VALUES (?,?,?,?)
                ON CONFLICT(event_key,chat_id) DO UPDATE SET status='pending',next_try=excluded.next_try,
                    expires_at=excluded.expires_at
                WHERE (outbox.status IN ('invalidated', 'cancelled') AND outbox.expires_at>=excluded.next_try)
                    OR (outbox.status='expired' AND excluded.expires_at>outbox.expires_at
                        AND excluded.expires_at>=excluded.next_try)""",
                (signal.key, chat_id, now, expires_at))

    def pending(self, now: float, limit: int):
        with self.connection:
            self.connection.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at < ?", (now,))
        return self.connection.execute("""SELECT * FROM outbox WHERE status='pending' AND next_try<=?
            ORDER BY next_try, rowid LIMIT ?""", (now, limit)).fetchall()

    def update_outbox(self, key, chat_id, status, next_try=0, error=None) -> None:
        with self.connection:
            attempted = int(status == "sent" or error is not None)
            self.connection.execute("""UPDATE outbox SET status=?,next_try=?,last_error=?,attempts=attempts+?
                WHERE event_key=? AND chat_id=?""", (status, next_try, error, attempted, key, chat_id))

    def pending_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]

    def cancel_superseded(self, signal: Signal) -> None:
        with self.connection:
            self.connection.execute("""UPDATE outbox SET status='superseded' WHERE status='pending'
                AND event_key IN (SELECT event_key FROM signals WHERE symbol=? AND timeframe=?
                    AND strategy_id=? AND event_key!=?)""",
                (signal.symbol, signal.timeframe, signal.strategy_id, signal.key))

    def invalidate_pending(self, source, symbol, timeframe) -> None:
        with self.connection:
            self.connection.execute("""UPDATE outbox SET status='invalidated' WHERE status='pending'
                AND event_key IN (SELECT event_key FROM signals WHERE source=? AND symbol=? AND timeframe=?)""",
                (source, symbol, timeframe))

    def invalidate_pending_for_pair(self, symbol, timeframe, strategy_id=None) -> None:
        condition = " AND strategy_id=?" if strategy_id is not None else ""
        values = [symbol, timeframe]
        if strategy_id is not None:
            values.append(strategy_id)
        with self.connection:
            self.connection.execute(f"""UPDATE outbox SET status='invalidated' WHERE status='pending'
                AND event_key IN (SELECT event_key FROM signals WHERE symbol=? AND timeframe=?{condition})""",
                values)

    def subscribe(self, chat_id: str, symbol: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?, ?)", (chat_id, symbol),
            )
        return cursor.rowcount == 1

    def unsubscribe(self, chat_id: str, symbol: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM subscriptions WHERE chat_id = ? AND symbol = ?", (chat_id, symbol),
            )
            self.connection.execute("""UPDATE outbox SET status='cancelled' WHERE chat_id=? AND status='pending'
                AND event_key IN (SELECT event_key FROM signals WHERE symbol=?)""", (chat_id, symbol))
        return cursor.rowcount == 1

    def subscriptions(self, chat_id: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT symbol FROM subscriptions WHERE chat_id = ? ORDER BY symbol", (chat_id,),
        )]

    def subscribers(self, symbol: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT chat_id FROM subscriptions WHERE symbol = ? ORDER BY chat_id", (symbol,),
        )]

    def was_delivered(self, event_key: str, chat_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE event_key = ? AND chat_id = ?", (event_key, chat_id),
        ).fetchone() is not None

    def was_logical_delivered(self, signal: Signal, chat_id: str) -> bool:
        closed_at = signal.closed_at.isoformat() if signal.closed_at else None
        return self.connection.execute("""SELECT 1 FROM deliveries d
            JOIN signals s ON s.event_key=d.event_key
            WHERE d.chat_id=? AND s.is_demo=? AND s.strategy_id=? AND s.symbol=?
                AND s.timeframe=? AND s.session=? AND COALESCE(s.closed_at,'')=COALESCE(?, '')
                AND s.confirmed=? AND s.side=? LIMIT 1""", (
            chat_id, int(signal.is_demo), signal.strategy_id, signal.symbol, signal.timeframe,
            signal.session.isoformat(), closed_at, int(signal.confirmed), signal.side.value,
        )).fetchone() is not None

    def mark_delivered(self, event_key: str, chat_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (event_key, chat_id, datetime.now(timezone.utc).isoformat()),
            )
