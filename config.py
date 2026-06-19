"""봇 설정 — 실시간 수정 가능한 파라미터.

UI/CLI 어디서든 같은 값을 읽도록 한 곳에 모은다.
실제 매매 안전장치(DRY_RUN, 일일예산)도 여기서 통제한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

WATCHLIST_FILE = Path(__file__).with_name("watchlist.txt")


@dataclass
class Settings:
    # ── 안전장치 ──────────────────────────────────────────
    dry_run: bool = True           # True면 실제 주문 안 보내고 로그만 (기본 ON)
    daily_budget_krw: Decimal = Decimal("50000")   # 하루 매수 예산 하드캡(원)
    max_positions: int = 3         # 동시 보유 종목 수 상한
    require_approval_for_sell: bool = True  # 손절 매도는 사람 승인 필요

    # ── 전략 파라미터 (AI 판단의 기본값/가드레일) ──────────
    default_take_profit_pct: Decimal = Decimal("3.0")   # 익절 기본 %
    default_stop_loss_pct: Decimal = Decimal("2.0")     # 손절 기본 %
    max_recommendations: int = 5   # 표출할 추천 종목 수

    # ── 시장 ─────────────────────────────────────────────
    markets: tuple[str, ...] = ("KR", "US")

    # ── AI ──────────────────────────────────────────────
    model: str = "claude-opus-4-8"

    watchlist: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.watchlist:
            self.watchlist = load_watchlist()


def load_watchlist() -> list[str]:
    """watchlist.txt 에서 종목 심볼을 읽는다. '#' 주석·빈 줄 무시."""
    if not WATCHLIST_FILE.exists():
        return []
    out: list[str] = []
    for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


def anthropic_key_present() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))
