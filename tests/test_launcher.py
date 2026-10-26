"""Launcher integration tests using disposable app bundles and account data."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(os.environ.get("LAUNCHER_SCRIPT", Path(__file__).resolve().parents[1] / "codex-multi-account.sh"))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multi-account-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = self.root / "primary"
        self.secondary = self.root / "secondary"
        self.profile = self.root / "Chromium Profile"
        self.registry = self.root / "registry"
        self.app = self.root / "ChatGPT Test.app"
        self.primary.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CODEX_") and key != "CHATGPT_APP"
        }
        self.env.update({
            "CODEX_PRIMARY_HOME": str(self.primary),
            "CODEX_SECONDARY_HOME": str(self.secondary),
            "CODEX_PROFILE_DIR": str(self.profile),
            "CODEX_MULTI_REGISTRY_DIR": str(self.registry),
            "CHATGPT_APP": str(self.app),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        })
        self.make_executable(self.app / "Contents/MacOS/ChatGPT", "#!/bin/sh\nexit 0\n")
        (self.primary / "state_5.sqlite").write_text("primary state")

    @staticmethod
    def make_executable(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
        return path

    def add_cli(self, layout="new", details=None):
        relative = "codex-cli/bin/codex" if layout == "new" else "codex"
        cli = self.app / "Contents/Resources" / relative
        if details is None:
            details = {"state DB": str(self.primary / "state_5.sqlite") + " (file)"}
        doctor_file = cli.parent / "doctor.json"
        calls_file = cli.parent / "calls.log"
        self.make_executable(cli, "#!/bin/sh\n"
                             + "printf '%s\\n' \"$CODEX_HOME|$*\" >> " + shlex.quote(str(calls_file)) + "\n"
                             + "cat " + shlex.quote(str(doctor_file)) + "\n")
        doctor_file.write_text(json.dumps({"checks": {"state.paths": {"details": details}}}))
        return cli

    def run_launcher(self, *args, expected=0):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--no-launch", *args],
            env=self.env, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def assert_link(self, name):
        link = self.secondary / name
        self.assertTrue(link.is_symlink(), str(link))
        self.assertEqual(os.readlink(link), str(self.primary / name))

    def test_new_bundle_layout(self):
        cli = self.add_cli()
        self.run_launcher("--symlink-history")
        self.assert_link("state_5.sqlite")
        self.assertIn(str(self.primary) + "|doctor --json", (cli.parent / "calls.log").read_text())

    def test_legacy_bundle_layout(self):
        cli = self.add_cli("legacy")
        self.run_launcher("--symlink-history")
        self.assert_link("state_5.sqlite")
        self.assertTrue((cli.parent / "calls.log").exists())

    def test_new_cli_preferred_when_both_exist(self):
        new = self.add_cli()
        old = self.add_cli("legacy")
        self.run_launcher("--symlink-history")
        self.assertTrue((new.parent / "calls.log").exists())
        self.assertFalse((old.parent / "calls.log").exists())

    def test_non_executable_new_cli_falls_back(self):
        new = self.add_cli()
        old = self.add_cli("legacy")
        new.chmod(0o644)
        self.run_launcher("--symlink-history")
        self.assertTrue((old.parent / "calls.log").exists())

    def test_directory_at_new_cli_path_falls_back(self):
        directory = self.app / "Contents/Resources/codex-cli/bin/codex"
        directory.mkdir(parents=True)
        old = self.add_cli("legacy")
        self.run_launcher("--symlink-history")
        self.assertTrue((old.parent / "calls.log").exists())

    def test_directory_is_not_a_cli(self):
        (self.app / "Contents/Resources/codex-cli/bin/codex").mkdir(parents=True)
        result = self.run_launcher(expected=1)
        self.assertIn("bundled Codex CLI not executable", result.stderr)
        self.assertFalse(self.secondary.exists())

    def test_missing_cli_fails_before_creating_profiles(self):
        result = self.run_launcher(expected=1)
        self.assertIn("bundled Codex CLI not executable", result.stderr)
        self.assertFalse(self.secondary.exists())
        self.assertFalse(self.profile.exists())

    def test_default_setup_does_not_share_history_or_auth(self):
        self.add_cli()
        (self.primary / "auth.json").write_text("test-only-primary-auth")
        self.run_launcher()
        self.assertTrue(self.secondary.is_dir())
        self.assertTrue(self.profile.is_dir())
        self.assertFalse((self.secondary / "auth.json").exists())
        self.assertFalse((self.secondary / "state_5.sqlite").exists())
        self.assertFalse(any(path.is_symlink() for path in self.secondary.iterdir()))

    def test_dry_run_leaves_profiles_and_primary_unchanged(self):
        self.add_cli()
        before = sorted(path.name for path in self.primary.iterdir())
        self.run_launcher("--dry-run", "--symlink-history")
        self.assertFalse(self.secondary.exists())
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.registry.exists())
        self.assertEqual(before, sorted(path.name for path in self.primary.iterdir()))

    def test_legacy_app_option(self):
        self.add_cli("legacy")
        self.env.pop("CHATGPT_APP")
        self.run_launcher("--codex-app", str(self.app))

    def test_bundle_executable_from_plist(self):
        self.add_cli()
        executable = self.app / "Contents/MacOS/ChatGPT"
        executable.rename(executable.with_name("CustomDesktop"))
        (self.app / "Contents/Info.plist").write_text(
            '<?xml version="1.0"?><plist version="1.0"><dict>'
            '<key>CFBundleExecutable</key><string>CustomDesktop</string></dict></plist>'
        )
        if os.uname().sysname != "Darwin":
            self.skipTest("requires macOS plutil")
        self.run_launcher()

    def test_reset_does_not_require_installed_app(self):
        self.secondary.mkdir()
        self.profile.mkdir()
        self.env["CHATGPT_APP"] = str(self.root / "missing.app")
        self.run_launcher("--reset")
        self.assertFalse(self.secondary.exists())
        self.assertFalse(self.profile.exists())
        self.assertTrue(self.primary.exists())


if __name__ == "__main__":
    unittest.main()
