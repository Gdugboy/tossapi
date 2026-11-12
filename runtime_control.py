"""로컬 실행기의 상태와 제어 요청. 인증정보나 실제 계좌는 저장하지 않는다."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "data" / "runtime"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def status(directory=RUNTIME):
    value = read_json(Path(directory) / "status.json")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["updated_at"])).total_seconds()
        alive = -5 <= age <= 15 and value.get("service") in {"starting", "running", "stopping"}
    except (KeyError, ValueError, TypeError):
        alive = False
    return {**value, "alive": alive}


def request(action, directory=RUNTIME):
    if action not in {"start", "pause", "shutdown"}:
        raise ValueError("지원하지 않는 실행 요청입니다.")
    current = status(directory)
    if not current["alive"]:
        return False
    write_json(Path(directory) / "request.json", {
        "id": uuid.uuid4().hex, "instance": current["instance"], "action": action,
    })
    return True


class InstanceLock:
    """PID를 종료하는 대신 OS 파일 잠금으로 중복 실행을 막는다."""

    def __init__(self, directory=RUNTIME):
        self.path = Path(directory) / "instance.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("이미 실행 중입니다.") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.close()
