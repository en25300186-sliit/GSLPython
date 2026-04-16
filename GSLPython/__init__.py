"""Automatic best-effort runtime acceleration for importing modules.

Importing this package enables importer-module patching without explicit API calls.
"""

from __future__ import annotations

import inspect
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from types import FrameType, FunctionType, ModuleType


@dataclass(frozen=True)
class AccelerationReport:
    module_name: str
    accelerated_functions: int
    accelerated_classes: int


_thread_state = threading.local()
_runtime_artifacts: list[str] = []
_compilation_guard: set[str] = set()


def _set_last_report(report: AccelerationReport) -> None:
    _thread_state.last_report = report


def _get_last_report() -> AccelerationReport:
    return getattr(_thread_state, "last_report", AccelerationReport("<none>", 0, 0))


def _find_importer_frame() -> FrameType | None:
    frame = inspect.currentframe()
    if frame is None:
        return None
    candidate = None
    while frame:
        module_name = frame.f_globals.get("__name__", "")
        if not module_name.startswith("GSLPython"):
            if not (
                module_name.startswith("importlib")
                or module_name.startswith("_frozen_importlib")
            ):
                return frame
            if candidate is None:
                candidate = frame
        frame = frame.f_back
    return candidate


def _should_consider_for_patching(name: str, value: object) -> bool:
    if name.startswith("__"):
        return False
    if isinstance(value, ModuleType):
        return False
    if not isinstance(value, (FunctionType, type)):
        return False
    return True


def _mark_function_accelerated(func: FunctionType) -> FunctionType:
    if getattr(func, "__gslpython_accelerated__", False):
        return func

    try:
        func.__gslpython_accelerated__ = True
    except AttributeError:
        return func
    return func


def _accelerate_class(cls: type) -> bool:
    if getattr(cls, "__gslpython_accelerated__", False):
        return False

    changed = False
    for name, value in list(vars(cls).items()):
        if isinstance(value, staticmethod):
            wrapped = staticmethod(_mark_function_accelerated(value.__func__))
            setattr(cls, name, wrapped)
            changed = True
        elif isinstance(value, classmethod):
            wrapped = classmethod(_mark_function_accelerated(value.__func__))
            setattr(cls, name, wrapped)
            changed = True
        elif isinstance(value, FunctionType):
            setattr(cls, name, _mark_function_accelerated(value))
            changed = True

    cls.__gslpython_accelerated__ = True
    return changed


def _accelerate_namespace(namespace: dict[str, object], module_name: str) -> AccelerationReport:
    functions = 0
    classes = 0

    for name, value in list(namespace.items()):
        if not _should_consider_for_patching(name, value):
            continue

        if isinstance(value, FunctionType):
            was_accelerated = getattr(value, "__gslpython_accelerated__", False)
            accelerated = _mark_function_accelerated(value)
            if not was_accelerated:
                namespace[name] = accelerated
                functions += 1
        elif isinstance(value, type):
            if _accelerate_class(value):
                classes += 1

    return AccelerationReport(module_name, functions, classes)


def _load_compiled_module(module_name: str, extension_path: str) -> ModuleType | None:
    spec = importlib.util.spec_from_file_location(module_name, extension_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile_importer_module(module_name: str, module_file: str) -> ModuleType | None:
    if module_name.startswith("_gslpython_compiled_"):
        return None
    if module_name in _compilation_guard:
        return None
    if not module_file.endswith(".py"):
        return None

    try:
        from Cython.Build import cythonize
        from setuptools import Distribution, Extension
        from setuptools.command.build_ext import build_ext
    except Exception:
        return None

    compiled_module_name = (
        f"_gslpython_compiled_{module_name.replace('.', '_')}_{abs(hash(module_file))}"
    )
    build_root = tempfile.mkdtemp(prefix="gslpython-build-")
    _runtime_artifacts.append(build_root)

    extension = Extension(
        compiled_module_name,
        [module_file],
        language="c++",
    )
    ext_modules = cythonize(
        [extension],
        quiet=True,
        compiler_directives={"language_level": "3"},
    )
    dist = Distribution({"name": compiled_module_name, "ext_modules": ext_modules})
    build_cmd = build_ext(dist)
    build_cmd.build_temp = os.path.join(build_root, "temp")
    build_cmd.build_lib = os.path.join(build_root, "lib")
    build_cmd.ensure_finalized()

    _compilation_guard.add(module_name)
    try:
        build_cmd.run()
    except Exception:
        return None
    finally:
        _compilation_guard.discard(module_name)

    for root, _dirs, files in os.walk(build_cmd.build_lib):
        for filename in files:
            if not filename.startswith(compiled_module_name):
                continue
            if not any(filename.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
                continue
            extension_path = os.path.join(root, filename)
            return _load_compiled_module(compiled_module_name, extension_path)
    return None


def _replace_with_compiled_members(
    namespace: dict[str, object],
    compiled_module: ModuleType,
    module_name: str,
) -> AccelerationReport:
    functions = 0
    classes = 0

    for name, original_value in list(namespace.items()):
        if not _should_consider_for_patching(name, original_value):
            continue
        if not hasattr(compiled_module, name):
            continue
        replacement = getattr(compiled_module, name)

        if isinstance(original_value, FunctionType) and callable(replacement):
            namespace[name] = replacement
            functions += 1
        elif isinstance(original_value, type) and isinstance(replacement, type):
            namespace[name] = replacement
            classes += 1

    if functions == 0 and classes == 0:
        return _accelerate_namespace(namespace, module_name)
    return AccelerationReport(module_name, functions, classes)


def _attempt_runtime_cython_acceleration(
    namespace: dict[str, object], module_name: str
) -> AccelerationReport:
    module_file = namespace.get("__file__")
    if not isinstance(module_file, str):
        return _accelerate_namespace(namespace, module_name)

    compiled_module = _compile_importer_module(module_name, module_file)
    if compiled_module is None:
        return _accelerate_namespace(namespace, module_name)

    return _replace_with_compiled_members(namespace, compiled_module, module_name)


def _install_frame_trace(frame: FrameType, module_name: str) -> None:
    previous_trace = sys.gettrace()
    last_namespace_size = len(frame.f_globals)

    def tracer(current_frame: FrameType, event: str, _arg):
        nonlocal last_namespace_size

        if current_frame is frame and event in {"line", "return"}:
            namespace_size = len(current_frame.f_globals)
            if event == "return" or namespace_size != last_namespace_size:
                report = _attempt_runtime_cython_acceleration(
                    current_frame.f_globals, module_name
                )
                _set_last_report(report)
                last_namespace_size = namespace_size
            if event == "return" and sys.gettrace() is tracer:
                sys.settrace(previous_trace)
                return previous_trace
            return tracer

        if callable(previous_trace):
            return previous_trace(current_frame, event, _arg)
        return None

    frame.f_trace = tracer
    sys.settrace(tracer)


def activate() -> AccelerationReport:
    importer_frame = _find_importer_frame()
    if importer_frame is None:
        report = AccelerationReport("<unknown>", 0, 0)
        _set_last_report(report)
        return report

    module_name = importer_frame.f_globals.get("__name__", "<unknown>")
    report = _accelerate_namespace(importer_frame.f_globals, module_name)
    _set_last_report(report)
    _install_frame_trace(importer_frame, module_name)
    return report


def get_last_report() -> AccelerationReport:
    return _get_last_report()


activate()
