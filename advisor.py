"""AI 트레이딩 코파일럿 — 워치리스트 분석 → 추천 표출 (CLI).

실행:  python advisor.py
필요:  .env 에 TOSS_CLIENT_ID/SECRET + ANTHROPIC_API_KEY

이 단계는 '추천만' 한다. 실제 매수/매도는 하지 않는다(다음 단계).
"""

from __future__ import annotations

import os

from config import Settings, anthropic_key_present
from toss_client import TossApiError, TossClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    cid = os.getenv("TOSS_CLIENT_ID")
    secret = os.getenv("TOSS_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("⚠️  .env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 를 먼저 채우세요.")
    if not anthropic_key_present():
        raise SystemExit(
            "⚠️  ANTHROPIC_API_KEY 가 없습니다. AI 분석에 필요합니다.\n"
            "    .env 에 ANTHROPIC_API_KEY=sk-ant-... 를 추가하세요."
        )

    settings = Settings()
    if not settings.watchlist:
        raise SystemExit("⚠️  watchlist.txt 가 비어 있습니다. 분석할 종목을 넣으세요.")

    print(f"📋 워치리스트 {len(settings.watchlist)}종목 분석 중... "
          f"(모델: {settings.model})")
    print(f"   안전모드(DRY_RUN): {settings.dry_run} · "
          f"일일예산: {settings.daily_budget_krw}원\n")

    from analyzer import analyze  # 지연 임포트 (anthropic 의존)

    client = TossClient(cid, secret)
    try:
        result = analyze(client, settings)
    except TossApiError as e:
        raise SystemExit(f"❌ 토스 API 오류: {e}")

    print(f"🗣️  시장 한줄평: {result.market_comment}\n")
    print("=" * 64)
    icon = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}
    for i, r in enumerate(result.recommendations, 1):
        print(
            f"{i}. {icon.get(r.action, '·')} {r.name} ({r.symbol}) "
            f"· {r.action} · {r.style} · 확신도 {r.conviction}/10"
        )
        print(f"   진입대 {r.entry_zone} | 익절 +{r.take_profit_pct}% / "
              f"손절 -{r.stop_loss_pct}%")
        print(f"   💡 {r.thesis}")
        print(f"   ⚠️  {r.risks}")
        print("-" * 64)

    print(
        "\n※ 이 추천은 차트·가격 기반 AI 의견입니다. 수익을 보장하지 않으며, "
        "실제 매수/매도는 본인 판단으로 하세요."
    )


if __name__ == "__main__":
    main()
