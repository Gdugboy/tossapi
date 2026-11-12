"""한 번 실행하면 로컬 화면과 모의 운용기를 함께 시작한다."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser

from runtime_control import InstanceLock, RUNTIME, ROOT, now_iso, read_json, request, status, write_json

REQUIRED = ("requests", "dotenv", "streamlit", "pandas", "tzdata", "jwt", "cryptography")


def prepare_python():
    """처음 실행할 때만 가상환경과 누락된 의존성을 준비한다."""
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("FOMO 실행 환경을 준비합니다.", flush=True)
        import venv
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(ROOT / ".venv")
    check = "import importlib.util; raise SystemExit(not all(importlib.util.find_spec(x) for x in " + repr(REQUIRED) + "))"
    if subprocess.run([str(python), "-c", check], cwd=ROOT).returncode:
        print("필요한 라이브러리를 설치합니다. 첫 실행에서만 잠시 기다려 주세요.", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], cwd=ROOT, check=True)
    return python


def dashboard_ready(url):
    try:
        with urllib.request.urlopen(url + "/_stcore/health", timeout=0.5) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def free_port():
    with socket.socket() as server:
        try:
            server.bind(("127.0.0.1", 8501))
        except OSError:
            server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


class Service:
    def __init__(self, start_worker=True):
        self.start_worker = start_worker
        self.worker = None
        self.worker_stop = threading.Event()
        self.state_lock = threading.Lock()
        self.data = {"instance": uuid.uuid4().hex, "pid": os.getpid(), "mode": "paper",
                     "service": "starting", "worker": "stopped", "dashboard": "starting",
                     "started_at": now_iso(), "error": None}

    def update(self, **values):
        with self.state_lock:
            self.data.update(values)

    def publish(self):
        with self.state_lock:
            write_json(RUNTIME / "status.json", {**self.data, "updated_at": now_iso()})

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker_stop = threading.Event()
        self.update(worker="starting", error=None)
        self.worker = threading.Thread(target=self.work, name="paper-runner", daemon=True)
        self.worker.start()

    def pause(self):
        if self.worker and self.worker.is_alive():
            self.update(worker="stopping")
            self.worker_stop.set()

    def work(self):
        runner = None
        try:
            from advisor import load_environment, make_runner
            from trader_config import Settings
            load_environment()
            runner = make_runner(Settings.load())
            if self.worker_stop.is_set():
                return
            self.update(worker="running")
            while not self.worker_stop.is_set():
                self.update(activity="checking", tick_started_at=now_iso())
                summary = runner.tick()
                self.update(activity="waiting", last_tick_at=now_iso(), last_summary=summary)
                self.worker_stop.wait(runner.settings.poll_seconds)
        except Exception as exc:
            # 원격 오류 본문이나 설정값을 로그에 복사하지 않는다.
            self.update(worker="error", activity="idle",
                        error=f"운용 시작/점검 실패 ({type(exc).__name__}). 로그인·키 설정을 확인한 뒤 다시 시작하세요.")
        finally:
            if runner:
                runner.state.release(runner.owner)
            with self.state_lock:
                if self.data["worker"] != "error":
                    self.data.update(worker="stopped", activity="idle")

    def run(self):
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        self.update(url=url)
        self.publish()
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        child = None
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                 "--server.address", "127.0.0.1", "--server.port", str(port),
                 "--server.headless", "true", "--browser.gatherUsageStats", "false",
                 "--server.fileWatcherType", "none"],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 45
            while not dashboard_ready(url):
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("화면을 시작하지 못했습니다. 실행 파일을 다시 열어 주세요.")
                self.publish()
                time.sleep(0.5)
            self.update(service="running", dashboard="ready")
            if self.start_worker:
                self.start()
            last_request = None
            shutting_down = False
            while True:
                self.publish()
                command = read_json(RUNTIME / "request.json")
                if command.get("instance") == self.data["instance"] and command.get("id") != last_request:
                    last_request = command.get("id")
                    if command.get("action") == "shutdown":
                        shutting_down = True
                        self.update(service="stopping")
                        self.pause()
                    elif not shutting_down and command.get("action") == "start":
                        self.start()
                    elif command.get("action") == "pause":
                        self.pause()
                if child.poll() is not None:
                    shutting_down = True
                    self.update(service="stopping", dashboard="stopped")
                    self.pause()
                if shutting_down and not (self.worker and self.worker.is_alive()):
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            self.pause()
        except Exception as exc:
            self.update(error=f"실행기 오류 ({type(exc).__name__}). 실행 파일을 다시 열어 주세요.")
        finally:
            self.pause()
            # 진행 중 분석이 끝나고 lease를 해제한 뒤 자식 화면을 종료한다.
            while self.worker and self.worker.is_alive():
                self.update(service="stopping")
                self.publish()
                self.worker.join(timeout=1)
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            self.update(service="stopped", dashboard="stopped")
            self.publish()


def launch(start_worker=True, open_browser=True):
    current = status()
    previous_instance = current.get("instance")
    spawned = not current["alive"]
    if current["alive"]:
        if start_worker:
            request("start")
    else:
        python = prepare_python()
        background_python = python.with_name("pythonw.exe") if os.name == "nt" else python
        if not background_python.exists():
            background_python = python
        command = [str(background_python), str(ROOT / "launch.py"), "--serve"]
        if not start_worker:
            command.append("--no-worker")
        subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ, "PYTHONUTF8": "1"},
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        current = status()
        if spawned and current.get("instance") == previous_instance:
            time.sleep(0.5)
            continue
        if current["alive"] and current.get("dashboard") == "ready":
            if open_browser:
                webbrowser.open(current["url"])
            print("FOMO 실행 완료: " + current["url"] + " · 모의 운용 / 실제 자산 조회", flush=True)
            return 0
        if current.get("service") == "stopped" and current.get("error"):
            print(current["error"], flush=True)
            return 1
        time.sleep(0.5)
    print("실행을 확인하지 못했습니다. 잠시 후 실행 파일을 다시 열어 주세요.", flush=True)
    return 1


def main():
    parser = argparse.ArgumentParser(description="FOMO 한 번에 실행")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stop:
        request("shutdown")
        return 0
    if args.serve:
        try:
            with InstanceLock():
                Service(start_worker=not args.no_worker).run()
        except RuntimeError:
            pass  # 다른 실행기를 유지한다. 임의 PID/포트를 종료하지 않는다.
        return 0
    return launch(not args.no_worker, not args.no_browser)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"실행 환경 준비 실패 ({type(exc).__name__}). Python 설치와 인터넷 연결을 확인하세요.")
        raise SystemExit(1)
