"""완전 오프라인 합성 시나리오. 실제 시세/실적 또는 투자 성과가 아니다."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from trader_config import Settings
from trading_models import Action, Plan
from trading_state import State
from paper_engine import Runner


def demo():
    now = datetime(2026, 10, 29, 14, 5, tzinfo=timezone.utc)
    settings = replace(Settings(), countries=("US",), enable_news=False)
    with TemporaryDirectory(prefix="fomo-paper-demo-") as folder:
        state = State(Path(folder) / "demo.sqlite3", settings.initial_capital_krw)

        class DemoMarket:
            price = "100"

            def calendar(self, country, at):
                return {"today": {"date": "2026-10-29", "regularMarket": {"startTime": "2026-10-29T13:30:00+00:00", "endTime": "2026-10-29T20:00:00+00:00"}}}

            def quotes(self, symbols, countries):
                return {s: {"symbol": s, "price": self.price, "timestamp": now.isoformat(), "country": "US", "currency": "USD"} for s in symbols}

            def fx(self):
                from decimal import Decimal
                return Decimal("1300")

            def screen(self, country, at, holdings):
                return {"at": at.isoformat(), "coverage": {"scope": "SYNTHETIC DEMO ONLY", "universe_count": 1},
                        "candidates": [{"symbol": "DEMO", "name": "합성 종목", "country": "US", "currency": "USD", "quote": self.quotes(["DEMO"], {})["DEMO"], "technical": {}}]}

        class DemoAI:
            usage = []

            def analyze(self, packet, context):
                buying = not context["portfolio"]["recent_fills"]
                action = Action.parse({"symbol": "DEMO", "action": "BUY" if buying else "WATCH", "entry_low": "99", "entry_high": "101",
                                       "stop_price": "95", "target_price": "110", "allocation_krw": "400000",
                                       "thesis": "오프라인 배선 검증용 합성 판단", "invalidation": "합성 가격 95 이하",
                                       "source_urls": []})
                return Plan("합성 데이터: 실제 수익성 검증이 아닙니다.", [action]), {"text": "DEMO", "sources": []}

        market = DemoMarket()
        runner = Runner(settings, state, market, DemoAI(), clock=lambda: now)
        first = runner.tick(now)
        now += timedelta(minutes=1)
        market.price = "111"
        second = runner.tick(now)
        overview = state.overview()
        assert not overview["positions"]
        assert [f["side"] for f in overview["recent_fills"]] == ["SELL", "BUY"]
        return {"fixture_only": True, "network_calls": 0, "real_orders": 0, "first_tick": first, "second_tick": second, "ledger": overview}
