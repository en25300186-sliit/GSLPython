"""CLI entry point: ``python -m GSLPython <script.py> [output]``

Compiles *script.py* to a native binary via a single C compilation step.
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print("Usage: python -m GSLPython <script.py> [output_path]")
        print()
        print("Compiles <script.py> to a native binary (.out / .exe) via Cython + gcc.")
        sys.exit(0)

    source = args[0]
    output = args[1] if len(args) > 1 else None

    # Import lazily so that activate() in __init__ doesn't trace *this* frame.
    import GSLPython  # noqa: PLC0415

    try:
        binary = GSLPython.build_executable(source, output)
        print(f"Built: {binary}")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
