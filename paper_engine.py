"""정기/중요 뉴스 재분석과 모의 진입·목표 청산을 연결하는 운용 루프."""
from __future__ import annotations
from datetime import timedelta
from hashlib import sha256
import json
import time
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from market_data import ZONES, order_window, quote_is_fresh, regular_session
from openai_engine import shared_context
from trading_models import Action, decimal, timestamp, utcnow


class Runner:
    def __init__(self, settings, state, market, ai, clock=utcnow):
        self.settings, self.state, self.market, self.ai, self.clock = settings, state, market, ai, clock
        self.owner = str(uuid4())
        self.strategy_hash = sha256(json.dumps(settings.public_dict(), sort_keys=True).encode()).hexdigest()[:12]

    def analyze(self, country: str, reason: str, now, identity: str | None = None) -> bool:
        day = timestamp(now).astimezone(ZONES[country]).date().isoformat()
        run_id = identity or f"{country}:{day}:{reason}:{self.strategy_hash}"
        attempt_key = f"analysis_attempts:{country}:{day}"
        attempts = self.state.get(attempt_key, 0)
        if attempts >= self.settings.max_analysis_attempts_per_market_day:
            return False
        if not self.state.begin_run(run_id, country, day, reason, now):
            return False
        self.state.set(attempt_key, attempts + 1)
        try:
            packet = self.market.screen(country, now, self.state.positions())
            packet["analysis_reason"] = reason
            if not packet["candidates"]:
                raise ValueError("유효한 최신 시세·차트 후보가 없습니다.")
            plan, research = self.ai.analyze(packet, shared_context(self.settings, self.state))
            created = timestamp(self.clock())
            payload = {"created_at": created.isoformat(), "expires_at": (created + timedelta(minutes=self.settings.plan_valid_minutes)).isoformat(),
                       "plan": plan.to_dict(), "research": research, "market": packet,
                       "strategy": self.settings.public_dict(), "usage": list(self.ai.usage)}
            self.state.finish_run(run_id, payload)
            if reason in {"premarket", "scheduled"}:
                self.state.set(f"{reason}:{country}:{day}", self.strategy_hash)
            self.state.set("news_checked:" + country, created.isoformat())
            self.ai.usage.clear()
            self.state.set("entries_paused:" + country, False)
            self.state.event("analysis_complete", {"id": run_id, "country": country, "reason": reason, "comment": plan.market_comment}, created)
            return True
        except Exception as exc:
            # 원문 에러/비밀값 대신 오류 종류를 장부에 기록한다.
            self.state.fail_run(run_id, type(exc).__name__)
            self.state.set("entries_paused:" + country, True)
            self.state.event("analysis_failed", {"id": run_id, "error_type": type(exc).__name__}, self.clock())
            return False
        finally:
            if self.ai.usage:
                self.state.event("openai_usage", {"reason": reason, "requests": list(self.ai.usage)}, self.clock())
                self.ai.usage.clear()

    def _quotes(self, positions_or_actions: list[dict], country: str) -> dict:
        return self.market.quotes([p["symbol"] for p in positions_or_actions], {p["symbol"]: country for p in positions_or_actions})

    def monitor(self, country: str, calendar: dict, now) -> list[str]:
        positions = [p for p in self.state.positions() if p["country"] == country]
        if not positions:
            return []
        quotes = self._quotes(positions, country)
        fx = self.market.fx() if country == "US" else decimal(1)
        marks = self.state.get("marks", {})
        for symbol, quote in quotes.items():
            marks[symbol] = {**quote, "fx": str(fx)}
        self.state.set("marks", marks)
        if not order_window(calendar, country, now):
            return []
        sold = []
        for p in positions:
            quote = quotes.get(p["symbol"])
            if not quote or not quote_is_fresh(quote, now, self.settings.quote_max_age_seconds):
                continue
            if quote.get("currency") != p["currency"] or quote.get("country") != p["country"]:
                continue
            price = decimal(quote["price"])
            reason = "target" if price >= decimal(p["target_price"]) else "stop" if price <= decimal(p["stop_price"]) else None
            if reason and self.state.sell(p["symbol"], quote, fx, self.settings, now, reason):
                sold.append(p["symbol"])
                self.state.event("paper_exit", {"symbol": p["symbol"], "reason": reason}, now)
                if reason == "target":
                    self.state.set("rotation:" + country, p["id"])
        return sold

    def apply_pending(self, country: str, calendar: dict, now):
        if not order_window(calendar, country, now):
            return
        pending = self.state.latest_plan(country)
        if not pending or timestamp(pending["expires_at"]) <= timestamp(now):
            return
        if pending.get("strategy") != self.settings.public_dict():
            return
        if pending["market"].get("phase") == "premarket" or pending["market"].get("analysis_reason") == "premarket":
            return
        actions = [Action.parse(a) for a in pending["plan"]["actions"]]
        country_by_symbol = {x["symbol"]: x["country"] for x in pending["market"]["candidates"]}
        country_by_symbol.update({p["symbol"]: p["country"] for p in self.state.positions()})
        active = [a for a in actions if a.action in {"BUY", "SELL"} and country_by_symbol.get(a.symbol) == country]
        if not active:
            return
        quotes = self._quotes([a.to_dict() for a in active], country)
        fx = self.market.fx() if country == "US" else decimal(1)
        # 매도 후 매수를 처리하되 잔고/일일 한도/같은 종목 재진입은 장부가 다시 검증한다.
        for a in sorted(active, key=lambda a: a.action != "SELL"):
            quote = quotes.get(a.symbol)
            if not quote or quote["country"] != country or quote["currency"] != ("USD" if country == "US" else "KRW"):
                continue
            if not quote_is_fresh(quote, now, self.settings.quote_max_age_seconds):
                continue
            if a.action == "SELL":
                self.state.sell(a.symbol, quote, fx, self.settings, now, "AI thesis invalidation: " + a.invalidation)
            elif not self.state.get("entries_paused:" + country, False):
                day = timestamp(now).astimezone(ZONES[country]).date().isoformat()
                # 장전/뉴스 계획만으로 진입하지 않고 당일 개장 재확인을 기다린다.
                if self.state.get(f"scheduled:{country}:{day}") == self.strategy_hash:
                    self.state.buy(pending["id"], a, quote, fx, self.settings, now)

    def check_news(self, country: str, now) -> list[dict]:
        if not self.settings.enable_news:
            return []
        key = "news_checked:" + country
        previous = self.state.get(key)
        if previous and timestamp(now) - timestamp(previous) < timedelta(minutes=self.settings.news_interval_minutes):
            return []
        day = timestamp(now).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        budget_key = "news_checks:" + day
        checks = self.state.get(budget_key, 0)
        if checks >= self.settings.max_news_checks_per_day:
            return []
        # 요청 실패에도 재시도 폭주/비용 증가가 없도록 요청 전에 기록한다.
        self.state.set(key, timestamp(now).isoformat())
        self.state.set(budget_key, checks + 1)
        try:
            events = self.ai.news(timestamp(now).isoformat(), country, self.state.positions())
        finally:
            if self.ai.usage:
                self.state.event("openai_usage", {"reason": "news_check", "requests": list(self.ai.usage)}, self.clock())
                self.ai.usage.clear()
        fresh = []
        for event in events:
            try:
                published = timestamp(event["published_at"])
                if not timedelta(seconds=-60) <= timestamp(now) - published <= timedelta(hours=6):
                    continue
                parsed = urlsplit(event["url"])
                # 기사 id 같은 의미 있는 query는 보존하고 추적용 파라미터만 제거한다.
                query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                                         if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "msclkid"}))
                canonical = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))
                identity = sha256((canonical + "|" + published.date().isoformat()).encode()).hexdigest()
                if event["material"] is not True or self.state.news_seen(identity):
                    continue
                self.state.remember_news(identity, event, now)
                fresh.append(event)
            except (KeyError, TypeError, ValueError):
                continue
        if fresh:
            self.state.event("material_news", {"country": country, "events": fresh}, now)
        return fresh

    def tick(self, now=None) -> dict:
        now = timestamp(now or self.clock())
        if not self.state.acquire(self.owner, now):
            return {"status": "another_runner_active"}
        summary = {"status": "paper", "at": now.isoformat(), "analyses": [], "exits": [], "errors": []}
        try:
            self.state.set("heartbeat", now.isoformat())
            for country in self.settings.countries:
                try:
                    calendar = self.market.calendar(country, now)
                    summary["exits"].extend(self.monitor(country, calendar, now))
                    session = regular_session(calendar, country, now)
                    day = now.astimezone(ZONES[country]).date().isoformat()
                    analyzed = False
                    today = calendar.get("today") or {}
                    base = today.get("integrated", {}) if country == "KR" else today
                    regular = base.get("regularMarket")
                    if regular:
                        start = timestamp(regular["startTime"])
                        preparation = start - timedelta(minutes=self.settings.analysis_before_open_minutes)
                        if preparation <= now < start and self.state.get(f"premarket:{country}:{day}") != self.strategy_hash:
                            analyzed = self.analyze(country, "premarket", now)
                            if analyzed:
                                summary["analyses"].append(country + ":premarket")
                    if session and now >= session[0] + timedelta(minutes=self.settings.analysis_after_open_minutes):
                        if self.state.get(f"scheduled:{country}:{day}") != self.strategy_hash:
                            analyzed = self.analyze(country, "scheduled", now)
                            if analyzed:
                                summary["analyses"].append(country + ":scheduled")
                    rotation = self.state.get("rotation:" + country)
                    if rotation and analyzed:
                        self.state.set("rotation:" + country, None)
                    elif rotation and session and self.state.count_runs(country, day, "rotation") < self.settings.max_rotation_analyses_per_day:
                        if self.analyze(country, "rotation", now, "rotation:" + rotation):
                            self.state.set("rotation:" + country, None)
                            analyzed = True
                            summary["analyses"].append(country + ":rotation")
                    # 장전 준비 시점부터 마감 2시간 후까지 중요 뉴스를 확인한다.
                    news_window = regular and timestamp(regular["startTime"]) - timedelta(minutes=self.settings.analysis_before_open_minutes) <= now <= timestamp(regular["endTime"]) + timedelta(hours=2)
                    # 정기 분석에서 이미 최신 웹 뉴스를 조사했으면 같은 tick에 중복 조회하지 않는다.
                    if news_window and not analyzed:
                        events = self.check_news(country, now)
                        if events and self.state.count_runs(country, day, "news") < self.settings.max_news_analyses_per_day:
                            identity = "news:" + country + ":" + sha256(dumps_for_hash(events).encode()).hexdigest()[:24]
                            if self.analyze(country, "news", now, identity):
                                summary["analyses"].append(country + ":news")
                    self.apply_pending(country, calendar, self.clock())
                except Exception as exc:
                    summary["errors"].append({"country": country, "error_type": type(exc).__name__})
                    self.state.set("entries_paused:" + country, True)
                    self.state.event("tick_error", summary["errors"][-1], self.clock())
            self.state.set("last_tick", summary)
            return summary
        finally:
            self.state.release(self.owner)

    def run(self):
        while True:
            print(json.dumps(self.tick(), ensure_ascii=False), flush=True)
            time.sleep(self.settings.poll_seconds)


def dumps_for_hash(events):
    return json.dumps(sorted(e["url"] for e in events), ensure_ascii=False)
