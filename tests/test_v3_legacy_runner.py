"""The retired legacy chain runner must fail closed and never compute.

scripts/v3_refresh_chain.py used to drive paid Dune queries directly. It is now
a tiny deprecation CLI: it must exit nonzero, point at the unified runner, and
perform zero network or subprocess work. These tests run it as a real
subprocess with the network and process-spawn APIs hard-blocked, and with
sentinel executables on PATH, so any silent paid path is caught.

No network and no Dune: everything below is a local subprocess.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "scripts/v3_refresh_chain.py"
UNIFIED = "scripts/v3_refresh_dashboard.py"


class LegacyRunnerTests(unittest.TestCase):
    def test_source_has_no_network_or_spawn_capability(self):
        source = LEGACY.read_text()
        for token in ("urllib", "subprocess", "socket", "api.dune.com",
                      "os.environ", "requests", "DUNE_API_KEY"):
            with self.subTest(token=token):
                self.assertNotIn(token, source,
                                 f"retired runner still carries capability: {token}")

    def test_subprocess_run_fails_closed_and_links_the_unified_runner(self):
        env = {k: v for k, v in os.environ.items() if k != "DUNE_API_KEY"}
        with tempfile.TemporaryDirectory() as empty_bin:
            env["PATH"] = empty_bin  # no dune/bash/git reachable
            result = subprocess.run([sys.executable, str(LEGACY)], cwd=ROOT,
                                    capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "legacy runner must not compute or print results")
        self.assertIn(UNIFIED, result.stderr)
        self.assertIn("retired", result.stderr.lower())

    def test_network_and_spawn_apis_are_never_attempted(self):
        driver = textwrap.dedent(f"""
            import runpy, socket, subprocess, sys
            attempts = []
            def deny(name):
                def blocked(*args, **kwargs):
                    attempts.append(name)
                    raise AssertionError("blocked " + name)
                return blocked
            socket.socket = deny("socket.socket")
            socket.create_connection = deny("socket.create_connection")
            subprocess.run = deny("subprocess.run")
            subprocess.Popen = deny("subprocess.Popen")
            sys.argv = [{str(LEGACY)!r}]
            code = 0
            try:
                runpy.run_path({str(LEGACY)!r}, run_name="__main__")
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            sys.stderr.write("BLOCKED=" + ",".join(attempts) + "\\n")
            raise SystemExit(code)
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "driver.py"
            path.write_text(driver)
            result = subprocess.run([sys.executable, str(path)], cwd=ROOT,
                                    capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BLOCKED=\n", result.stderr,
                      "legacy runner attempted network/spawn despite retirement")
        self.assertIn(UNIFIED, result.stderr)

    def test_sentinel_dune_bash_and_git_are_never_invoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            sentinel = Path(tmp) / "invoked"
            for name in ("dune", "bash", "git", "sh"):
                script = bindir / name
                script.write_text(f"#!/bin/sh\necho {name} >> {sentinel}\nexit 0\n")
                script.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "DUNE_API_KEY"}
            env["PATH"] = str(bindir)
            result = subprocess.run([sys.executable, str(LEGACY)], cwd=ROOT,
                                    capture_output=True, text=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(UNIFIED, result.stderr)
            self.assertFalse(sentinel.exists(),
                             "legacy runner invoked a compute/network tool")


if __name__ == "__main__":
    unittest.main()
