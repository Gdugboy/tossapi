"""모의 장부·분석 화면과 로컬 실행기 제어. 실제 주문은 전송하지 않는다."""
import json
from decimal import Decimal
import pandas as pd
import streamlit as st

from trader_config import Settings
from trading_models import decimal
from trading_state import State
import theme


def runtime_controls():
    from runtime_control import request, status
    current = status()
    if not current["alive"]:
        st.info("자동 운용이 꺼져 있습니다. toss 폴더의 실행.cmd를 더블클릭하세요.")
        return
    labels = {"starting": "준비 중", "running": "실행 중", "stopping": "중지 중", "stopped": "중지됨", "error": "확인 필요"}
    worker = current.get("worker", "stopped")
    st.write("모의 자동 운용 · " + labels.get(worker, worker))
    st.caption("창을 닫아도 계속 실행됩니다. PC가 켜져 있고 절전 상태가 아니어야 합니다.")
    left, right = st.columns(2)
    with left:
        if worker in {"running", "starting", "stopping"}:
            if st.button("운용 중지", disabled=worker == "stopping", use_container_width=True):
                request("pause")
                st.rerun()
        elif st.button("운용 시작", type="primary", use_container_width=True):
            request("start")
            st.rerun()
    with right:
        if st.button("앱 완전 종료", use_container_width=True, disabled=current.get("service") == "stopping"):
            request("shutdown")
            st.info("진행 중인 점검을 마친 뒤 종료합니다. 다시 열 때는 실행.cmd를 누르세요.")
    if worker == "stopping" or current.get("service") == "stopping":
        st.info("진행 중인 분석·점검을 마치고 중지합니다. 보유 모의 종목을 자동 청산하지는 않습니다.")
    if current.get("activity") == "checking":
        st.caption("시세 점검 또는 AI 분석 중 · 상태는 자동 갱신됩니다.")
    if current.get("error"):
        st.error(current["error"])
    summary = current.get("last_summary", {})
    if summary.get("errors"):
        st.warning("일부 시장 점검에 실패했습니다. 분석·차트의 최근 실행 상태를 확인하세요.")
    elif summary.get("status") == "another_runner_active":
        st.warning("별도로 실행한 운용기가 있습니다. 중복 모의 체결을 막고 대기합니다.")


def authentication(settings):
    if settings.ai_auth_mode != "chatgpt":
        st.caption("OpenAI API 키 방식 · 별도 API 사용량 과금")
        return
    from chatgpt_auth import ChatGPTAuth, USAGE_URL
    auth = ChatGPTAuth()
    status = auth.status()
    st.caption("Using ChatGPT plan" if status["plan_enabled"] else "ChatGPT 요금제 연결 필요")
    if status["plan_enabled"]:
        st.write(status.get("active_label"), "·", status.get("model") or "계정에서 모델 자동 선택")
        if status.get("welcome_pending"):
            st.info("You're using your ChatGPT plan. 이 앱의 AI 요청은 ChatGPT 구독의 사용 한도를 이용합니다.")
            if st.button("Got it"):
                auth.acknowledge()
                st.rerun()
    else:
        if st.button("Continue with ChatGPT", type="secondary"):
            import subprocess
            import sys
            from trader_config import ROOT
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            command = [str(python if python.exists() else sys.executable), str(ROOT / "advisor.py"), "login"]
            subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            st.info("브라우저에서 로그인과 요금제 사용 승인을 완료한 뒤 화면을 새로고침하세요.")
    st.link_button("Manage usage", USAGE_URL)
    with st.expander("연결 상태"):
        st.json(status)


@st.fragment(run_every=10)
def render():
    settings = Settings.load()
    st.subheader("OpenAI 모의 운용")
    st.caption("가상 자금 50만 원 · 실제 계좌는 왼쪽 ‘실제 자산 조회’에서 확인")
    runtime_controls()
    authentication(settings)
    if not settings.db_path.exists():
        st.info("운용 기록이 아직 없습니다. 실행.cmd로 켜면 자동으로 기록을 준비합니다.")
        return
    state = State(settings.db_path, settings.initial_capital_krw)
    data = state.overview()
    marks = state.get("marks", {})
    cash = decimal(data["cash_krw"])
    holdings_value = Decimal(0)
    rows = []
    for p in data["positions"]:
        mark = marks.get(p["symbol"], {})
        price = decimal(mark.get("price", p["entry_price"]))
        fx = decimal(mark.get("fx", p["entry_fx"]))
        value = decimal(p["quantity"]) * price * fx
        holdings_value += value
        rows.append({"종목": p["symbol"], "수량": p["quantity"], "진입가": p["entry_price"],
                     "표시 현재가": str(price), "목표가": p["target_price"], "철회 가격": p["stop_price"],
                     "평가금액(원)": float(value), "미실현손익(원)": float(value - decimal(p["cost_krw"])),
                     "시세 시각": mark.get("timestamp", "진입가 기준"), "근거": p["thesis"]})
    equity = cash + holdings_value
    initial = decimal(data["initial_capital_krw"])
    c1, c2, c3 = st.columns(3)
    c1.metric("모의 평가액", f"₩{equity:,.0f}", f"{(equity / initial - 1) * 100:+.2f}%")
    c2.metric("모의 현금", f"₩{cash:,.0f}")
    c3.metric("보유 종목", str(len(rows)))
    st.caption("평가액은 저장된 시세·환율 기준이며 예상 매도 비용과 OpenAI API 이용료는 포함하지 않습니다.")
    st.write("마지막 운용 확인:", state.get("heartbeat", "실행 이력 없음"))
    tabs = st.tabs(["보유·모의 체결", "분석·차트", "공유 규칙·상태"])
    with tabs[0]:
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("현재 모의 보유 종목이 없습니다.")
        st.write("최근 모의 체결")
        st.dataframe(pd.DataFrame(data["recent_fills"]), hide_index=True, use_container_width=True)
    with tabs[1]:
        runs = [r for r in data["recent_runs"] if r["status"] == "complete" and r["payload"]]
        if runs:
            choices = {r["id"]: r for r in runs}
            selected = st.selectbox("저장된 분석", list(choices))
            payload = choices[selected]["payload"]
            st.write(payload["plan"]["market_comment"])
            st.dataframe(pd.DataFrame(payload["plan"]["actions"]), hide_index=True, use_container_width=True)
            st.write("시장 탐색 범위", payload["market"]["coverage"])
            candidates = {x["symbol"]: x for x in payload["market"]["candidates"]}
            if candidates:
                symbol = st.selectbox("차트", list(candidates))
                snapshot = candidates[symbol]
                bars = snapshot["technical"].get("ohlcv", [])
                if bars:
                    chart = [{"date": b["timestamp"][:10], "open": float(b["openPrice"]), "high": float(b["highPrice"]),
                              "low": float(b["lowPrice"]), "close": float(b["closePrice"]), "volume": float(b["volume"])} for b in bars]
                    st.vega_lite_chart(pd.DataFrame(chart), {
                        "encoding": {"x": {"field": "date", "type": "ordinal", "axis": {"labelAngle": -45}},
                                     "color": {"condition": {"test": "datum.close >= datum.open", "value": theme.UP}, "value": theme.DOWN}},
                        "layer": [{"mark": "rule", "encoding": {"y": {"field": "low", "type": "quantitative", "scale": {"zero": False}}, "y2": {"field": "high"}}},
                                  {"mark": {"type": "bar", "size": 5}, "encoding": {"y": {"field": "open", "type": "quantitative", "scale": {"zero": False}}, "y2": {"field": "close"}}}],
                        "height": 320}, use_container_width=True)
                    st.bar_chart(pd.DataFrame(chart).set_index("date")["volume"], color=theme.PRIMARY)
                st.json({k: v for k, v in snapshot["technical"].items() if k != "ohlcv"})
            st.write("웹 검색 근거")
            st.write(payload["research"]["text"])
            for source in payload["research"]["sources"]:
                st.link_button(source.get("title", source["url"]), source["url"])
        else:
            st.info("완료된 AI 분석이 아직 없습니다.")
        with st.expander("최근 실행 상태"):
            st.json([{k: v for k, v in r.items() if k != "payload"} for r in data["recent_runs"]])
    with tabs[2]:
        st.json(settings.public_dict())
        st.write("다른 AI 세션은 AGENTS.md, SESSION_CONTEXT.md와 아래 명령의 출력을 읽으면 됩니다.")
        st.code("python advisor.py context --out data/shared_context.json", language="powershell")
        st.json(state.get("last_tick", {}))
