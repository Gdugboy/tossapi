# FOMO · OpenAI 모의 트레이더

나스닥 시장 탐색 → 차트·기업/뉴스 분석 → 모의 진입 → 목표가/철회 가격 감시 → 모의 청산 → 다음 후보 탐색.
기존 토스 포트폴리오 화면을 유지하고, OpenAI 기반 분석과 세션 간 공유 장부를 추가했다.

**이 버전은 모의 운용 전용이다. 실제 계좌 주문을 보내지 않으며 live 모드가 없다.**

## 실행은 하나

**저장소 폴더의 `실행.cmd`를 더블클릭**한다. 기존 상위 toss 폴더의 실행 파일도 사용할 수 있다. 화면과 모의 자동 운용이 함께 켜진다.
필요한 실행 환경은 처음에 자동 준비하며, 기존 `.env`와 ChatGPT 로그인은 그대로 쓴다.

- 화면에서 **운용 시작 / 운용 중지 / 앱 완전 종료**를 누른다. 상태와 장부는 자동 갱신된다.
- 다시 더블클릭하면 기존 창을 열며, 운용이 중지돼 있으면 재시작한다. 중복 운용기는 만들지 않는다.
- 왼쪽 **실제 자산 조회**에서 연결된 토스 계좌의 잔고·보유 종목을 확인한다.
- 브라우저를 닫아도 모의 운용은 계속된다. PC를 끄거나 절전하면 점검도 멈춘다.
- 중지는 진행 중인 점검 후 적용된다. 모의 보유 종목을 자동 청산하지 않는다.

로컬 화면만 열며 Windows 자동 시작이나 클라우드 배포는 설치하지 않는다.
계좌 조회는 사용자 화면에서만 수행하고 실제 계좌 정보는 AI 분석·공유 장부로 보내지 않는다.

## 개발자용 설정·명령

아래 명령은 점검·개발용이며 평소에는 입력할 필요 없다.

### 수동 준비

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

기존 `.env`는 그대로 둔다. 필요한 항목은 `.env.example`을 참고해 추가한다.

```dotenv
TOSS_CLIENT_ID=발급받은_값
TOSS_CLIENT_SECRET=발급받은_값
OPENAI_AUTH_MODE=chatgpt
OPENAI_MODEL=auto
```

토스 키는 이 파일이나 채팅에 적지 않는다. **기본 연결은 ChatGPT 구독 로그인이며 별도 OPENAI_API_KEY는 필요 없다.**
이 앱에서 새로 로그인해 요금제 사용 권한을 승인한다. 기존 Codex/다른 앱의 인증 파일을 읽거나 가져오지 않는다.
실제 계정의 모델 목록에서 모델을 선택한다. `auto`는 목록에 있을 때 GPT-6.1 Sol, 없으면 서버의 첫 표시 모델을 사용한다.
ChatGPT 구독의 공유 사용 한도와 앱 사용 한도가 적용된다. 한도/권한 오류 시 별도 과금 API로 자동 전환하지 않는다.
이 대화 기록은 자동으로 공유되지 않으며 `SESSION_CONTEXT.md`와 SQLite 이력을 매 분석 요청에 포함한다.

```powershell
.\.venv\Scripts\python.exe advisor.py login        # 브라우저: Continue with ChatGPT → 로그인/권한 승인
.\.venv\Scripts\python.exe advisor.py auth-status  # 토큰 값 없이 상태 확인
.\.venv\Scripts\python.exe advisor.py auth-models  # 실제 계정의 사용 가능한 모델
.\.venv\Scripts\python.exe advisor.py auth-test    # 구독으로 짧은 실제 응답 확인
.\.venv\Scripts\python.exe advisor.py logout       # 이 앱의 갱신 세션 해제
```

연결은 실제 모델 응답까지 받아야 완전히 확인된 것으로 본다. 웹 검색도 계정/모델 정책에 따라 별도 확인한다.
Windows DPAPI로 암호화한 인증정보를 `data/chatgpt/profiles.dpapi`에 저장한다. Git/공유 문맥/로그에 넣지 않는다.
`login --new-account`로 다른 계정/워크스페이스를 추가하고 `login --profile <auth-status에 나온 profile>`로 기존 등록을 선택할 수 있다.
사용량/연결 권한은 [Manage usage](https://chatgpt.com/settings/usage)에서 관리한다.

별도 API 결제를 직접 선택하려면 `OPENAI_AUTH_MODE=api_key`, `OPENAI_API_KEY`, `OPENAI_MODEL`을 명시한다.
이 모드는 ChatGPT 구독과 별도 API 과금이다. 현재 기본값은 아니다.

### 수동 실행·점검

```powershell
.\.venv\Scripts\python.exe advisor.py doctor       # 인증/의존성 확인. 네트워크 호출 없음
.\.venv\Scripts\python.exe advisor.py demo         # 합성 데이터 검증. 네트워크 불필요
.\.venv\Scripts\python.exe advisor.py analyze --country US  # 즉시 분석, 모의 계획 저장
.\.venv\Scripts\python.exe advisor.py run --once   # 한 번 점검
.\.venv\Scripts\python.exe advisor.py run          # 정기/뉴스 분석·모의 운용
.\.venv\Scripts\python.exe advisor.py status       # 저장된 모의 장부
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

화면의 기본 탭은 모의 운용이다. 기존 실제 자산 조회는 별도 화면으로 유지된다.
Streamlit만 수동 실행하면 화면만 열린다. `실행.cmd`는 별도의 로컬 실행기에서 화면과 운용 루프를 함께 시작한다.
PC가 꺼지거나 프로세스가 종료되면 감시도 중단된다. 실행기의 상태/요청 파일은 무시 경로인 `data/runtime/`에 둔다.

## 기본 동작

- 가상 원금 50만 원, 종목당 최대 40만 원, 예비 현금 10만 원, 보유 최대 2종목.
- 시장별 정규장 시작 **60분 전 사전 분석**, **개장 10분 후 재확인**을 각각 일 1회 수행한다.
  국내/미국의 일정은 서로 다르며 서머타임은 캘린더를 따른다. 해당 시간에 프로그램이 실행 중이어야 한다.
  장전에는 밤사이 뉴스·실적과 전일 확정 차트로 준비하고 개장 후 새 시세로 진입 조건을 재검토한다.
  장전 현재가가 없으면 전일 확정 종가를 참고값으로 명시하며, 장전 계획만으로 모의 진입하지 않는다.
- 중요 뉴스는 정규장 1시간 전부터 마감 2시간 후까지 60분 간격으로 확인한다.
  기본 하루 뉴스 조회 합계 6회, 새 중요 뉴스 재분석 최대 2회/시장일.
- 발표시각이 확인되는 최근 6시간 이내 사건과 검색 출처만 사용하고, URL 추적 파라미터를 제거해 중복 확인한다.
  이는 웹 검색에 기반한 주기적 확인이며 속보 푸시/모든 뉴스의 실시간 탐지를 보장하지 않는다.
- 목표가 청산 후 추가 후보 분석 최대 1회/시장일. 한국시간 기준 신규 진입 합계 최대 2회.
- 재시도는 기본 5분 이후. 분석 실패까지 포함해 시장일당 최대 8회로 제한한다.
- 목표가/철회 가격은 60초마다 현재가와 비교한다. 이 비교에는 LLM을 호출하지 않는다.
- 토스 캘린더로 휴장·서머타임·조기폐장을 처리한다. 미국 소수점 접수 종료(정규장 마감 1시간 전)도 반영한다.
- 진입 구간/유효기간을 벗어나거나 5분 넘게 지연된 시세이면 모의 체결하지 않는다.
  장전 분석용 전일 종가 참고값을 체결 가격으로 사용하지 않는다.
- 슬리피지 기본 편도 10bp, 수수료 기본 편도 10bp를 반영한다. 실제 증권사/종목별 세금·제비용과는 다르다.
- 국내는 정수 주, 미국은 소수점 6자리까지 내림 처리한다.
- 분석 실패 시 해당 시장 신규 진입을 중지한다. 기존 모의 포지션의 목표/철회 감시는 계속한다.
- 청산한 종목은 24시간 안에 다시 매수하지 않는다.

모든 값은 `strategy.json`에서 조정한다. 재시작 시 적용하며, 변경 전 계획으로 신규 진입하지 않는다.
기존 장부의 원금은 설정 변경으로 초기화되지 않는다. 새로운 실험은 다른 `database` 경로를 사용한다.

## 시장 탐색 범위와 한계

미국은 토스 `stocks/all?market=NASDAQ`으로 거래 가능한 활성 보통주 목록을 일 단위로 캐시하고,
전체 목록의 현재가를 200개씩 조회한다. 거래대금·상승률·거래량 랭킹의 합집합에서 최대 60개를 선별한다.
후보와 보유 종목의 확정 일봉 최대 200개를 분석하고, 상위 12개를 OpenAI 기업/뉴스 검토에 전달한다.
유니버스 수·시세 조회 수·차트 분석 수·최종 후보 수·결측 수를 각각 기록한다.
**전 종목의 기업 실사를 수행하는 것이 아니다. 랭킹 후보 밖의 기회는 놓칠 수 있다.**

국내 대상은 삼성전자(005930)와 SK하이닉스(000660)다. 코인 데이터/거래소 어댑터는 아직 없다.
지표는 SMA20/60/200(충분한 봉이 있을 때), Wilder RSI14/ATR14, 5/20일 수익률,
상대 거래량·평균 거래대금·20일 고저점이다. 진행 중인 일봉은 제외한다.
호가/체결·유동성에 따른 실제 슬리피지, 세금, OpenAI API 이용료는 모의 수익률에 완전히 반영되지 않는다.
따라서 모의 장부의 수익률은 실제 투자 성과나 검증된 전략 수익률이 아니다.

## 여러 AI 세션이 공유하기

```powershell
.\.venv\Scripts\python.exe advisor.py context --out data/shared_context.json
```

다른 AI가 `AGENTS.md` → `strategy.json` → `SESSION_CONTEXT.md` → 위 JSON을 읽으면
동일한 규칙·가상 잔고·판단 이력으로 이어갈 수 있다. JSON은 출력 시점의 스냅샷이므로 새 작업 전에 다시 생성한다.
운용 기록은 `data/paper.sqlite3`가 기준이며 SQLite WAL/transaction과 단일 작성자 lease를 사용한다.
여러 AI가 규칙을 읽는 것과 여러 프로세스가 동시에 매매를 기록하는 것은 구분한다.
프로세스가 비정상 종료되면 작성자 lease가 만료될 때까지 최대 30분 대기할 수 있다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

가짜 HTTP/시세와 임시 SQLite만 사용한다. 테스트에서 토스 실계좌나 유료 OpenAI API를 호출하지 않는다.
`demo`의 가격/뉴스/수익은 합성 시나리오이며 실제 투자 결과가 아니다.

공식 사양: [토스 OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json),
[OpenAI Responses 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs),
[OpenAI 웹 검색](https://developers.openai.com/api/docs/guides/tools-web-search).
