"""이전 import 경로 호환. 공통 설정은 trader_config.py에서 관리한다."""
from trader_config import ROOT, Settings


def load_watchlist():
    path = ROOT / "watchlist.txt"
    return [v for line in path.read_text(encoding="utf-8").splitlines() if (v := line.split("#", 1)[0].strip())] if path.exists() else []
