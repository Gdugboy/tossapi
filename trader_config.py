"""공유 전략 설정. 인증정보는 환경변수에만 둔다."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    initial_capital_krw: Decimal = Decimal("500000")
    max_position_krw: Decimal = Decimal("400000")
    cash_reserve_krw: Decimal = Decimal("100000")
    max_positions: int = 2
    max_entries_per_day: int = 2
    max_news_analyses_per_day: int = 2
    max_news_checks_per_day: int = 6
    max_rotation_analyses_per_day: int = 1
    max_analysis_attempts_per_market_day: int = 8
    analysis_before_open_minutes: int = 60
    analysis_after_open_minutes: int = 10
    news_interval_minutes: int = 60
    poll_seconds: int = 60
    quote_max_age_seconds: int = 300
    plan_valid_minutes: int = 90
    shortlist_size: int = 12
    screening_pool_size: int = 60
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("10")
    kr_symbols: tuple[str, ...] = ("005930", "000660")
    countries: tuple[str, ...] = ("US", "KR")
    enable_news: bool = True
    ai_auth_mode: str = "chatgpt"
    model: str = "auto"
    database: str = "data/paper.sqlite3"
    context_file: str = "SESSION_CONTEXT.md"
    dry_run: bool = field(default=True, init=False)

    def __post_init__(self):
        for key in ("initial_capital_krw", "max_position_krw", "cash_reserve_krw", "fee_bps", "slippage_bps"):
            value = Decimal(str(getattr(self, key)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{key}: 유한한 0 이상의 값이 필요합니다.")
            object.__setattr__(self, key, value)
        for key in ("max_positions", "max_entries_per_day", "max_analysis_attempts_per_market_day", "poll_seconds", "quote_max_age_seconds", "plan_valid_minutes", "shortlist_size", "screening_pool_size", "news_interval_minutes"):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key}: 양의 정수가 필요합니다.")
        for key in ("max_news_analyses_per_day", "max_news_checks_per_day", "max_rotation_analyses_per_day", "analysis_after_open_minutes"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key}: 0 이상의 정수가 필요합니다.")
        if type(self.enable_news) is not bool:
            raise ValueError("enable_news는 boolean이어야 합니다.")
        if self.ai_auth_mode not in {"chatgpt", "api_key"}:
            raise ValueError("ai_auth_mode는 chatgpt 또는 api_key여야 합니다.")
        if self.ai_auth_mode == "api_key" and self.model == "auto":
            raise ValueError("api_key 모드에서는 사용할 API 모델을 명시해야 합니다.")
        if type(self.analysis_before_open_minutes) is not int or self.analysis_before_open_minutes <= 0:
            raise ValueError("analysis_before_open_minutes: 양의 정수가 필요합니다.")
        if self.initial_capital_krw <= 0 or self.max_position_krw <= 0 or self.cash_reserve_krw >= self.initial_capital_krw:
            raise ValueError("초기 운용금/종목당 금액/예비 현금을 확인하세요.")
        if self.fee_bps >= 1000 or self.slippage_bps >= 1000:
            raise ValueError("수수료/슬리피지 단위를 확인하세요 (bps).")
        if not set(self.countries) <= {"US", "KR"} or not self.countries:
            raise ValueError("countries는 US/KR만 지원합니다.")
        if not set(self.kr_symbols) <= {"005930", "000660"}:
            raise ValueError("국내 종목은 삼성전자와 SK하이닉스로 제한합니다.")
        object.__setattr__(self, "kr_symbols", tuple(self.kr_symbols))
        object.__setattr__(self, "countries", tuple(self.countries))

    @classmethod
    def load(cls, path: Path = ROOT / "strategy.json") -> "Settings":
        values = json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else {}
        if values.pop("mode", "paper") != "paper":
            raise ValueError("paper 모드만 지원합니다. 실거래 모드가 없습니다.")
        values.pop("version", None)
        values["ai_auth_mode"] = os.getenv("OPENAI_AUTH_MODE") or values.get("ai_auth_mode", "chatgpt")
        values["model"] = os.getenv("OPENAI_MODEL") or values.get("model", "auto")
        return cls(**values)

    def public_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))

    @property
    def db_path(self) -> Path:
        return ROOT / self.database
