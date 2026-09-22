"""Locate the workspace a run reads from and writes to.

Two different kinds of "root" exist in this project, and conflating them was a real
defect (E-22):

* **Packaged resources** — ``models.yml``'s fallback, ``bundled_skills/`` — ship
  *inside* the ``queryforge`` package. They are found relative to ``__file__`` and
  work identically from a source checkout and from an installed wheel.
* **The workspace** — ``.queryforge/`` state (history, traces, logs, charts, the
  vector KB), ``sample_data/``, ``evaluation/`` — belongs to whoever is *running*
  QueryForge, not to the installed package.

Every module used to derive the second one as ``Path(__file__).parents[N]``. That
resolves to the repository root in a developer checkout and to ``site-packages``
after ``pip install``, where ``.queryforge/history.db`` is (a) not the user's data
and (b) frequently unwritable. Resolution is therefore explicit and ordered:

1. ``QUERYFORGE_ROOT`` — an explicit operator decision, always wins.
2. The nearest ancestor of the package that looks like a source checkout
   (a marker such as ``pyproject.toml``), so checkout behaviour is unchanged and
   a vendored/zipped layout still finds its own root.
3. The current working directory, which is the user's workspace when the package
   lives in ``site-packages``.

Resolution happens once per process, because an installed process cannot
meaningfully switch workspaces mid-run.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

__all__ = ["WORKSPACE_ROOT_ENV", "resolve_path", "workspace_root"]

#: Environment variable an operator sets to point QueryForge at a workspace.
WORKSPACE_ROOT_ENV = "QUERYFORGE_ROOT"

#: Files that only ever exist at the root of a source checkout (or a deployment
#: that mirrors one). ``.git`` covers worktrees and bare checkouts.
_CHECKOUT_MARKERS = ("pyproject.toml", "models.yml", ".git")


def _is_checkout(path: Path) -> bool:
    return any((path / marker).exists() for marker in _CHECKOUT_MARKERS)


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    """Return the resolved workspace root for this process.

    Falls back to the current working directory rather than to ``site-packages``:
    a wrong-but-writable directory is a silent data-integrity bug, whereas the
    working directory is at least where the caller asked QueryForge to work.
    """
    return _resolve_root(Path(__file__).resolve().parent)


def _resolve_root(package_dir: Path) -> Path:
    """Apply the documented resolution order to an arbitrary package location.

    Split out from :func:`workspace_root` so the installed-package case (a package
    directory with no checkout ancestor, i.e. ``site-packages``) is testable
    without patching ``__file__``.
    """
    override = (os.getenv(WORKSPACE_ROOT_ENV) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve()

    for candidate in (package_dir, *package_dir.parents):
        if _is_checkout(candidate):
            return candidate
    return Path.cwd().resolve()


def resolve_path(path: str | Path, *, base: Path | None = None) -> Path:
    """Resolve ``path`` against the workspace root, expanding ``~``.

    Absolute paths pass through untouched, so a caller-supplied path is never
    silently reinterpreted.
    """
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return (base or workspace_root()) / resolved
