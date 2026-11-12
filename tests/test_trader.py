"""실계좌/유료 API를 사용하지 않는 의미 있는 운용 회귀 검증."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO

from trader_config import Settings
from trading_models import Action, Plan, decimal
from trading_state import State
from market_data import indicators, quote_is_fresh, order_window, regular_session, MarketData
from openai_engine import OpenAIEngine
from paper_engine import Runner
from toss_client import TossApiError, TossClient

NOW = datetime(2026, 10, 29, 14, 5, tzinfo=timezone.utc)


def action(symbol="AAA", **overrides):
    return Action.parse({"symbol": symbol, "action": "BUY", "entry_low": "99", "entry_high": "101", "stop_price": "95", "target_price": "110",
                         "allocation_krw": "400000", "thesis": "검증용 근거", "invalidation": "지지선 이탈", "source_urls": ["https://example.test/ir"], **overrides})


def quote(symbol="AAA", at=NOW, price="100", country="US"):
    return {"symbol": symbol, "price": price, "timestamp": at.isoformat(), "country": country, "currency": "USD" if country == "US" else "KRW"}


def calendar(start="2026-10-29T13:30:00+00:00", end="2026-10-29T20:00:00+00:00"):
    return {"today": {"date": "2026-10-29", "regularMarket": {"startTime": start, "endTime": end}}}


def candles(count=80, trend=Decimal("0.1")):
    rows = []
    for i in range(count):
        price = Decimal(100) + trend * i
        at = NOW.date() - timedelta(days=count - i)
        rows.append({"timestamp": f"{at.isoformat()}T00:00:00-04:00", "openPrice": str(price), "closePrice": str(price),
                     "lowPrice": str(price - 1), "highPrice": str(price + 1), "volume": "1000", "currency": "USD"})
    return list(reversed(rows))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(Settings(), countries=("US",), enable_news=False)
        self.state = State(Path(self.tmp.name) / "paper.sqlite3", self.settings.initial_capital_krw)

    def buy(self, identity="run1", symbol="AAA", **kwargs):
        return self.state.buy(identity, action(symbol, **kwargs), quote(symbol), Decimal("1300"), self.settings, NOW)


class ValidationTests(unittest.TestCase):
    def test_nonfinite_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", True, "oops"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal(value)

    def test_price_ordering(self):
        with self.assertRaises(ValueError):
            action(target_price="98")

    def test_unknown_symbol_and_duplicate_rejected(self):
        for actions in ([action("ZZZ").to_dict()], [action().to_dict(), action().to_dict()]):
            with self.assertRaises(ValueError):
                Plan.parse({"market_comment": "test", "actions": actions}, {"AAA"})

    def test_live_mode_rejected(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text('{"mode":"live"}')
            with self.assertRaises(ValueError):
                Settings.load(p)

    def test_korean_scope(self):
        with self.assertRaises(ValueError):
            replace(Settings(), kr_symbols=("035420",))

    def test_premarket_lead_time_positive(self):
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(Settings(), analysis_before_open_minutes=value)

    def test_env_diagnostics_distinguish_toss_and_openai_without_exposing_keys(self):
        from advisor import doctor, load_environment
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("TOSS_CLIENT_ID=TEST_ID_VALUE\nTOSS_CLIENT_SECRET=TEST_SECRET_VALUE\n", encoding="utf-8")
            with patch("advisor.ROOT", root), patch.dict("os.environ", {}, clear=True):
                load_environment()
                stream = StringIO()
                with redirect_stdout(stream):
                    self.assertFalse(doctor(replace(Settings(), ai_auth_mode="api_key", model="gpt-5.5")))
                result = json.loads(stream.getvalue())
                self.assertTrue(result["credentials_present"]["TOSS_CLIENT_ID"])
                self.assertTrue(result["credentials_present"]["TOSS_CLIENT_SECRET"])
                self.assertFalse(result["credentials_present"]["OPENAI_API_KEY"])
                self.assertNotIn("TEST_SECRET_VALUE", stream.getvalue())
                self.assertNotIn("TEST_ID_VALUE", stream.getvalue())


class LedgerTests(Base):
    def test_budget_fee_reserve_and_persistence(self):
        self.assertTrue(self.buy())
        reopened = State(self.state.path, Decimal("999999"))
        result = reopened.overview()
        self.assertEqual(result["initial_capital_krw"], "500000")
        self.assertGreaterEqual(decimal(result["cash_krw"]), Decimal("100000"))
        self.assertLessEqual(decimal(result["positions"][0]["cost_krw"]), Decimal("400000"))
        self.assertGreater(decimal(result["recent_fills"][0]["fee_krw"]), 0)

    def test_duplicate_and_concurrent_entry(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.buy(), range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.state.overview()["recent_fills"]), 1)

    def test_daily_limit_survives_restart(self):
        self.settings = replace(self.settings, max_entries_per_day=1)
        self.assertTrue(self.buy())
        self.state.sell("AAA", quote(price="111"), Decimal("1300"), self.settings, NOW, "target")
        self.state = State(self.state.path, Decimal("500000"))
        self.assertFalse(self.buy("run2", "BBB"))

    def test_no_same_symbol_reentry_and_exit_once(self):
        self.buy()
        self.assertTrue(self.state.sell("AAA", quote(price="111"), Decimal("1300"), self.settings, NOW, "target"))
        self.assertFalse(self.state.sell("AAA", quote(price="111"), Decimal("1300"), self.settings, NOW, "target"))
        self.assertFalse(self.buy("new-run"))

    def test_slippage_outside_entry_does_not_fill(self):
        self.assertFalse(self.buy(entry_high="100.05"))

    def test_kr_whole_shares(self):
        a = action("005930", entry_low="90000", entry_high="110000", stop_price="85000", target_price="120000")
        self.state.buy("kr", a, quote("005930", price="100000", country="KR"), Decimal(1), self.settings, NOW)
        self.assertEqual(decimal(self.state.positions()[0]["quantity"]) % 1, 0)

    def test_single_writer_lease(self):
        self.assertTrue(self.state.acquire("first", NOW))
        self.assertFalse(self.state.acquire("second", NOW))
        self.state.release("second")
        self.assertFalse(self.state.acquire("second", NOW))
        self.state.release("first")
        self.assertTrue(self.state.acquire("second", NOW))

    def test_wrong_currency_or_symbol_cannot_change_ledger(self):
        self.assertFalse(self.state.buy("bad", action(), {**quote(), "currency": "KRW"}, Decimal("1300"), self.settings, NOW))
        self.assertFalse(self.state.buy("bad", action(), quote("BBB"), Decimal("1300"), self.settings, NOW))
        self.buy()
        self.assertFalse(self.state.sell("AAA", {**quote(), "currency": "KRW"}, Decimal("1300"), self.settings, NOW, "bad"))
        self.assertEqual(len(self.state.positions()), 1)

    def test_dynamic_market_discovery_and_coverage(self):
        client = Mock()
        client.get_market_calendar.return_value = calendar()
        client.get_all_stocks.return_value = [{"symbol": s, "securityType": "STOCK", "isCommonShare": True} for s in ("AAA", "BBB", "CCC")]
        client.get_rankings.side_effect = lambda country, kind: {"rankings": [{"symbol": "BBB"}, {"symbol": "NYSE_ONLY"}]}
        client.get_prices.side_effect = lambda symbols: [{"symbol": s, "lastPrice": "100", "timestamp": NOW.isoformat(), "currency": "USD"} for s in symbols]
        client.get_stocks.side_effect = lambda symbols: [{"symbol": s, "name": s, "market": "NASDAQ", "status": "ACTIVE", "currency": "USD"} for s in symbols]
        client.get_candles.return_value = {"candles": candles()}
        client.get_stock_warnings.return_value = []
        market = MarketData(client, self.state, self.settings)
        result = market.screen("US", NOW, [])
        self.assertEqual(result["coverage"]["universe_count"], 3)
        self.assertEqual(result["coverage"]["quoted_count"], 3)
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["BBB"])
        self.assertIn("before", client.get_candles.call_args.kwargs)
        market.screen("US", NOW, [])
        self.assertEqual(client.get_all_stocks.call_count, 1)

    def test_premarket_old_quote_is_labeled_reference_not_current(self):
        client = Mock()
        client.get_market_calendar.return_value = calendar()
        client.get_all_stocks.return_value = [{"symbol": "AAA", "securityType": "STOCK", "isCommonShare": True}]
        client.get_rankings.return_value = {"rankings": [{"symbol": "AAA"}]}
        client.get_stocks.return_value = [{"symbol": "AAA", "name": "A", "market": "NASDAQ", "status": "ACTIVE", "currency": "USD"}]
        client.get_prices.return_value = [{"symbol": "AAA", "lastPrice": "100", "timestamp": (NOW - timedelta(days=1)).isoformat(), "currency": "USD"}]
        client.get_candles.return_value = {"candles": candles()}
        client.get_stock_warnings.return_value = []
        market = MarketData(client, self.state, self.settings)
        before_open = NOW.replace(hour=12, minute=30)
        result = market.screen("US", before_open, [])
        self.assertEqual(result["phase"], "premarket")
        self.assertEqual(result["coverage"]["previous_close_reference_count"], 1)
        self.assertEqual(result["candidates"][0]["quote"]["kind"], "previous_close_reference")
        self.assertEqual(result["candidates"][0]["quote"]["price"], str(result["candidates"][0]["technical"]["last_close"]))
        self.assertEqual(market.screen("US", NOW, [])["candidates"], [])


class MarketTests(unittest.TestCase):
    def test_freshness_future_and_naive_timestamp(self):
        self.assertTrue(quote_is_fresh(quote(), NOW, 300))
        self.assertFalse(quote_is_fresh(quote(at=NOW - timedelta(minutes=6)), NOW, 300))
        self.assertFalse(quote_is_fresh(quote(at=NOW + timedelta(minutes=2)), NOW, 300))
        self.assertFalse(quote_is_fresh({**quote(), "timestamp": "2026-10-29T14:00:00"}, NOW, 300))

    def test_holiday_and_fractional_cutoff(self):
        self.assertIsNone(regular_session({"today": {"regularMarket": None}}, "US", NOW))
        self.assertTrue(order_window(calendar(), "US", NOW))
        self.assertFalse(order_window(calendar(), "US", NOW.replace(hour=19, minute=0)))

    def test_dst_and_early_close_use_calendar(self):
        winter = calendar("2026-11-27T14:30:00+00:00", "2026-11-27T18:00:00+00:00")
        at = datetime(2026, 11, 27, 16, 59, tzinfo=timezone.utc)
        self.assertTrue(order_window(winter, "US", at))
        self.assertFalse(order_window(winter, "US", at + timedelta(minutes=1)))

    def test_indicators_exclude_unfinished_bar(self):
        bars = candles()
        expected = indicators(bars, before_date="2026-10-29")
        current = {**bars[0], "timestamp": "2026-10-29T00:00:00-04:00", "closePrice": "999999"}
        self.assertEqual(indicators([current] + bars, before_date="2026-10-29"), expected)
        self.assertEqual(expected["rsi14"], 100)
        self.assertEqual(expected["atr14"], 2)
        self.assertEqual(expected["sma20"], sum(Decimal(100) + Decimal("0.1") * i for i in range(60, 80)) / 20)

    def test_flat_rsi_and_bad_data(self):
        self.assertEqual(indicators(candles(trend=Decimal(0)), before_date="2026-10-29")["rsi14"], 50)
        bars = candles()
        bars[0]["volume"] = "NaN"
        with self.assertRaises(ValueError):
            indicators(bars, before_date="2026-10-29")

    def test_broker_mutation_is_blocked_before_network(self):
        client = TossClient("dummy", "dummy")
        client._session = Mock()
        with self.assertRaises(TossApiError):
            client.create_order(1, {"symbol": "AAA"})
        with self.assertRaises(TossApiError):
            client.cancel_order(1, "dummy")
        client._session.post.assert_not_called()


class OpenAITests(unittest.TestCase):
    def response(self, text, status="completed"):
        return {"status": status, "id": "dummy", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    def test_missing_key_never_requests(self):
        http = Mock()
        ai = OpenAIEngine(Settings(), session=http, api_key="")
        with self.assertRaises(RuntimeError):
            ai._request("test", {})
        http.post.assert_not_called()

    def test_responses_contract_and_no_remote_storage(self):
        http = Mock()
        http.post.return_value.status_code = 200
        http.post.return_value.json.return_value = self.response("ok")
        ai = OpenAIEngine(Settings(), session=http, api_key="DUMMY")
        ai._request("test", {}, search=True)
        body = http.post.call_args.kwargs["json"]
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"], [{"type": "web_search"}])

    def test_incomplete_and_refusal(self):
        http = Mock()
        http.post.return_value.status_code = 200
        http.post.return_value.json.return_value = self.response("{}", "incomplete")
        with self.assertRaises(RuntimeError):
            OpenAIEngine(Settings(), session=http, api_key="DUMMY")._request("test", {})
        with self.assertRaises(RuntimeError):
            OpenAIEngine.text({"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]})

    def test_fabricated_source_rejected(self):
        ai = OpenAIEngine(Settings(), api_key="DUMMY")
        ai.research = Mock(return_value={"text": "facts", "sources": [{"url": "https://example.test/verified"}]})
        ai._request = Mock(return_value=self.response(json.dumps({"market_comment": "test", "actions": [action().to_dict()]})))
        with self.assertRaises(ValueError):
            ai.analyze({"at": NOW.isoformat(), "candidates": [{"symbol": "AAA", "name": "A"}]}, {"portfolio": {"positions": []}})


class RunnerTests(Base):
    def runner(self):
        market = Mock()
        market.calendar.return_value = calendar()
        market.fx.return_value = Decimal("1300")
        market.quotes.side_effect = lambda symbols, countries: {s: quote(s) for s in symbols}
        market.screen.return_value = {"at": NOW.isoformat(), "coverage": {}, "candidates": [{"symbol": "AAA", "name": "A", "country": "US"}]}
        ai = Mock()
        ai.usage = []
        ai.analyze.return_value = (Plan("test", [action()]), {"text": "facts", "sources": []})
        return Runner(self.settings, self.state, market, ai, clock=lambda: NOW)

    def test_scheduled_analysis_once_across_restarts(self):
        runner = self.runner()
        runner.tick(NOW)
        runner.tick(NOW)
        runner2 = self.runner()
        runner2.tick(NOW)
        self.assertEqual(runner.ai.analyze.call_count, 1)
        runner2.ai.analyze.assert_not_called()
        self.assertEqual(len(self.state.overview()["recent_fills"]), 1)

    def test_premarket_once_then_open_confirmation_at_ten_minutes(self):
        runner = self.runner()
        current = NOW.replace(hour=12, minute=30)
        runner.clock = lambda: current
        runner.market.quotes.side_effect = lambda symbols, countries: {s: quote(s, at=current) for s in symbols}
        first = runner.tick(current)
        self.assertEqual(first["analyses"], ["US:premarket"])
        self.assertFalse(self.state.positions())
        restarted = self.runner()
        restarted.clock = lambda: current
        restarted.tick(current)
        restarted.ai.analyze.assert_not_called()
        current = NOW.replace(hour=13, minute=35)
        self.assertEqual(runner.tick(current)["analyses"], [])
        self.assertFalse(self.state.positions())
        current += timedelta(minutes=5)
        confirmed = runner.tick(current)
        self.assertEqual(confirmed["analyses"], ["US:scheduled"])
        self.assertEqual(runner.ai.analyze.call_count, 2)
        self.assertEqual(len(self.state.positions()), 1)

    def test_premarket_does_not_run_early_or_on_holiday(self):
        runner = self.runner()
        before_window = NOW.replace(hour=12, minute=29)
        runner.clock = lambda: before_window
        self.assertEqual(runner.tick(before_window)["analyses"], [])
        runner.ai.analyze.assert_not_called()
        runner.market.calendar.return_value = {"today": {"date": "2026-10-29", "regularMarket": None}}
        runner.tick(NOW)
        runner.ai.analyze.assert_not_called()

    def test_premarket_plan_never_executes_at_open_without_fresh_plan(self):
        runner = self.runner()
        before_open = NOW.replace(hour=12, minute=30)
        runner.clock = lambda: before_open
        runner.analyze("US", "premarket", before_open)
        # 장전 모의 매수안은 개장 뒤에도 그대로 실행되면 안 된다.
        self.state.set("scheduled:US:2026-10-29", runner.strategy_hash)
        runner.apply_pending("US", calendar(), NOW.replace(hour=13, minute=40))
        self.assertFalse(self.state.positions())

    def test_premarket_uses_winter_calendar_instead_of_fixed_hour(self):
        runner = self.runner()
        at = datetime(2026, 11, 27, 13, 30, tzinfo=timezone.utc)
        runner.clock = lambda: at
        runner.market.calendar.return_value = calendar("2026-11-27T14:30:00+00:00", "2026-11-27T18:00:00+00:00")
        self.assertEqual(runner.tick(at)["analyses"], ["US:premarket"])

    def test_old_quote_and_expired_plan_do_not_fill(self):
        runner = self.runner()
        self.assertTrue(runner.analyze("US", "scheduled", NOW))
        runner.market.quotes.side_effect = lambda symbols, countries: {s: quote(s, at=NOW - timedelta(hours=1)) for s in symbols}
        runner.apply_pending("US", calendar(), NOW)
        self.assertFalse(self.state.positions())
        runner.market.quotes.side_effect = lambda symbols, countries: {s: quote(s, at=NOW + timedelta(hours=2)) for s in symbols}
        runner.apply_pending("US", calendar(), NOW + timedelta(hours=2))
        self.assertFalse(self.state.positions())

    def test_failure_pauses_entries_but_position_monitor_survives(self):
        runner = self.runner()
        self.buy()
        runner.ai.analyze.side_effect = ValueError("bad output")
        self.assertFalse(runner.analyze("US", "news", NOW))
        self.assertTrue(self.state.get("entries_paused:US"))
        runner.market.quotes.side_effect = lambda symbols, countries: {s: quote(s, price="90") for s in symbols}
        self.assertEqual(runner.monitor("US", calendar(), NOW), ["AAA"])

    def test_news_deduplication_freshness_and_check_budget(self):
        runner = self.runner()
        runner.settings = replace(self.settings, enable_news=True, news_interval_minutes=1, max_news_checks_per_day=2)
        event = {"title": "new earnings", "url": "https://example.test/earnings?utm_source=1", "published_at": NOW.isoformat(), "symbols": ["AAA"], "material": True, "reason": "guidance"}
        runner.ai.news.return_value = [event, {**event, "url": "https://example.test/old", "published_at": (NOW - timedelta(days=2)).isoformat()}]
        self.assertEqual(len(runner.check_news("US", NOW)), 1)
        runner.ai.news.return_value = [{**event, "url": "https://example.test/earnings?utm_source=2"}]
        self.assertEqual(runner.check_news("US", NOW + timedelta(minutes=2)), [])
        self.assertEqual(runner.check_news("US", NOW + timedelta(minutes=4)), [])
        self.assertEqual(runner.ai.news.call_count, 2)

    def test_offline_end_to_end_demo(self):
        from demo_trader import demo
        result = demo()
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["first_tick"]["analyses"], ["US:scheduled"])
        self.assertEqual(result["second_tick"]["exits"], ["DEMO"])
        self.assertEqual(result["second_tick"]["analyses"], ["US:rotation"])

    def test_analysis_failure_retry_budget(self):
        runner = self.runner()
        runner.settings = replace(self.settings, max_analysis_attempts_per_market_day=1)
        runner.ai.analyze.side_effect = RuntimeError("simulated API failure")
        self.assertFalse(runner.analyze("US", "scheduled", NOW))
        self.assertFalse(runner.analyze("US", "scheduled", NOW + timedelta(minutes=6)))
        self.assertEqual(runner.ai.analyze.call_count, 1)

    def test_changed_rules_do_not_execute_previous_plan(self):
        runner = self.runner()
        runner.analyze("US", "scheduled", NOW)
        runner.settings = replace(self.settings, max_position_krw=Decimal("200000"))
        runner.apply_pending("US", calendar(), NOW)
        self.assertFalse(self.state.positions())


if __name__ == "__main__":
    unittest.main()
