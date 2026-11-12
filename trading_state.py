"""여러 프로세스/AI가 공유하는 모의 장부. 실계좌 정보는 저장하지 않는다."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from trader_config import Settings
from trading_models import Action, decimal, dumps, timestamp


class State:
    def __init__(self, path: Path, initial_capital: Decimal):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, at TEXT NOT NULL,
                    day TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, country TEXT NOT NULL,
                    day TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('cash', ?)", (str(initial_capital),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('initial_capital', ?)", (str(initial_capital),))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def get(self, key: str, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def event(self, kind: str, payload: dict, now):
        with self.connection(write=True) as db:
            db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)", (timestamp(now).isoformat(), kind, dumps(payload)))

    def acquire(self, owner: str, now, seconds: int = 1800) -> bool:
        with self.connection(write=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='runner_lease'").fetchone()
            lease = json.loads(row[0]) if row else {}
            if lease and lease["owner"] != owner and timestamp(lease["until"]) > timestamp(now):
                return False
            value = {"owner": owner, "until": (timestamp(now) + timedelta(seconds=seconds)).isoformat()}
            db.execute("INSERT OR REPLACE INTO meta VALUES('runner_lease',?)", (dumps(value),))
            return True

    def release(self, owner: str):
        with self.connection(write=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='runner_lease'").fetchone()
            if row and json.loads(row[0])["owner"] == owner:
                db.execute("DELETE FROM meta WHERE key='runner_lease'")

    def positions(self) -> list[dict]:
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM positions ORDER BY symbol")]

    def overview(self) -> dict:
        # 장부와 현금을 같은 read transaction에서 읽어 일관된 공유 컨텍스트를 만든다.
        with self.connection() as db:
            db.execute("BEGIN")
            values = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('cash','initial_capital')"))
            positions = [json.loads(r[0]) for r in db.execute("SELECT payload FROM positions ORDER BY symbol")]
            fills = [json.loads(r[0]) for r in db.execute("SELECT payload FROM fills ORDER BY rowid DESC LIMIT 30")]
            runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 10")]
            for row in runs:
                row["payload"] = json.loads(row["payload"]) if row["payload"] else None
            events = [dict(r) for r in db.execute("SELECT at,kind,payload FROM events ORDER BY id DESC LIMIT 20")]
            db.commit()
        return {"mode": "paper", "cash_krw": values["cash"], "initial_capital_krw": values["initial_capital"],
                "positions": positions, "recent_fills": fills, "recent_runs": runs, "events": events}

    def count_runs(self, country: str, day: str, reason: str) -> int:
        with self.connection() as db:
            return db.execute("SELECT count(*) FROM runs WHERE country=? AND day=? AND reason=? AND status='complete'", (country, day, reason)).fetchone()[0]

    def begin_run(self, run_id: str, country: str, day: str, reason: str, now) -> bool:
        with self.connection(write=True) as db:
            old = db.execute("SELECT status,at FROM runs WHERE id=?", (run_id,)).fetchone()
            if old and (old["status"] == "complete" or timestamp(now) - timestamp(old["at"]) < timedelta(minutes=5)):
                return False
            db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,'running',NULL,NULL)", (run_id, country, day, reason, timestamp(now).isoformat()))
            return True

    def finish_run(self, run_id: str, payload: dict):
        with self.connection(write=True) as db:
            db.execute("UPDATE runs SET status='complete',payload=?,error=NULL WHERE id=?", (dumps(payload), run_id))

    def fail_run(self, run_id: str, error: str):
        with self.connection(write=True) as db:
            db.execute("UPDATE runs SET status='failed',error=? WHERE id=?", (error, run_id))

    def latest_plan(self, country: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT id,payload,at FROM runs WHERE country=? AND status='complete' ORDER BY at DESC,rowid DESC LIMIT 1", (country,)).fetchone()
        return {"id": row["id"], **json.loads(row["payload"])} if row else None

    def news_seen(self, identity: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM news WHERE id=?", (identity,)).fetchone() is not None

    def remember_news(self, identity: str, item: dict, now):
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO news VALUES(?,?,?)", (identity, timestamp(now).isoformat(), dumps(item)))

    def buy(self, run_id: str, action: Action, quote: dict, fx: Decimal, settings: Settings, now) -> bool:
        """잔고·중복·한도 확인과 모의 체결을 한 transaction으로 실행한다."""
        day = timestamp(now).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        country = quote["country"]
        if action.action != "BUY" or quote.get("symbol") != action.symbol or country not in {"US", "KR"}:
            return False
        if quote.get("currency") != ("USD" if country == "US" else "KRW"):
            return False
        if country == "KR" and action.symbol not in settings.kr_symbols:
            return False
        price = decimal(quote["price"], positive=True) * (1 + settings.slippage_bps / 10000)
        # 슬리피지 적용 후에도 진입 범위를 벗어난 체결은 만들지 않는다.
        if not action.entry_low <= price <= action.entry_high:
            return False
        fx = decimal(fx, positive=True)
        fill_id = f"{run_id}:BUY:{action.symbol}"
        fee_rate = settings.fee_bps / 10000
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM fills WHERE id=?", (fill_id,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM positions WHERE symbol=?", (action.symbol,)).fetchone():
                return False
            recent = db.execute("SELECT at FROM fills WHERE symbol=? AND side='SELL' ORDER BY rowid DESC LIMIT 1", (action.symbol,)).fetchone()
            if recent and timestamp(now) - timestamp(recent[0]) < timedelta(hours=24):
                return False
            if db.execute("SELECT count(*) FROM positions").fetchone()[0] >= settings.max_positions:
                return False
            if db.execute("SELECT count(*) FROM fills WHERE side='BUY' AND day=?", (day,)).fetchone()[0] >= settings.max_entries_per_day:
                return False
            cash = decimal(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            budget = min(action.allocation_krw, settings.max_position_krw, cash - settings.cash_reserve_krw)
            if budget <= 0:
                return False
            quantum = Decimal("1") if country == "KR" else Decimal("0.000001")
            quantity = (budget / (price * fx * (1 + fee_rate))).quantize(quantum, rounding=ROUND_DOWN)
            if quantity <= 0:
                return False
            gross = quantity * price * fx
            fee = gross * fee_rate
            cost = gross + fee
            position = {"id": fill_id, "symbol": action.symbol, "country": country, "currency": quote["currency"],
                        "quantity": str(quantity), "entry_price": str(price), "entry_fx": str(fx), "cost_krw": str(cost),
                        "stop_price": str(action.stop_price), "target_price": str(action.target_price),
                        "opened_at": timestamp(now).isoformat(), "thesis": action.thesis, "invalidation": action.invalidation}
            fill = {"id": fill_id, "at": timestamp(now).isoformat(), "side": "BUY", "symbol": action.symbol,
                    "quantity": str(quantity), "price": str(price), "fx": str(fx), "fee_krw": str(fee), "cash_change_krw": str(-cost), "reason": action.thesis}
            db.execute("INSERT INTO positions VALUES(?,?)", (action.symbol, dumps(position)))
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?)", (fill_id, fill["at"], day, action.symbol, "BUY", dumps(fill)))
            db.execute("UPDATE meta SET value=? WHERE key='cash'", (str(cash - cost),))
            return True

    def sell(self, symbol: str, quote: dict, fx: Decimal, settings: Settings, now, reason: str) -> bool:
        day = timestamp(now).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        price = decimal(quote["price"], positive=True) * (1 - settings.slippage_bps / 10000)
        fx = decimal(fx, positive=True)
        with self.connection(write=True) as db:
            row = db.execute("SELECT payload FROM positions WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                return False
            position = json.loads(row[0])
            if quote.get("symbol") != symbol or quote.get("currency") != position["currency"] or quote.get("country") != position["country"]:
                return False
            quantity = decimal(position["quantity"], positive=True)
            gross = quantity * price * fx
            fee = gross * settings.fee_bps / 10000
            proceeds = gross - fee
            fill_id = position["id"] + ":SELL"
            fill = {"id": fill_id, "at": timestamp(now).isoformat(), "side": "SELL", "symbol": symbol,
                    "quantity": str(quantity), "price": str(price), "fx": str(fx), "fee_krw": str(fee),
                    "cash_change_krw": str(proceeds), "realized_pnl_krw": str(proceeds - decimal(position["cost_krw"])), "reason": reason}
            cash = decimal(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?)", (fill_id, fill["at"], day, symbol, "SELL", dumps(fill)))
            db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            db.execute("UPDATE meta SET value=? WHERE key='cash'", (str(cash + proceeds),))
            return True
