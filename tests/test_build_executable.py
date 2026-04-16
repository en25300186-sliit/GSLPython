"""Tests for GSLPython.build_executable (AOT compilation)."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class BuildExecutableTests(unittest.TestCase):
    def _write_script(self, tmp_dir: str, code: str) -> Path:
        script = Path(tmp_dir) / "hello.py"
        script.write_text(textwrap.dedent(code), encoding="utf-8")
        return script

    def test_build_produces_binary(self):
        import GSLPython

        with tempfile.TemporaryDirectory() as tmp:
            script = self._write_script(
                tmp,
                """
                def greet(name):
                    return f"Hello, {name}!"

                if __name__ == "__main__":
                    print(greet("world"))
                """,
            )
            binary = GSLPython.build_executable(script)
            self.assertTrue(os.path.isfile(binary), f"Binary not found: {binary}")
            self.assertTrue(os.access(binary, os.X_OK), "Binary is not executable")

    def test_build_output_contains_expected_text(self):
        import GSLPython

        with tempfile.TemporaryDirectory() as tmp:
            script = self._write_script(
                tmp,
                """
                if __name__ == "__main__":
                    print("GSLPython_AOT_OK")
                """,
            )
            binary = GSLPython.build_executable(script)
            result = subprocess.run([binary], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertIn("GSLPython_AOT_OK", result.stdout)

    def test_build_custom_output_path(self):
        import GSLPython

        with tempfile.TemporaryDirectory() as tmp:
            script = self._write_script(
                tmp,
                """
                if __name__ == "__main__":
                    pass
                """,
            )
            ext = ".exe" if sys.platform == "win32" else ".out"
            custom = Path(tmp) / f"my_program{ext}"
            binary = GSLPython.build_executable(script, custom)
            self.assertEqual(binary, str(custom))
            self.assertTrue(custom.is_file())

    def test_build_missing_source_raises(self):
        import GSLPython

        with self.assertRaises(FileNotFoundError):
            GSLPython.build_executable("/nonexistent/path/missing.py")


if __name__ == "__main__":
    unittest.main()
