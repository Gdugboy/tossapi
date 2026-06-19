"""AI 분석 엔진 — Claude Opus 4.8 로 워치리스트를 평가해 추천을 만든다.

토스 API 로 종목별 시세/캔들/유의사항을 모아 압축 요약한 뒤, Claude 에게
구조화 출력(JSON 스키마)으로 랭킹된 추천을 받는다. 뉴스 데이터는 아직
연동 전이라, 판단 근거는 가격·거래량·지표·유의사항에 한정된다.

주의: LLM 은 주가를 예측하지 못한다. 근거 있는 의견일 뿐, 수익을 보장하지
않는다. 최종 매수/매도 방아쇠는 사람이 당긴다.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from config import Settings
from toss_client import TossApiError, TossClient


# ── 구조화 출력 스키마 ──────────────────────────────────────────
class Recommendation(BaseModel):
    symbol: str
    name: str
    action: Literal["BUY", "WATCH", "AVOID"] = Field(
        description="BUY=지금 매수 후보, WATCH=관망, AVOID=피함"
    )
    conviction: int = Field(description="확신도 1(약)~10(강)", ge=1, le=10)
    style: Literal["SCALP", "SWING", "HOLD"] = Field(
        description="SCALP=단타로 먹고 빠짐, SWING=며칠, HOLD=중기(몇 주~달)"
    )
    entry_zone: str = Field(description="권장 진입 가격대 (예: '71500~72000')")
    take_profit_pct: float = Field(description="권장 익절 %")
    stop_loss_pct: float = Field(description="권장 손절 %")
    thesis: str = Field(description="이 판단의 핵심 근거 (한국어, 2~4문장)")
    risks: str = Field(description="주의/리스크 (한국어, 1~2문장)")


class Analysis(BaseModel):
    market_comment: str = Field(description="전체 시장/흐름 한줄평 (한국어)")
    recommendations: list[Recommendation]


SYSTEM = """\
너는 공격적인 단기 트레이딩을 보조하는 한국어 AI 애널리스트다. 소액으로
변동성 큰 종목을 단타~스윙으로 매매하려는 사용자를 돕는다. 수익성을 최우선으로
보되, 정직해야 한다.

원칙:
- 주가는 예측 불가능하다. 너는 '예언'이 아니라 제공된 데이터(가격·거래량·캔들·
  유의사항)에 근거한 확률적 의견을 낸다. 모르면 모른다고, WATCH/AVOID 로 표시하라.
- 각 종목을 SCALP(단타로 먹고 빠질 자리) / SWING / HOLD(중기 보유로 더 갈 자리)로
  명확히 구분하고, 그 이유를 thesis 에 쓴다.
- 익절/손절 %는 사용자의 기본 가드레일을 참고하되 종목 변동성에 맞춰 조정하라.
- 거래정지·VI·투자경고 등 유의사항이 있으면 conviction 을 낮추고 risks 에 명시하라.
- 뉴스/실적 데이터는 제공되지 않는다. 차트·가격 흐름만으로 판단하고, 그 한계를
  thesis 나 risks 에 솔직히 적어라. 없는 뉴스를 지어내지 마라.
- conviction 이 높은 순으로 정렬해서 반환하라."""


def _d(v) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def build_snapshot(client: TossClient, symbol: str) -> dict | None:
    """종목 하나의 분석용 압축 스냅샷. 조회 실패 시 None."""
    try:
        stocks = client.get_stocks([symbol])
        info = stocks[0] if stocks else {}
        prices = client.get_prices([symbol])
        last = prices[0]["lastPrice"] if prices else None

        daily = client.get_candles(symbol, "1d", 30).get("candles", [])
        minute = client.get_candles(symbol, "1m", 30).get("candles", [])
        warnings = client.get_stock_warnings(symbol)
    except TossApiError as e:
        return {"symbol": symbol, "error": str(e)}

    def closes(candles):
        # API는 최신순. 오래된→최신 순으로 뒤집어 흐름을 읽기 쉽게.
        return [c["closePrice"] for c in reversed(candles)]

    def vols(candles):
        return [c["volume"] for c in reversed(candles)]

    daily_close = closes(daily)
    pct_5d = None
    if len(daily_close) >= 6 and _d(daily_close[-6]) != 0:
        pct_5d = float(
            (_d(daily_close[-1]) - _d(daily_close[-6])) / _d(daily_close[-6]) * 100
        )

    return {
        "symbol": symbol,
        "name": info.get("name", symbol),
        "market": info.get("market"),
        "currency": info.get("currency"),
        "status": info.get("status"),
        "lastPrice": last,
        "change_5d_pct": round(pct_5d, 2) if pct_5d is not None else None,
        "daily_closes": daily_close[-15:],
        "daily_volumes": vols(daily)[-15:],
        "minute_closes": closes(minute)[-15:],
        "minute_volumes": vols(minute)[-15:],
        "warnings": [w.get("warningType") for w in warnings],
    }


def analyze(client: TossClient, settings: Settings) -> Analysis:
    """워치리스트를 분석해 랭킹된 추천을 반환."""
    snapshots = []
    for sym in settings.watchlist:
        snap = build_snapshot(client, sym)
        if snap:
            snapshots.append(snap)

    if not snapshots:
        return Analysis(market_comment="분석할 종목이 없습니다.", recommendations=[])

    user_prompt = (
        f"가드레일: 기본 익절 {settings.default_take_profit_pct}%, "
        f"기본 손절 {settings.default_stop_loss_pct}%, "
        f"동시보유 최대 {settings.max_positions}종목, "
        f"하루 예산 {settings.daily_budget_krw}원.\n"
        f"최대 {settings.max_recommendations}개까지 추천하라.\n\n"
        f"아래는 워치리스트 종목별 시세 스냅샷이다 "
        f"(가격·거래량은 오래된→최신 순):\n\n"
        f"{snapshots}"
    )

    anthropic_client = anthropic.Anthropic()
    response = anthropic_client.messages.parse(
        model=settings.model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=Analysis,
    )
    return response.parsed_output
