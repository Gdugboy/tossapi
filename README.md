# 토스증권 포트폴리오 대시보드

토스증권 Open API로 보유 자산·손익을 실시간으로 보여주는 **조회 전용** 대시보드.
매매 주문 API는 호출하지 않으므로 돈이 나갈 위험 없이 인증·데이터 흐름을 익힐 수 있다.

## 기능
- OAuth2 토큰 자동 발급/캐싱 (만료 30초 전 자동 갱신)
- 계좌 → 보유자산 → 환율 조회
- USD 자산을 KRW로 환산한 통합 평가금액·손익·당일손익 요약
- 종목별 평단/현재가/수익률 테이블 + 비중 차트

## 프로젝트 구조
```
tossapi/
├── app.py              # Streamlit 대시보드 (화면)
├── toss_client.py      # 토스 API 클라이언트 (토큰 관리 + 조회 메서드)
├── test_connection.py  # 인증/조회 연결 점검 스크립트
├── requirements.txt    # 의존성
├── .env                # 자격증명 (git 제외, 직접 작성)
├── .env.example        # 자격증명 템플릿
└── .gitignore
```

## 사전 준비: 자격증명
토스증권 개발자 콘솔(Open API Key 관리)에서 발급한 값을 `.env`에 넣는다.

| 콘솔 라벨 | `.env` 항목 |
|-----------|-------------|
| API Key (`tsck_live_...`) | `TOSS_CLIENT_ID` |
| Secret Key (`tssk_live_...`) | `TOSS_CLIENT_SECRET` |

```bash
cp .env.example .env   # 그리고 두 값 입력
```
⚠️ `.env`와 Secret Key는 절대 git/채팅에 올리지 말 것. 노출 시 콘솔에서 재발급.

## 설치
```bash
python -m pip install -r requirements.txt
```

## 실행
> Windows에서 `streamlit` 명령이 PATH에 없을 수 있으므로 `python -m streamlit`로 실행한다.

```bash
# 1) 연결 점검 (토큰→계좌→보유자산)
python test_connection.py

# 2) 대시보드 실행 → http://localhost:8501
python -m streamlit run app.py
```

종료: 터미널에서 `Ctrl+C`.

## 주의사항
- **조회 전용**이다. 주문/정정/취소 API는 호출하지 않는다.
- 데이터는 30초 캐시된다(Rate Limit 보호). 사이드바 🔄 로 강제 새로고침.
- 환율은 참고용 표시 환율(1분 갱신)이라 실제 거래 환율과 다를 수 있다.
- 개인 금융 정보이므로 Streamlit의 **Deploy(공개 배포)는 사용하지 않는다.**

## 다음 단계 아이디어
- 가격/손익 알림 봇 (텔레그램·슬랙)
- 캔들 데이터 백테스터
- DCA 자동 적립매수 (실매매의 가장 안전한 입문)
