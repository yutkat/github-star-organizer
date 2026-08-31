"""Prepare a bounded set of uncategorized stars for the AI agent."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from github_api import (
    GitHubGraphQL,
    GraphQLExecutor,
    list_item_ids,
    paginated_lists,
    starred_repositories,
)
from plan import create_input


def prepare_input(
    client: GraphQLExecutor,
    batch_size: int,
    scope: str = "full",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if scope not in {"full", "weekly"}:
        raise ValueError("scope must be full or weekly")

    cutoff = None
    if scope == "weekly":
        cutoff = (now or datetime.now(UTC)) - timedelta(days=7)

    login, lists = paginated_lists(client)
    memberships: set[str] = set()
    for user_list in lists:
        memberships.update(list_item_ids(client, user_list["id"]))

    stars = starred_repositories(client, cutoff)
    uncategorized = [repo for repo in stars if repo["id"] not in memberships]
    return {
        "existing_lists": lists,
        "repositories": uncategorized[:batch_size],
        "stats": {
            "batch_size": batch_size,
            "existing_lists": len(lists),
            "remaining_after_batch": max(0, len(uncategorized) - batch_size),
            "scope": scope,
            "scope_started_at": cutoff.isoformat() if cutoff else None,
            "starred_in_scope": len(stars),
            "uncategorized_total": len(uncategorized),
        },
        "viewer_login": login,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--batch-size", type=int, default=500)
    result.add_argument("--scope", choices=("full", "weekly"), default="full")
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    client = GitHubGraphQL(os.environ.get("COPILOT_GITHUB_TOKEN", ""))
    source = prepare_input(client, args.batch_size, args.scope)
    write_json(args.output, create_input(source))


if __name__ == "__main__":
    main()
