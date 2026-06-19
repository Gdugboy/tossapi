"""디자인 토큰 + Streamlit CSS — DESIGN.md(Binance-design-analysis) 기반.

UI 작업은 이 파일의 토큰을 import 해서 쓴다. 색·간격·라운드 값을 화면 코드에
하드코딩하지 말 것. 새 컴포넌트가 필요하면 DESIGN.md를 먼저 보고 토큰을 늘린다.
"""

from __future__ import annotations

# ── 색상 (DESIGN.md colors) ─────────────────────────────────────
PRIMARY = "#FCD535"          # Binance Yellow — primary CTA·accent·value-claim
PRIMARY_ACTIVE = "#f0b90b"   # press/hover
CANVAS_DARK = "#0b0e11"      # 페이지 바닥
SURFACE_CARD = "#1e2329"     # 카드·테이블 표면
SURFACE_ELEVATED = "#2b3139" # 한 단계 위 + 하어라인
BODY = "#eaecef"             # 본문 텍스트(다크)
MUTED = "#707a8a"            # 캡션·컬럼헤더
INK = "#181a20"              # 노란 버튼 위 검은 텍스트
ON_PRIMARY = "#181a20"
HAIRLINE = "#2b3139"
UP = "#0ecb81"               # trading-up (상승) — 텍스트 컬러로만
DOWN = "#f6465d"             # trading-down (하락)

# ── 라운드 (DESIGN.md rounded) ──────────────────────────────────
R_MD = "6px"     # 버튼·인풋
R_LG = "8px"     # 카드
R_XL = "12px"    # 엘리베이티드 컨테이너


def pnl_color(value: float) -> str:
    """손익 부호에 맞는 트레이딩 컬러. 0은 본문색."""
    if value > 0:
        return UP
    if value < 0:
        return DOWN
    return BODY


# ── Streamlit 주입용 CSS ────────────────────────────────────────
CSS = f"""
<style>
  .stApp {{ background: {CANVAS_DARK}; }}

  h1, h2, h3 {{ color: {BODY}; font-weight: 600; letter-spacing: -0.3px; }}

  /* 숫자는 탭ular 모노 느낌(BinancePlex 대용) */
  [data-testid="stMetricValue"] {{
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }}
  [data-testid="stMetric"] {{
    background: {SURFACE_CARD};
    border: 1px solid {HAIRLINE};
    border-radius: {R_XL};
    padding: 16px 20px;
  }}

  /* 1차 버튼: 노랑 + 검정 텍스트 (시스템 시그니처) */
  .stButton > button {{
    background: {PRIMARY};
    color: {ON_PRIMARY};
    border: none;
    border-radius: {R_MD};
    font-weight: 600;
  }}
  .stButton > button:hover {{ background: {PRIMARY_ACTIVE}; color: {ON_PRIMARY}; }}
  .stButton > button:active {{ background: {PRIMARY_ACTIVE}; }}

  /* 사이드바 표면 */
  section[data-testid="stSidebar"] {{ background: {SURFACE_CARD}; }}

  /* 캡션 muted */
  [data-testid="stCaptionContainer"], .stCaption {{ color: {MUTED}; }}

  /* 데이터프레임 라운드 */
  [data-testid="stDataFrame"] {{ border-radius: {R_LG}; overflow: hidden; }}

  /* 구분선 하어라인 */
  hr {{ border-color: {HAIRLINE}; }}
</style>
"""


def brand_header(title: str, subtitle: str = "") -> str:
    """노란 액센트가 들어간 페이지 헤더 마크업."""
    sub = (
        f'<div style="color:{MUTED};font-size:13px;margin-top:2px">{subtitle}</div>'
        if subtitle
        else ""
    )
    return (
        f'<div style="padding:8px 0 16px">'
        f'<span style="color:{PRIMARY};font-size:28px;font-weight:700;'
        f'letter-spacing:-0.5px">{title}</span>{sub}</div>'
    )
