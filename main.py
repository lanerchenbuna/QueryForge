"""Backward-compatible repository entry point.

Installed users should prefer ``queryforge`` or ``python -m queryforge``.
Importers receive the canonical :mod:`queryforge.cli` module so existing test and
integration patches continue to target the function's real globals.
"""

import sys

from queryforge import cli as _cli


if __name__ == "__main__":
    raise SystemExit(_cli.main())

sys.modules[__name__] = _cli
