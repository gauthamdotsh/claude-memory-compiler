"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads hook input (cwd) to resolve the active project, selects the
knowledge articles relevant to that project (plus project: global articles
about the KB/tooling itself), and injects their full bodies - not just the
index - alongside the index and the recent daily log.

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from utils import get_article_project, list_wiki_articles, parse_frontmatter  # noqa: E402

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"

MAX_CONTEXT_CHARS = 90_000
MAX_LOG_LINES = 30


def read_hook_input() -> dict:
    """Read the SessionStart hook JSON payload from stdin (cwd, session_id, source)."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


def resolve_project(cwd: str) -> str:
    """Resolve the active project id from cwd: nearest git repo's basename, else basename(cwd)."""
    if not cwd:
        return "unknown"

    path = Path(cwd).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate.name

    return path.name


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def select_relevant_articles(project: str) -> tuple[list[Path], list[Path]]:
    """Pick articles for this project: all project:global + all matching project.

    Returns (selected, dropped_due_to_budget) - dropped is filled in by the caller
    once it knows how much budget is left.
    """
    global_articles = []
    project_articles = []

    for article in list_wiki_articles():
        meta = parse_frontmatter(article)
        article_project = meta.get("project", "global")
        if article_project == "global":
            global_articles.append((article, meta))
        elif article_project == project:
            project_articles.append((article, meta))

    # Most-recently-updated project articles first, so budget trimming drops the oldest.
    project_articles.sort(key=lambda pair: pair[1].get("updated", ""), reverse=True)
    global_articles.sort(key=lambda pair: pair[1].get("updated", ""), reverse=True)

    ordered = [a for a, _ in global_articles] + [a for a, _ in project_articles]
    return ordered


def build_article_bundle(articles: list[Path], budget: int) -> tuple[str, list[str]]:
    """Concatenate article bodies up to budget chars. Returns (bundle, dropped_names)."""
    parts = []
    dropped = []
    used = 0

    for i, article in enumerate(articles):
        rel = article.relative_to(KNOWLEDGE_DIR)
        content = article.read_text(encoding="utf-8")
        entry = f"### {rel}\n\n{content}"
        if used + len(entry) > budget:
            # Stop at the first miss so the cutoff is a clean recency boundary -
            # everything from here on (already sorted most-recent-first) is dropped.
            dropped.extend(str(a.relative_to(KNOWLEDGE_DIR)) for a in articles[i:])
            break
        parts.append(entry)
        used += len(entry)

    return "\n\n---\n\n".join(parts), dropped


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    hook_input = read_hook_input()
    project = resolve_project(hook_input.get("cwd", ""))

    parts = []

    # Today's date
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    # Session behavior reminder (see AGENTS.md "Session Behavior")
    parts.append(
        "## KB Session Behavior\n"
        "1. Read the index below first.\n"
        "2. Relevant full articles for this project are already included below - read them "
        "before answering if they bear on the question.\n"
        "3. If a strong/non-obvious answer emerges, file it to `knowledge/qa/` or "
        "`knowledge/concepts/` (via `scripts/query.py \"...\" --file-back` or directly)."
    )

    # Knowledge base index (always included - cheap, lets Claude request anything not injected)
    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        parts.append(f"## Knowledge Base Index\n\n{index_content}")
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Full bodies of articles relevant to this project (the actual fix: not just the index)
    selected = select_relevant_articles(project)
    bundle, dropped = build_article_bundle(selected, MAX_CONTEXT_CHARS)
    if bundle:
        parts.append(f"## Relevant Knowledge Articles (project: {project})\n\n{bundle}")
    if dropped:
        parts.append(
            f"## Note: {len(dropped)} article(s) omitted for context budget: "
            + ", ".join(dropped)
        )

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    return "\n\n---\n\n".join(parts)


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
