import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


def _load_module_from_code(name: str, code: str):
    temp_dir = tempfile.TemporaryDirectory()
    module_path = Path(temp_dir.name) / f"{name}.py"
    module_path.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, module_path)
    if not spec or not spec.loader:
        temp_dir.cleanup()
        raise RuntimeError(f"Unable to load module spec for {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__gslpython_temp_dir__ = temp_dir
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

    def test_compiled_members_replace_runtime_namespace(self):
        module = _load_module_from_code(
            "target_module_c",
            textwrap.dedent(
                """
                import GSLPython, types

                def _fake_compiler(_module_name, _module_file):
                    compiled = types.ModuleType("_fake_compiled_module")

                    def add(a, b):
                        # Intentionally different behavior to verify replacement happened.
                        return a - b

                    class Math:
                        def mul(self, a, b):
                            # Intentionally different behavior to verify replacement happened.
                            return a + b

                    compiled.add = add
                    compiled.Math = Math
                    return compiled

                GSLPython._compile_importer_module = _fake_compiler

                def add(a, b):
                    return a + b

                class Math:
                    def mul(self, a, b):
                        return a * b
                """
            ),
        )

        # These values confirm fake compiled members replaced the original runtime definitions.
        self.assertEqual(module.add(5, 3), 2)
        self.assertEqual(module.Math().mul(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
