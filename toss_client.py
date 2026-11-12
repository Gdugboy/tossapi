"""토스증권 Open API 클라이언트.

OAuth2 토큰 발급/캐싱과 대시보드에 필요한 조회 API를 감싼다.
금액은 정밀도 보존을 위해 응답의 문자열을 그대로 다루며, 계산이 필요한
곳에서만 Decimal 로 변환한다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests

BASE_URL = "https://openapi.tossinvest.com"


class TossApiError(RuntimeError):
    """API 가 에러 envelope 또는 비정상 상태코드를 반환했을 때."""

    def __init__(self, status: int, code: str | None, message: str):
        self.status = status
        self.code = code
        super().__init__(f"[{status}] {code or ''} {message}".strip())


@dataclass
class _Token:
    access_token: str
    expires_at: float  # epoch seconds

    @property
    def valid(self) -> bool:
        # 만료 30초 전이면 미리 갱신
        return time.time() < self.expires_at - 30


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: _Token | None = None
        self._lock = threading.Lock()
        self._session = requests.Session()

    # ── 인증 ────────────────────────────────────────────────
    def _get_token(self) -> str:
        with self._lock:
            if self._token and self._token.valid:
                return self._token.access_token

            resp = self._session.post(
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                body = _safe_json(resp)
                raise TossApiError(
                    resp.status_code,
                    body.get("error"),
                    body.get("error_description", "토큰 발급 실패"),
                )
            data = resp.json()
            self._token = _Token(
                access_token=data["access_token"],
                expires_at=time.time() + int(data["expires_in"]),
            )
            return self._token.access_token

    # ── 공통 요청 ────────────────────────────────────────────
    def _get(
        self, path: str, params: dict | None = None, account_seq: int | None = None
    ) -> object:
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)

        resp = self._session.get(
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
        body = _safe_json(resp)
        if resp.status_code != 200:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise TossApiError(
                resp.status_code,
                err.get("code"),
                err.get("message", "요청 실패"),
            )
        return body.get("result") if isinstance(body, dict) else body

    def _post(self, path: str, body: dict, account_seq: int | None = None) -> object:
        # 인증용 POST는 _get_token 내부에서만 수행한다. 주문 경로는 전송하지 않는다.
        raise TossApiError(0, "live-orders-disabled", "이 프로젝트는 조회·모의운용 전용입니다. 실거래 주문을 전송하지 않습니다.")

    # ── 조회 API ────────────────────────────────────────────
    def get_accounts(self) -> list[dict]:
        """정상 상태 계좌 목록. 각 항목에 accountSeq 포함."""
        return self._get("/api/v1/accounts") or []

    def get_holdings(self, account_seq: int, symbol: str | None = None) -> dict:
        """보유 자산 요약 + 종목별 상세."""
        params = {"symbol": symbol} if symbol else None
        return self._get("/api/v1/holdings", params=params, account_seq=account_seq)

    def get_exchange_rate(self, base: str = "USD", quote: str = "KRW") -> dict:
        """참고용 표시 환율 (1분 갱신)."""
        return self._get(
            "/api/v1/exchange-rate",
            params={"baseCurrency": base, "quoteCurrency": quote},
        )

    def get_prices(self, symbols: list[str]) -> list[dict]:
        """현재가 다건 조회 (최대 200건)."""
        return self._get(
            "/api/v1/prices", params={"symbols": ",".join(symbols)}
        ) or []

    def get_candles(
        self, symbol: str, interval: str = "1d", count: int = 60, before: str | None = None
    ) -> dict:
        """캔들(OHLCV) 조회. interval: '1m' | '1d'. 최대 200봉."""
        params = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        return self._get(
            "/api/v1/candles",
            params=params,
        )

    def get_stock_warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항(VI·투자경고·정리매매 등)."""
        return self._get(f"/api/v1/stocks/{symbol}/warnings") or []

    def get_all_stocks(self, market: str = "NASDAQ") -> list[dict]:
        """거래소 전체 활성 보통주 목록. 호출 측에서 일 단위로 캐시한다."""
        return self._get("/api/v1/stocks/all", params={"market": market, "status": "ACTIVE", "commonShare": "true"}) or []

    def get_rankings(self, country: str, kind: str = "MARKET_TRADING_AMOUNT", count: int = 100) -> dict:
        return self._get("/api/v1/rankings", params={"marketCountry": country, "type": kind, "duration": "1d", "count": count}) or {"rankings": []}

    def get_market_calendar(self, country: str, date: str) -> dict:
        return self._get(f"/api/v1/market-calendar/{country}", params={"date": date})

    def get_stocks(self, symbols: list[str]) -> list[dict]:
        """종목 기본정보(이름·시장·통화·거래정지 등)."""
        return self._get(
            "/api/v1/stocks", params={"symbols": ",".join(symbols)}
        ) or []

    def get_buying_power(self, account_seq: int, currency: str = "KRW") -> dict:
        """현금 기반 매수 가능 금액."""
        return self._get(
            "/api/v1/buying-power",
            params={"currency": currency},
            account_seq=account_seq,
        )

    def get_sellable_quantity(self, account_seq: int, symbol: str) -> dict:
        """종목별 매도 가능 수량."""
        return self._get(
            "/api/v1/sellable-quantity",
            params={"symbol": symbol},
            account_seq=account_seq,
        )

    def get_orders(self, account_seq: int, status: str = "OPEN") -> list[dict]:
        """주문 목록. status: 'OPEN'(진행중) | 'CLOSED'(종료)."""
        return self._get(
            "/api/v1/orders",
            params={"status": status},
            account_seq=account_seq,
        ) or []

    # ── 주문 API (계좌에 영향, 신중히) ───────────────────────────
    def create_order(self, account_seq: int, order: dict) -> dict:
        """매수/매도 주문 생성. order 는 OrderCreateRequest 스키마 dict."""
        return self._post("/api/v1/orders", body=order, account_seq=account_seq)

    def cancel_order(self, account_seq: int, order_id: str) -> dict:
        """주문 취소."""
        return self._post(
            f"/api/v1/orders/{order_id}/cancel", body={}, account_seq=account_seq
        )


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
