import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


def _load_module_from_code(name: str, code: str):
    with tempfile.TemporaryDirectory() as temp_dir:
        module_path = Path(temp_dir) / f"{name}.py"
        module_path.write_text(code, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        if not spec or not spec.loader:
            raise RuntimeError(f"Unable to load module spec for {name}")
        spec.loader.exec_module(module)
        return module


class GSLPythonImportTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("GSLPython", None)

    def test_import_auto_accelerates_functions_and_classes(self):
        module = _load_module_from_code(
            "target_module_a",
            textwrap.dedent(
                """
                import GSLPython

                def add(a, b):
                    return a + b

                class Math:
                    def mul(self, a, b):
                        return a * b
                """
            ),
        )

        self.assertTrue(getattr(module.add, "__gslpython_accelerated__", False))
        self.assertTrue(getattr(module.Math.mul, "__gslpython_accelerated__", False))
        self.assertEqual(module.add(2, 3), 5)
        self.assertEqual(module.Math().mul(2, 3), 6)

    def test_importer_module_is_detected(self):
        module = _load_module_from_code(
            "target_module_b",
            textwrap.dedent(
                """
                import GSLPython

                REPORT = GSLPython.get_last_report()
                """
            ),
        )
        self.assertEqual(module.REPORT.module_name, "target_module_b")


if __name__ == "__main__":
    unittest.main()
