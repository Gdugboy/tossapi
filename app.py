"""토스증권 포트폴리오 대시보드 (Streamlit).

실행:  streamlit run app.py
인증:  .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 설정하거나 사이드바에 입력.
주의:  조회 전용. 주문/매매 API 는 호출하지 않는다.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from toss_client import TossApiError, TossClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def D(value) -> Decimal:
    """문자열/None 금액을 Decimal 로. 변환 불가/None 은 0."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def fmt_krw(amount: Decimal) -> str:
    return f"₩{amount:,.0f}"


st.set_page_config(page_title="토스 포트폴리오", page_icon="📈", layout="wide")
st.title("📈 토스증권 포트폴리오 대시보드")

# ── 인증 정보 ────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")
    client_id = st.text_input(
        "Client ID", value=os.getenv("TOSS_CLIENT_ID", ""), type="password"
    )
    client_secret = st.text_input(
        "Client Secret", value=os.getenv("TOSS_CLIENT_SECRET", ""), type="password"
    )
    st.caption("조회 전용 대시보드입니다. 매매 주문은 보내지 않습니다.")
    refresh = st.button("🔄 새로고침", use_container_width=True)

if not client_id or not client_secret:
    st.info("사이드바에 Client ID / Secret 을 입력하면 포트폴리오를 불러옵니다.")
    st.stop()


@st.cache_resource
def get_client(cid: str, secret: str) -> TossClient:
    return TossClient(cid, secret)


@st.cache_data(ttl=30)
def load_data(cid: str, secret: str) -> dict:
    client = get_client(cid, secret)
    accounts = client.get_accounts()
    if not accounts:
        return {"accounts": [], "holdings": None, "rate": None}
    account_seq = accounts[0]["accountSeq"]
    holdings = client.get_holdings(account_seq)
    try:
        rate = client.get_exchange_rate("USD", "KRW")
    except TossApiError:
        rate = None
    return {"accounts": accounts, "holdings": holdings, "rate": rate}


if refresh:
    load_data.clear()

try:
    data = load_data(client_id, client_secret)
except TossApiError as e:
    st.error(f"API 오류: {e}")
    st.stop()
except Exception as e:  # 네트워크 등
    st.error(f"불러오기 실패: {e}")
    st.stop()

accounts = data["accounts"]
holdings = data["holdings"]
rate_info = data["rate"]

if not accounts:
    st.warning("조회 가능한 계좌가 없습니다.")
    st.stop()

st.caption(f"계좌: {accounts[0].get('accountNo', '-')} · accountSeq={accounts[0]['accountSeq']}")

usd_krw = D(rate_info["rate"]) if rate_info else Decimal(0)

# ── 합산 요약 (USD → KRW 환산하여 단일 통화로) ──────────────────
mv = holdings["marketValue"]["amount"]
pl = holdings["profitLoss"]
daily = holdings["dailyProfitLoss"]

total_value_krw = D(mv["krw"]) + D(mv["usd"]) * usd_krw
total_pl_krw = D(pl["amount"]["krw"]) + D(pl["amount"]["usd"]) * usd_krw
daily_pl_krw = D(daily["amount"]["krw"]) + D(daily["amount"]["usd"]) * usd_krw
pl_rate = D(pl.get("rate")) * 100  # SDK 제공 KRW 환산 손익률
daily_rate = D(daily.get("rate")) * 100

c1, c2, c3, c4 = st.columns(4)
c1.metric("총 평가금액", fmt_krw(total_value_krw))
c2.metric("총 손익", fmt_krw(total_pl_krw), f"{pl_rate:+.2f}%")
c3.metric("당일 손익", fmt_krw(daily_pl_krw), f"{daily_rate:+.2f}%")
c4.metric("USD/KRW", f"{usd_krw:,.1f}" if usd_krw else "—")

st.divider()

# ── 종목별 상세 ────────────────────────────────────────────
items = holdings.get("items", [])
if not items:
    st.info("보유 중인 종목이 없습니다.")
    st.stop()

rows = []
for it in items:
    cur = it["currency"]
    eval_native = D(it["marketValue"]["amount"])
    eval_krw = eval_native * usd_krw if cur == "USD" else eval_native
    rows.append(
        {
            "종목": f"{it['name']} ({it['symbol']})",
            "국가": it["marketCountry"],
            "수량": float(D(it["quantity"])),
            "평단": float(D(it["averagePurchasePrice"])),
            "현재가": float(D(it["lastPrice"])),
            "평가금액(KRW)": float(eval_krw),
            "손익(원화)": float(
                D(it["profitLoss"]["amount"]) * (usd_krw if cur == "USD" else 1)
            ),
            "수익률": float(D(it["profitLoss"].get("rate")) * 100),
        }
    )

df = pd.DataFrame(rows).sort_values("평가금액(KRW)", ascending=False)

left, right = st.columns([3, 2])

with left:
    st.subheader("보유 종목")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "평가금액(KRW)": st.column_config.NumberColumn(format="₩%.0f"),
            "손익(원화)": st.column_config.NumberColumn(format="₩%.0f"),
            "수익률": st.column_config.NumberColumn(format="%.2f%%"),
            "평단": st.column_config.NumberColumn(format="%.2f"),
            "현재가": st.column_config.NumberColumn(format="%.2f"),
        },
    )

with right:
    st.subheader("종목 비중")
    alloc = df.set_index("종목")["평가금액(KRW)"]
    st.bar_chart(alloc)

st.caption(
    "※ 환율은 참고용 표시 환율(1분 갱신)이며 실제 거래 환율과 다를 수 있습니다. "
    "데이터는 30초 캐시됩니다."
)
