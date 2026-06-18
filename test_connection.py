"""인증 + 기본 조회가 되는지 빠르게 확인하는 스크립트.

실행:  python test_connection.py
"""

import os

from toss_client import TossApiError, TossClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

cid = os.getenv("TOSS_CLIENT_ID")
secret = os.getenv("TOSS_CLIENT_SECRET")

if not cid or not secret or "여기에" in (secret or ""):
    raise SystemExit("⚠️  .env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 를 먼저 채우세요.")

client = TossClient(cid, secret)

try:
    print("1) 토큰 발급 중...")
    token = client._get_token()
    print(f"   ✅ 토큰 OK (앞 20자: {token[:20]}...)")

    print("2) 계좌 조회 중...")
    accounts = client.get_accounts()
    if not accounts:
        print("   ⚠️  계좌가 없습니다 (빈 배열).")
    else:
        for a in accounts:
            print(f"   ✅ 계좌 {a.get('accountNo')} · accountSeq={a['accountSeq']}")

        seq = accounts[0]["accountSeq"]
        print("3) 보유자산 조회 중...")
        h = client.get_holdings(seq)
        items = h.get("items", [])
        print(f"   ✅ 보유 종목 {len(items)}개")
        for it in items[:5]:
            print(f"      - {it['name']}({it['symbol']}) x{it['quantity']}")

    print("\n🎉 연결 정상. 이제 'streamlit run app.py' 로 대시보드를 실행하세요.")
except TossApiError as e:
    print(f"\n❌ API 오류: {e}")
    print("   → client_id/secret 이 맞는지, 키 상태가 '활성'인지 확인하세요.")
except Exception as e:
    print(f"\n❌ 연결 실패: {e}")
