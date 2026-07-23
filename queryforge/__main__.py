"""Allow ``python -m queryforge`` to use the canonical CLI."""

from queryforge.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
