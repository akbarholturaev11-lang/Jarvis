from __future__ import annotations

import stat
import runpy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import actions.reminder as reminder_module


class ReminderTests(unittest.TestCase):
    def test_device_profile_platform_wins_over_host_platform(self):
        profile = {"platform": {"os": "macos"}}

        with patch.object(reminder_module.platform, "system", return_value="Windows"):
            self.assertEqual(reminder_module.resolve_reminder_os(profile), "macos")

    def test_supported_device_profile_platforms_are_normalized(self):
        for raw, expected in (
            ("mac", "macos"),
            ("macos", "macos"),
            ("windows", "windows"),
            ("linux", "linux"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    reminder_module.resolve_reminder_os({"platform": {"os": raw}}),
                    expected,
                )

    def test_missing_profile_uses_direct_platform_detection(self):
        with patch.object(reminder_module.platform, "system", return_value="Darwin"):
            self.assertEqual(reminder_module.resolve_reminder_os(), "macos")

    def test_unknown_device_profile_does_not_guess_a_scheduler(self):
        profile = {"platform": {"os": "unknown"}}

        self.assertEqual(reminder_module.resolve_reminder_os(profile), "unknown")
        result = reminder_module.reminder(
            {
                "date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
                "time": "12:00",
                "message": "Test",
            },
            device_profile=profile,
        )
        self.assertIn("couldn't determine", result.lower())

    def test_mac_script_is_valid_private_and_uses_safe_voice_fallback(self):
        task_name = "JARVISReminder_20260711_120000_a1b2c3"
        message = 'Dori ich; $(touch /tmp/nope) "quoted"\nnext'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(reminder_module, "_scripts_dir", return_value=root):
                script_path = reminder_module._write_notify_script(
                    task_name,
                    message,
                    "macos",
                )

            source = script_path.read_text(encoding="utf-8")
            compile(source, str(script_path), "exec")

            self.assertIn('["/usr/bin/say", spoken_message]', source)
            self.assertIn('["/usr/bin/osascript", "-e", script]', source)
            self.assertIn("os.replace(event_path, fallback_path)", source)
            self.assertIn('["/bin/launchctl", "remove", launch_label]', source)
            self.assertIn("launch_agent_path.unlink", source)
            self.assertNotIn("shell=True", source)
            self.assertEqual(stat.S_IMODE(script_path.stat().st_mode), 0o600)

    def test_mac_system_fallback_uses_argv_without_shell(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        runner = Mock(return_value=completed)

        with (
            patch.object(reminder_module.Path, "exists", return_value=True),
            patch.object(reminder_module.subprocess, "run", runner),
        ):
            ok, detail = reminder_module.speak_reminder_fallback(
                'Dori ich; $(touch /tmp/nope)',
                "macos",
            )

        self.assertTrue(ok, detail)
        command = runner.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/say")
        self.assertIn("$(touch /tmp/nope)", command[1])
        self.assertNotIn("shell", runner.call_args.kwargs)

    def test_generated_script_speaks_even_when_notification_succeeds(self):
        task_name = "JARVISReminder_20260711_120000_a1b2c3"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        runner = Mock(return_value=completed)
        notification = SimpleNamespace(notify=Mock())
        plyer = ModuleType("plyer")
        plyer.notification = notification

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(reminder_module, "_scripts_dir", return_value=root):
                script_path = reminder_module._write_notify_script(
                    task_name,
                    "Suv ich",
                    "macos",
                )

            with (
                patch.dict(sys.modules, {"plyer": plyer}),
                patch.object(Path, "home", return_value=root),
                patch.object(reminder_module.subprocess, "run", runner),
                patch("time.monotonic", side_effect=[0.0, 3.0]),
            ):
                runpy.run_path(str(script_path), run_name="__main__")

            notification.notify.assert_called_once()
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertIn(["/usr/bin/say", "Akbar. Suv ich"], commands)
            self.assertFalse(script_path.exists())

    def test_mac_profile_routes_only_to_launch_agent_scheduler(self):
        future = datetime.now() + timedelta(days=1)
        fake_script = Path("/tmp/test-reminder.py")

        with (
            patch.object(reminder_module, "_write_notify_script", return_value=fake_script),
            patch.object(reminder_module, "_schedule_mac", return_value="mac-job") as mac,
            patch.object(reminder_module, "_schedule_windows") as windows,
            patch.object(reminder_module, "_schedule_linux") as linux,
            patch.object(reminder_module.secrets, "token_hex", return_value="a1b2c3"),
        ):
            result = reminder_module.reminder(
                {
                    "date": future.strftime("%Y-%m-%d"),
                    "time": future.strftime("%H:%M"),
                    "message": "Dori ich",
                },
                device_profile={"platform": {"os": "macos"}},
            )

        self.assertTrue(result.startswith("Reminder set"))
        mac.assert_called_once()
        windows.assert_not_called()
        linux.assert_not_called()

    def test_linux_at_command_quotes_script_paths(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        runner = Mock(return_value=completed)

        def which(name):
            return "/usr/bin/at" if name == "at" else None

        with (
            patch.object(reminder_module.shutil, "which", side_effect=which),
            patch.object(reminder_module.subprocess, "run", runner),
        ):
            job_id = reminder_module._schedule_linux(
                datetime.now() + timedelta(days=1),
                "test-job",
                Path("/tmp/reminder scripts/test.py"),
            )

        self.assertEqual(job_id, "test-job")
        self.assertIn("'/tmp/reminder scripts/test.py'", runner.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
