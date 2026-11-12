"""AI 출력 계약과 런타임 검증. 가격/금액은 Decimal 문자열이다."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json


def decimal(value, *, positive=False) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean은 숫자가 아닙니다.")
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("올바른 십진수가 필요합니다.") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("유한한 양수가 필요합니다.")
    return result


def timestamp(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("타임존이 없는 시간은 사용할 수 없습니다.")
    return result.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps(value, **kwargs) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, **kwargs)


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}
ACTION_SCHEMA = object_schema({
    "symbol": STR, "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD", "WATCH"]},
    "entry_low": STR, "entry_high": STR, "stop_price": STR, "target_price": STR,
    "allocation_krw": STR, "thesis": STR, "invalidation": STR,
    "source_urls": {"type": "array", "items": STR},
})
PLAN_SCHEMA = object_schema({"market_comment": STR, "actions": {"type": "array", "items": ACTION_SCHEMA}})


@dataclass(frozen=True)
class Action:
    symbol: str
    action: str
    entry_low: Decimal
    entry_high: Decimal
    stop_price: Decimal
    target_price: Decimal
    allocation_krw: Decimal
    thesis: str
    invalidation: str
    source_urls: list[str]

    @classmethod
    def parse(cls, data: dict) -> "Action":
        if set(data) != set(ACTION_SCHEMA["properties"]):
            raise ValueError("AI 주문안의 필드가 계약과 다릅니다.")
        if not isinstance(data["symbol"], str) or not data["symbol"] or len(data["symbol"]) > 20:
            raise ValueError("종목 심볼이 올바르지 않습니다.")
        if data["action"] not in {"BUY", "SELL", "HOLD", "WATCH"}:
            raise ValueError("지원하지 않는 판단입니다.")
        for key in ("thesis", "invalidation"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError("판단 근거와 철회 조건이 필요합니다.")
        if not isinstance(data["source_urls"], list) or not all(isinstance(u, str) and u.startswith("https://") for u in data["source_urls"]):
            raise ValueError("출처는 HTTPS URL 목록이어야 합니다.")
        numeric = {k: decimal(data[k]) for k in ("entry_low", "entry_high", "stop_price", "target_price", "allocation_krw")}
        if any(v < 0 for v in numeric.values()):
            raise ValueError("가격과 금액은 음수가 될 수 없습니다.")
        if data["action"] == "BUY":
            if not (0 < numeric["stop_price"] < numeric["entry_low"] <= numeric["entry_high"] < numeric["target_price"]):
                raise ValueError("손절 < 진입 하단 <= 진입 상단 < 목표가여야 합니다.")
            if numeric["allocation_krw"] <= 0:
                raise ValueError("매수 금액이 필요합니다.")
        return cls(**{**data, **numeric})

    def to_dict(self) -> dict:
        return json.loads(dumps(asdict(self)))


@dataclass(frozen=True)
class Plan:
    market_comment: str
    actions: list[Action]

    @classmethod
    def parse(cls, data: dict, allowed: set[str], max_actions: int = 12) -> "Plan":
        if set(data) != {"market_comment", "actions"} or not isinstance(data["market_comment"], str):
            raise ValueError("AI 분석 결과 형식이 다릅니다.")
        if not isinstance(data["actions"], list) or len(data["actions"]) > max_actions:
            raise ValueError("판단 종목 수가 너무 많습니다.")
        actions = [Action.parse(a) for a in data["actions"]]
        if len({a.symbol for a in actions}) != len(actions):
            raise ValueError("같은 종목에 대한 중복 판단입니다.")
        if any(a.symbol not in allowed for a in actions):
            raise ValueError("검증되지 않은 종목을 AI가 제시했습니다.")
        return cls(data["market_comment"], actions)

    def to_dict(self) -> dict:
        return {"market_comment": self.market_comment, "actions": [a.to_dict() for a in self.actions]}
