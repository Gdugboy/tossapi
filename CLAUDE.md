# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code(및 개발자)를 위한 안내다.

## 프로젝트 개요
토스증권 Open API를 사용하는 **조회 전용 포트폴리오 대시보드**.
보유 자산·손익·환율을 불러와 Streamlit 화면으로 보여준다.
인증/데이터 흐름을 익히는 입문 프로젝트이며, 이후 알림 봇·백테스터·DCA 봇으로 확장 예정.

## 핵심 원칙
- **조회 전용**: 주문/정정/취소 등 계좌에 영향을 주는 API는 호출하지 않는다.
  매매 기능을 추가할 때는 반드시 사용자에게 명시적으로 확인받는다.
- **시크릿 보호**: `.env`(특히 `TOSS_CLIENT_SECRET`)는 git/로그/채팅에 노출 금지.
  `.gitignore`에 `.env` 포함됨. 라이브 키이므로 취급 주의.
- **공개 배포 금지**: 개인 금융 정보라 Streamlit Deploy 등 외부 호스팅을 쓰지 않는다.

## 구조
| 파일 | 역할 |
|------|------|
| `toss_client.py` | API 클라이언트. OAuth2 토큰 발급/캐싱 + 조회 메서드 |
| `app.py` | Streamlit 대시보드 |
| `test_connection.py` | 인증/조회 연결 점검 |

## 실행
```bash
python -m pip install -r requirements.txt
python test_connection.py          # 연결 점검
python -m streamlit run app.py     # 대시보드 (localhost:8501)
```
- 이 환경에서 `streamlit` 명령이 PATH에 없다 → 항상 `python -m streamlit` 사용.
- 셸은 PowerShell. 백그라운드 서버 실행 후 출력 로그로 기동 확인.

## API 메모 (tossAPI.txt 스펙 기준)
- 인증: `POST /oauth2/token`, client_credentials, form-urlencoded.
  client당 유효 토큰 1개, 재발급 시 이전 토큰 즉시 무효화. expires_in 후 만료.
- 계좌: `GET /api/v1/accounts` → `accountSeq` 획득.
- 계좌 컨텍스트 API는 `X-Tossinvest-Account: {accountSeq}` 헤더 필요.
- 보유자산: `GET /api/v1/holdings` (요약 + items, 금액은 문자열).
- 환율: `GET /api/v1/exchange-rate` (USD↔KRW, 1분 갱신, 참고용).
- **금액은 정밀도 보존을 위해 문자열로 옴 → Decimal로 처리(float 금지).**
- Rate Limit이 그룹별로 존재 → 무한 폴링 금지, 캐시(현재 30초) 활용.
- 웹소켓 미지원(폴링만), 캔들은 1분봉/일봉만.

## 컨벤션
- 주석/문서는 한국어. 코드 식별자는 영어.
- 새 API는 `toss_client.py`에 메서드로 추가하고 화면 로직과 분리한다.
