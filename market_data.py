"""토스 조회 데이터, 동적 유니버스, 확정봉 지표. 실거래 주문 기능 없음."""
from __future__ import annotations
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader_config import Settings
from trading_models import decimal, timestamp

ZONES = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}


def quote_is_fresh(quote: dict, now, max_age: int) -> bool:
    try:
        age = (timestamp(now) - timestamp(quote["timestamp"])).total_seconds()
        return -30 <= age <= max_age and decimal(quote["price"], positive=True) > 0
    except (KeyError, ValueError, TypeError):
        return False


def regular_session(calendar: dict, country: str, now):
    """UTC 비교로 DST·미국 조기폐장·휴장을 모두 캘린더에 위임한다."""
    today = (calendar or {}).get("today") or {}
    value = today.get("integrated", {}) if country == "KR" else today
    session = value.get("regularMarket")
    if not session:
        return None
    start, end = timestamp(session["startTime"]), timestamp(session["endTime"])
    return (start, end) if start <= timestamp(now) < end else None


def order_window(calendar: dict, country: str, now) -> bool:
    session = regular_session(calendar, country, now)
    # 미국 소수점 API 제약을 모의운용에도 동일하게 적용한다.
    return bool(session and timestamp(now) < session[1] - (timedelta(hours=1) if country == "US" else timedelta()))


def indicators(candles: list[dict], *, before_date: str) -> dict:
    """당일 진행봉을 제외하고 확정봉만 사용한다. 데이터 오류를 0으로 숨기지 않는다."""
    by_date = {}
    for c in candles:
        stamp = c["timestamp"]
        timestamp(stamp)
        if stamp[:10] >= before_date:
            continue
        row = {k: decimal(c[k], positive=k != "volume") for k in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")}
        if row["volume"] < 0 or not (row["lowPrice"] <= min(row["openPrice"], row["closePrice"]) <= max(row["openPrice"], row["closePrice"]) <= row["highPrice"]):
            raise ValueError("OHLCV 값이 올바르지 않습니다.")
        by_date[stamp[:10]] = {"timestamp": stamp, **row}
    rows = [by_date[k] for k in sorted(by_date)]
    if len(rows) < 60:
        raise ValueError("확정 일봉이 60개 미만입니다.")
    closes = [r["closePrice"] for r in rows]
    volumes = [r["volume"] for r in rows]
    sma = lambda n: sum(closes[-n:]) / n
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        gains.append(max(cur - prev, Decimal(0)))
        losses.append(max(prev - cur, Decimal(0)))
    gain, loss = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    for g, l in zip(gains[14:], losses[14:]):
        gain, loss = (gain * 13 + g) / 14, (loss * 13 + l) / 14
    rsi = Decimal(50) if gain == loss == 0 else Decimal(100) if loss == 0 else 100 - 100 / (1 + gain / loss)
    trs = [max(row["highPrice"] - row["lowPrice"], abs(row["highPrice"] - prev), abs(row["lowPrice"] - prev)) for prev, row in zip(closes, rows[1:])]
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    avg_volume = sum(volumes[-21:-1]) / 20
    return {"as_of": rows[-1]["timestamp"], "bars": len(rows), "last_close": closes[-1],
            "sma20": sma(20), "sma60": sma(60), "sma200": sma(200) if len(rows) >= 200 else None,
            "rsi14": rsi, "atr14": atr, "return5_pct": (closes[-1] / closes[-6] - 1) * 100,
            "return20_pct": (closes[-1] / closes[-21] - 1) * 100,
            "relative_volume": volumes[-1] / avg_volume if avg_volume else None,
            "support20": min(r["lowPrice"] for r in rows[-20:]),
            "resistance20": max(r["highPrice"] for r in rows[-20:]),
            "average_dollar_volume20": sum(r["volume"] * r["closePrice"] for r in rows[-20:]) / 20,
            "ohlcv": rows[-60:]}


class MarketData:
    def __init__(self, client, state, settings: Settings):
        self.client, self.state, self.settings = client, state, settings

    def calendar(self, country, now):
        day = timestamp(now).astimezone(ZONES[country]).date().isoformat()
        key = f"calendar:{country}:{day}"
        cached = self.state.get(key)
        if not cached:
            cached = self.client.get_market_calendar(country, day)
            # 잘못된 날짜나 누락 데이터는 휴장으로 조용히 처리하지 않는다.
            if not cached or (cached.get("today") or {}).get("date") != day:
                raise ValueError("시장 캘린더의 기준일이 요청일과 다릅니다.")
            self.state.set(key, cached)
        return cached

    def quotes(self, symbols: list[str], countries: dict[str, str]) -> dict[str, dict]:
        output = {}
        symbols = list(dict.fromkeys(symbols))
        for start in range(0, len(symbols), 200):
            for q in self.client.get_prices(symbols[start:start + 200]):
                symbol = q["symbol"]
                if symbol not in countries:
                    continue
                output[symbol] = {"symbol": symbol, "price": str(decimal(q["lastPrice"], positive=True)),
                                  "timestamp": q["timestamp"], "currency": q["currency"], "country": countries[symbol]}
        return output

    def fx(self) -> Decimal:
        result = self.client.get_exchange_rate("USD", "KRW")
        return decimal(result["rate"], positive=True)

    def screen(self, country: str, now, holdings: list[dict]) -> dict:
        day = timestamp(now).astimezone(ZONES[country]).date().isoformat()
        coverage = {"country": country, "scope": "NASDAQ active common shares" if country == "US" else "Samsung / SK hynix only", "errors": []}
        calendar = self.calendar(country, now)
        base = calendar["today"].get("integrated", {}) if country == "KR" else calendar["today"]
        regular = base.get("regularMarket")
        is_premarket = bool(regular and timestamp(now) < timestamp(regular["startTime"]))
        cutoff_day = timestamp(now).astimezone(ZONES[country]).date()
        if regular and timestamp(now) >= timestamp(regular["endTime"]):
            cutoff_day += timedelta(days=1)
        before = (datetime.combine(cutoff_day, time(), tzinfo=ZONES[country]) - timedelta(seconds=1)).isoformat()
        if country == "US":
            key = "universe:NASDAQ:" + day
            universe = self.state.get(key)
            if universe is None:
                universe = [x for x in self.client.get_all_stocks("NASDAQ") if x.get("securityType") in {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"} and x.get("isCommonShare") is True]
                self.state.set(key, universe)
            listed = {x["symbol"]: x for x in universe}
            ranked = []
            for kind in ("MARKET_TRADING_AMOUNT", "TOP_GAINERS", "MARKET_TRADING_VOLUME"):
                result = self.client.get_rankings("US", kind)
                ranked.extend(x["symbol"] for x in result.get("rankings", []) if x["symbol"] in listed)
            pool = list(dict.fromkeys(ranked))[:self.settings.screening_pool_size]
        else:
            listed = {s: {"symbol": s} for s in self.settings.kr_symbols}
            pool = list(listed)
        if not listed:
            raise ValueError("시장 종목 목록이 비어 있습니다.")
        # 전체 유니버스의 현재가를 일괄 확인하고, 유동성 랭킹 후보만 심층 분석한다.
        countries = dict.fromkeys(listed, country)
        held = [p["symbol"] for p in holdings if p["country"] == country]
        countries.update(dict.fromkeys(held, country))
        quotes = self.quotes(list(countries), countries)
        pool = list(dict.fromkeys(held + pool))
        coverage.update(universe_count=len(listed), quoted_count=len(quotes), candidate_count=len(pool))
        details = {}
        for start in range(0, len(pool), 200):
            details.update({x["symbol"]: x for x in self.client.get_stocks(pool[start:start + 200])})
        snapshots = []
        for symbol in pool:
            try:
                info = details[symbol]
                if info.get("status") != "ACTIVE" or (country == "US" and info.get("market") != "NASDAQ"):
                    continue
                technical = indicators(self.client.get_candles(symbol, "1d", 200, before=before)["candles"], before_date=cutoff_day.isoformat())
                quote = quotes.get(symbol)
                if not quote or not quote_is_fresh(quote, now, self.settings.quote_max_age_seconds):
                    if not is_premarket:
                        raise ValueError("현재가가 지연되었거나 시각이 잘못되었습니다.")
                    # 장전에는 직전 확정 일봉을 참고 자료로 쓴다. 체결 가능한 현재가가 아니다.
                    quote = {"symbol": symbol, "price": str(technical["last_close"]), "timestamp": technical["as_of"],
                             "currency": info["currency"], "country": country, "kind": "previous_close_reference"}
                else:
                    quote = {**quote, "kind": "current_quote"}
                if quote["currency"] != info["currency"] or quote["currency"] != ("USD" if country == "US" else "KRW"):
                    raise ValueError("종목 정보와 시세의 통화가 다릅니다.")
                # 선별 점수는 예측 확률이 아니다. 추세/유동성 순으로 검토량만 줄인다.
                score = (20 if technical["last_close"] > technical["sma20"] else 0) + (20 if technical["sma20"] > technical["sma60"] else 0)
                score += min(max(technical["return20_pct"], Decimal(-20)), Decimal(20))
                snapshots.append({"symbol": symbol, "name": info["name"], "country": country, "currency": info["currency"],
                                  "quote": quote, "technical": technical, "screen_score": score,
                                  "warnings": self.client.get_stock_warnings(symbol)})
            except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                coverage["errors"].append({"symbol": symbol, "error": type(exc).__name__})
        snapshots.sort(key=lambda s: (s["symbol"] in held, s["screen_score"], s["technical"]["average_dollar_volume20"]), reverse=True)
        coverage["chart_count"] = len(snapshots)
        selected = snapshots[:max(self.settings.shortlist_size, len(held))]
        coverage["shortlist_count"] = len(selected)
        coverage["previous_close_reference_count"] = sum(s["quote"]["kind"] == "previous_close_reference" for s in selected)
        return {"at": timestamp(now).isoformat(), "phase": "premarket" if is_premarket else "regular_or_aftermarket",
                "coverage": coverage, "candidates": selected}
