"""季度持仓脚本必须在任意工作目录下找到 cli，失败时非 0 且留下日志。"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "quarterly_holdings_sync.sh"


def _fake_python(directory: Path, *, fail_import: bool) -> Path:
    path = directory / "python3"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ]; then
              backend="{BACKEND}"
              ok=0
              case ":${{PYTHONPATH}}:" in
                *":${{backend}}:"*) ok=1 ;;
              esac
              if [ "$(pwd)" = "$backend" ]; then
                ok=1
              fi
              if [ "$ok" != 1 ] || [ "{int(fail_import)}" = 1 ]; then
                echo "python3: No module named cli" >&2
                exit 1
              fi
              printf '%s\\n' '{{"total": 2, "success": 1, "skip": 1, "fail": 0}}'
              exit 0
            fi
            exec /usr/bin/python3 "$@"
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class QuarterlyHoldingsSyncTests(TestCase):
    """用假解释器验证 cwd / PYTHONPATH 和失败退出码。"""

    def _run(self, python: Path, log_dir: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["IFUND_SYNC_FORCE"] = "1"
        env["IFUND_SYNC_LOG_DIR"] = str(log_dir)
        env["IFUND_PYTHON"] = str(python)
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_script_imports_cli_from_other_cwd(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run(_fake_python(root, fail_import=False), root / "logs")
            log = next((root / "logs").glob("quarterly_sync_*.log")).read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 0, log)
            self.assertIn(f"cwd={BACKEND}", log)
            self.assertIn(str(BACKEND), log)
            self.assertIn("statistics: total=2 new=1 skip=1 fail=0", log)
            self.assertIn("rc=0", log)

    def test_import_failure_is_nonzero_and_logged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run(_fake_python(root, fail_import=True), root / "logs")
            log = next((root / "logs").glob("quarterly_sync_*.log")).read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 1, log)
            self.assertIn("python3: No module named cli", log)
            self.assertIn("ERROR: holdings fetch failed rc=1", log)

    def test_shell_syntax_and_backend_cd(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('cd "$BACKEND_DIR"', text)
        self.assertIn("PYTHONPATH", text)
