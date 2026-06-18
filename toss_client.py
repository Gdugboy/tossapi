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


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
