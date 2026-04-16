"""Automatic best-effort runtime acceleration for importing modules.

Importing this package enables importer-module patching without explicit API calls.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from types import FrameType, FunctionType, ModuleType


@dataclass(frozen=True)
class AccelerationReport:
    module_name: str
    accelerated_functions: int
    accelerated_classes: int


_last_report = AccelerationReport("<none>", 0, 0)


def _find_importer_frame() -> FrameType | None:
    frame = inspect.currentframe()
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


def _accelerate_function(func: FunctionType) -> FunctionType:
    if getattr(func, "__gslpython_accelerated__", False):
        return func

    func.__gslpython_accelerated__ = True
    return func


def _accelerate_class(cls: type) -> bool:
    if getattr(cls, "__gslpython_accelerated__", False):
        return False

    changed = False
    for name, value in list(vars(cls).items()):
        if isinstance(value, staticmethod):
            wrapped = staticmethod(_accelerate_function(value.__func__))
            setattr(cls, name, wrapped)
            changed = True
        elif isinstance(value, classmethod):
            wrapped = classmethod(_accelerate_function(value.__func__))
            setattr(cls, name, wrapped)
            changed = True
        elif isinstance(value, FunctionType):
            setattr(cls, name, _accelerate_function(value))
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
            accelerated = _accelerate_function(value)
            if not was_accelerated:
                namespace[name] = accelerated
                functions += 1
        elif isinstance(value, type):
            if _accelerate_class(value):
                classes += 1

    return AccelerationReport(module_name, functions, classes)


def _install_frame_trace(frame: FrameType, module_name: str) -> None:
    previous_trace = sys.gettrace()
    last_namespace_size = len(frame.f_globals)

    def tracer(current_frame: FrameType, event: str, _arg):
        nonlocal last_namespace_size

        if current_frame is frame and event in {"line", "return"}:
            namespace_size = len(current_frame.f_globals)
            if event == "return" or namespace_size != last_namespace_size:
                _accelerate_namespace(current_frame.f_globals, module_name)
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
    global _last_report

    importer_frame = _find_importer_frame()
    if importer_frame is None:
        _last_report = AccelerationReport("<unknown>", 0, 0)
        return _last_report

    module_name = importer_frame.f_globals.get("__name__", "<unknown>")
    _last_report = _accelerate_namespace(importer_frame.f_globals, module_name)
    _install_frame_trace(importer_frame, module_name)
    return _last_report


def get_last_report() -> AccelerationReport:
    return _last_report


activate()
