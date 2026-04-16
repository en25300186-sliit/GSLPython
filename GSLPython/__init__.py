"""Automatic best-effort runtime acceleration for importing modules.

Importing this package enables importer-module patching without explicit API calls.
"""

from __future__ import annotations

import atexit
import hashlib
import inspect
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, FunctionType, ModuleType


@dataclass(frozen=True)
class AccelerationReport:
    module_name: str
    accelerated_functions: int
    accelerated_classes: int


_thread_state = threading.local()
_runtime_artifacts: list[str] = []
_compilation_guard: set[str] = set()
_extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)


def _cleanup_runtime_artifacts() -> None:
    while _runtime_artifacts:
        path = _runtime_artifacts.pop()
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_runtime_artifacts)


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

    module_suffix = hashlib.sha256(module_file.encode("utf-8")).hexdigest()[:32]
    compiled_module_name = (
        f"_gslpython_compiled_{module_name.replace('.', '_')}_{module_suffix}"
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
            if not filename.endswith(_extension_suffixes):
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


# ---------------------------------------------------------------------------
# AOT compilation: Python file → native executable (.out / .exe)
# ---------------------------------------------------------------------------

def _python_build_flags() -> tuple[list[str], list[str]]:
    """Return (include_flags, link_flags) needed to embed the Python runtime."""
    inc = sysconfig.get_path("include")
    inc_flags = [f"-I{inc}"]

    cfg = sysconfig.get_config_vars()
    lib_dir = cfg.get("LIBDIR", "")
    link_flags: list[str] = []
    if lib_dir:
        link_flags.append(f"-L{lib_dir}")

    # BLDLIBRARY may already be a linker flag (e.g. "-lpython3.12") or a bare
    # filename (e.g. "libpython3.12.a").  Prefer constructing the flag from
    # LDVERSION which is always just the version string (e.g. "3.12").
    ldversion = cfg.get("LDVERSION") or f"{sys.version_info.major}.{sys.version_info.minor}"
    link_flags.append(f"-lpython{ldversion}")
    # Extra flags present on the current platform (dl, m, etc.)
    for var in ("LIBS", "SYSLIBS"):
        extra = cfg.get(var, "")
        if extra:
            link_flags.extend(extra.split())
    return inc_flags, link_flags


def build_executable(
    source_file: str | os.PathLike,
    output_path: str | os.PathLike | None = None,
) -> str:
    """Compile *source_file* (a ``.py`` module) into a native binary.

    The compilation pipeline is:

    1. **Cython** translates the Python source to a C translation unit.
    2. **A single** ``gcc`` invocation compiles + links that C file together
       with a tiny ``main()`` shim into a self-contained ``.out`` / ``.exe``.

    Parameters
    ----------
    source_file:
        Path to the ``.py`` file to compile.
    output_path:
        Desired path for the produced binary.  Defaults to
        ``<source_stem>.out`` (``<source_stem>.exe`` on Windows) in the same
        directory as *source_file*.

    Returns
    -------
    str
        Absolute path of the produced binary.

    Raises
    ------
    RuntimeError
        If Cython or the C compiler is not available, or if any step fails.
    """
    try:
        from Cython.Compiler.Main import compile as cython_compile
        from Cython.Compiler.CmdLine import parse_command_line
    except ImportError as exc:
        raise RuntimeError(
            "Cython is required for build_executable. "
            "Install it with: pip install cython"
        ) from exc

    source = Path(source_file).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source file not found: {source}")

    stem = source.stem
    if output_path is None:
        suffix = ".exe" if sys.platform == "win32" else ".out"
        output = source.with_name(stem + suffix)
    else:
        output = Path(output_path).resolve()

    build_dir = Path(tempfile.mkdtemp(prefix="gslpython-aot-"))
    _runtime_artifacts.append(str(build_dir))

    try:
        # ------------------------------------------------------------------
        # Step 1 – Cython: .py → .c
        # ------------------------------------------------------------------
        c_file = build_dir / f"{stem}.c"
        opts, _ = parse_command_line(
            [
                "--embed",          # emit a main() that embeds the Python runtime
                "-3",               # Python 3 semantics
                "-o", str(c_file),
                str(source),
            ]
        )
        result = cython_compile(str(source), opts)
        if result.num_errors:
            raise RuntimeError(
                f"Cython failed to compile {source} ({result.num_errors} error(s))"
            )
        if not c_file.is_file():
            raise RuntimeError(f"Cython did not produce expected C file: {c_file}")

        # ------------------------------------------------------------------
        # Step 2 – single gcc call: .c → native binary
        # ------------------------------------------------------------------
        compiler = os.environ.get("CC", shutil.which("gcc") or shutil.which("cc") or "gcc")
        inc_flags, link_flags = _python_build_flags()

        cmd: list[str] = (
            [compiler]
            + inc_flags
            + ["-O3", "-fwrapv", str(c_file), "-o", str(output)]
            + link_flags
        )

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"C compilation failed.\nCommand: {' '.join(cmd)}\n"
                f"stderr:\n{proc.stderr}"
            )
    finally:
        # The build directory is only needed during compilation; clean it up
        # immediately instead of waiting for process exit.
        try:
            _runtime_artifacts.remove(str(build_dir))
        except ValueError:
            pass
        shutil.rmtree(str(build_dir), ignore_errors=True)

    return str(output)


activate()
