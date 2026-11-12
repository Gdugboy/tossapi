"""실제 시세/AI 호출 없이 실행기의 중복·제어·중지 동작을 검증한다."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import launch
from runtime_control import InstanceLock, now_iso, read_json, request, status, write_json


class RuntimeTests(unittest.TestCase):
    def test_stale_status_cannot_receive_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old = datetime.now(timezone.utc) - timedelta(minutes=1)
            write_json(directory / "status.json", {"service": "running", "instance": "old", "updated_at": old.isoformat()})
            self.assertFalse(status(directory)["alive"])
            self.assertFalse(request("start", directory))
            self.assertFalse((directory / "request.json").exists())

    def test_command_is_bound_to_current_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "status.json", {"service": "running", "instance": "current", "updated_at": now_iso()})
            self.assertTrue(request("pause", directory))
            command = read_json(directory / "request.json")
            self.assertEqual((command["instance"], command["action"]), ("current", "pause"))
            with self.assertRaises(ValueError):
                request("buy", directory)

    def test_only_one_instance_can_hold_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            with InstanceLock(temporary):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(temporary):
                        self.fail("중복 실행 잠금 실패")
            with InstanceLock(temporary):
                pass

    def test_stop_waits_for_current_tick_then_releases_lease(self):
        started, finish = threading.Event(), threading.Event()
        runner = Mock(owner="test-owner")
        runner.settings.poll_seconds = 60

        def tick():
            started.set()
            self.assertTrue(finish.wait(5))
            return {"status": "paper", "errors": []}

        runner.tick.side_effect = tick
        service = launch.Service()
        with patch("advisor.load_environment"), patch("advisor.make_runner", return_value=runner):
            service.start()
            try:
                self.assertTrue(started.wait(5))
                original = service.worker
                service.start()
                self.assertIs(service.worker, original)
                service.pause()
                self.assertTrue(service.worker.is_alive())
                self.assertEqual(service.data["worker"], "stopping")
            finally:
                finish.set()
                service.worker.join(5)
        self.assertFalse(service.worker.is_alive())
        self.assertEqual(service.data["worker"], "stopped")
        runner.tick.assert_called_once()
        runner.state.release.assert_called_once_with("test-owner")

    def test_error_does_not_copy_secret_values_into_status(self):
        service = launch.Service()
        with patch("advisor.load_environment"), patch("advisor.make_runner", side_effect=RuntimeError("private-token")):
            service.work()
        self.assertEqual(service.data["worker"], "error")
        self.assertNotIn("private-token", repr(service.data))


if __name__ == "__main__":
    unittest.main()
