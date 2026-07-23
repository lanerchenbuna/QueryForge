"""Print reproducible Phase 2 demo commands without requiring provider credentials."""

from __future__ import annotations


def main() -> int:
    commands = (
        "# Basic governed query",
        'queryforge --question "List the top anime by watch hours"',
        "# Conversation follow-up",
        'queryforge --session-id demo --question "List anime by watch hours"',
        'queryforge --session-id demo --question "by genre"',
        "# Complex workflow and progress events",
        'queryforge --stream --complexity-mode complex --question "Compare monthly completion rate by region"',
        "# Scoped static report",
        "queryforge --report --enable-subject-tree "
        "--subject-tree sample_data/anime_streaming/subjects.yml --subject engagement "
        '--database sample_data/anime_streaming/anime_streaming.sqlite '
        '--question "Build report for watch hours by genre"',
        "# Optional MCP server",
        "python -m queryforge.interfaces.mcp.server --transport stdio",
    )
    print("\n".join(commands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
