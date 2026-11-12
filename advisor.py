"""FOMO CLI. 실제 주문을 보내는 명령은 없다."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

from trader_config import ROOT, Settings
from trading_models import dumps, utcnow


def load_environment():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)


def doctor(settings):
    import importlib.util
    from chatgpt_auth import ChatGPTAuth
    auth_status = ChatGPTAuth().status() if settings.ai_auth_mode == "chatgpt" else None
    result = {"mode": "paper", "model": settings.model,
              "env_file": str(ROOT / ".env"),
              "credentials_present": {k: bool(os.getenv(k)) for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "OPENAI_API_KEY")},
              "dependencies": {k: bool(importlib.util.find_spec(k)) for k in ("requests", "dotenv", "streamlit", "pandas", "tzdata", "jwt")},
              "credential_roles": {"TOSS_CLIENT_ID/TOSS_CLIENT_SECRET": "토스 시세·계좌 조회 인증", "OPENAI_API_KEY": "api_key 모드에서만 필요. ChatGPT 구독 모드에서는 불필요"},
              "chatgpt_auth": auth_status,
              "strategy": settings.public_dict(), "network_calls": 0}
    print(dumps(result, indent=2))
    toss_ready = all(result["credentials_present"][k] for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"))
    ai_ready = auth_status.get("plan_enabled", False) if auth_status is not None else result["credentials_present"]["OPENAI_API_KEY"]
    return toss_ready and ai_ready


def make_runner(settings):
    required = ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"]
    if settings.ai_auth_mode == "api_key":
        required.append("OPENAI_API_KEY")
    needed = [k for k in required if not os.getenv(k)]
    if needed:
        raise RuntimeError("설정이 필요합니다: " + ", ".join(needed) + ". 키 값은 채팅에 붙여넣지 말고 .env에 보관하세요.")
    from market_data import MarketData
    from openai_engine import OpenAIEngine
    from paper_engine import Runner
    from toss_client import TossClient
    from trading_state import State
    ai = OpenAIEngine(settings)
    ai.ensure_ready()
    state = State(settings.db_path, settings.initial_capital_krw)
    client = TossClient(os.environ["TOSS_CLIENT_ID"], os.environ["TOSS_CLIENT_SECRET"])
    return Runner(settings, state, MarketData(client, state, settings), ai)


def main():
    parser = argparse.ArgumentParser(description="OpenAI 시장 분석·공유 기록·모의 자동운용")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="키의 존재 여부만 확인. 네트워크 호출 없음")
    login = sub.add_parser("login", help="Continue with ChatGPT: 브라우저에서 구독 사용 연결")
    login.add_argument("--new-account", action="store_true", help="별도 계정/워크스페이스 추가")
    login.add_argument("--profile", help="auth-status에 표시되는 기존 계정 등록 선택")
    sub.add_parser("auth-status", help="토큰을 노출하지 않고 ChatGPT 연결 상태 확인")
    sub.add_parser("auth-models", help="연결된 계정에서 사용 가능한 모델 목록")
    sub.add_parser("auth-test", help="ChatGPT 구독으로 짧은 연결 확인 요청")
    sub.add_parser("logout", help="이 앱의 ChatGPT 갱신 세션 해제. 다른 앱 로그인은 변경하지 않음")
    sub.add_parser("status", help="모의 장부 읽기")
    context = sub.add_parser("context", help="다른 AI 세션이 읽을 공유 문맥 출력")
    context.add_argument("--out", help="선택: 프로젝트 안에 JSON 파일로 저장")
    analyze = sub.add_parser("analyze", help="시세 조회와 OpenAI API로 지금 분석, 모의 진입 기준 저장")
    analyze.add_argument("--country", choices=["US", "KR"], default="US")
    run = sub.add_parser("run", help="정기 분석·뉴스 확인·모의 목표 청산 루프")
    run.add_argument("--once", action="store_true", help="한 tick만 실행")
    sub.add_parser("demo", help="키/네트워크 없이 합성 데이터로 모의 진입·청산 검증")
    args = parser.parse_args()
    load_environment()
    settings = Settings.load()
    if args.command in {"login", "auth-status", "auth-models", "auth-test", "logout"}:
        from chatgpt_auth import ChatGPTAuth
        auth = ChatGPTAuth()
        if args.command == "login":
            auth.sign_in(new_account=args.new_account, profile=args.profile)
            print("You're using your ChatGPT plan. Manage usage: https://chatgpt.com/settings/usage", flush=True)
        if args.command == "logout":
            result = auth.sign_out()
            print(dumps(result, indent=2))
            if not result["remote_revocation_confirmed"]:
                print("로컬 연결은 해제됐지만 서버 세션 해제는 확인하지 못했습니다. ChatGPT 설정에서 앱 연결을 확인하세요.")
        elif args.command == "auth-models":
            print(dumps(auth.models(), indent=2))
        elif args.command in {"login", "auth-test"}:
            from openai_engine import OpenAIEngine
            ai = OpenAIEngine(settings, auth=auth)
            response = ai._request("계정 연결 확인이다. '연결 확인'이라는 말만 답하라.", {"purpose": "ChatGPT plan connection check"})
            print(dumps({"connection_test": "completed", "auth_mode": ai.auth_mode, "model": response.get("model"), "reply": ai.text(response), "real_orders": 0}, indent=2))
        else:
            print(dumps(auth.status(), indent=2))
        return 0
    if args.command == "doctor":
        return 0 if doctor(settings) else 2
    if args.command == "demo":
        from demo_trader import demo
        print(dumps(demo(), indent=2))
        return 0
    if args.command in {"status", "context"}:
        from trading_state import State
        from openai_engine import shared_context
        state = State(settings.db_path, settings.initial_capital_krw)
        value = state.overview() if args.command == "status" else shared_context(settings, state)
        rendered = dumps(value, indent=2)
        if args.command == "context" and args.out:
            target = (ROOT / args.out).resolve()
            if not target.is_relative_to(ROOT / "data") or target.suffix.lower() != ".json":
                raise ValueError("공유 출력은 프로젝트 data/ 폴더 안의 JSON 파일로 저장하세요.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
        print(rendered)
        return 0
    runner = make_runner(settings)
    if args.command == "analyze":
        now = utcnow()
        ok = runner.state.acquire(runner.owner, now)
        if not ok:
            raise RuntimeError("다른 운용 프로세스가 실행 중입니다.")
        try:
            identity = "manual:" + args.country + ":" + now.isoformat()
            succeeded = runner.analyze(args.country, "manual", now, identity)
            print(dumps({"analysis_complete": succeeded, "mode": "paper", "run_id": identity}))
            return 0 if succeeded else 1
        finally:
            runner.state.release(runner.owner)
    if args.once:
        print(dumps(runner.tick(), indent=2))
    else:
        runner.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("모의 운용을 종료합니다. 장부는 유지됩니다.")
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1)
