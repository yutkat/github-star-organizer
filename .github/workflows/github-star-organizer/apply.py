"""Validate an AI categorization plan and apply it to GitHub Lists."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from github_api import (
    ASSIGN_LIST_MUTATION,
    GitHubGraphQL,
    GraphQLExecutor,
    list_memberships,
    paginated_lists,
)
from plan import verified_source

INPUT_FILENAME = "categorization-input.json"


def validate_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, str]:
    prepared = verified_source(prepared_input, plan)
    if plan.get("new_lists"):
        raise ValueError("creating lists is not supported; assign existing lists only")
    assignments = plan.get("assignments")
    if not isinstance(assignments, list):
        raise TypeError("assignments must be an array")

    existing_ids = {item["id"] for item in prepared["existing_lists"]}
    required_ids = {repository["id"] for repository in prepared["repositories"]}
    assigned: dict[str, str] = {}
    for item in assignments:
        if not isinstance(item, dict):
            raise TypeError("assignments entries must be objects")
        repository_id = item.get("repository_id")
        list_ref = item.get("list_ref")
        if repository_id in assigned:
            raise ValueError(f"duplicate assignment for {repository_id}")
        if list_ref not in existing_ids:
            raise ValueError(f"unknown list reference: {list_ref!r}")
        assigned[repository_id] = list_ref

    if set(assigned) != required_ids:
        missing = sorted(required_ids - set(assigned))
        extra = sorted(set(assigned) - required_ids)
        raise ValueError(f"assignment mismatch; missing={missing}, extra={extra}")
    return assigned


def preview_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    assignments = validate_plan(prepared_input, plan)
    assignments_by_list: dict[str, int] = {}
    for list_ref in assignments.values():
        assignments_by_list[list_ref] = assignments_by_list.get(list_ref, 0) + 1
    return {
        "assignments": len(assignments),
        "assignments_by_list": dict(sorted(assignments_by_list.items())),
    }


def apply_plan(
    client: GraphQLExecutor, prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, int]:
    assignments = validate_plan(prepared_input, plan)
    _, current_lists = paginated_lists(client)

    current_memberships: dict[str, set[str]] = {}
    all_ids = [item["id"] for item in current_lists]
    for list_id, item_ids in list_memberships(client, all_ids).items():
        for repository_id in item_ids:
            current_memberships.setdefault(repository_id, set()).add(list_id)

    for repository_id, list_ref in assignments.items():
        list_ids = sorted(current_memberships.get(repository_id, set()) | {list_ref})
        client.execute(
            ASSIGN_LIST_MUTATION,
            {"input": {"itemId": repository_id, "listIds": list_ids}},
        )

    return {"assigned": len(assignments)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--dry-run", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    input_path = args.plan.with_name(INPUT_FILENAME)
    with input_path.open(encoding="utf-8") as handle:
        prepared_input = json.load(handle)
    with args.plan.open(encoding="utf-8") as handle:
        plan = json.load(handle)
    if args.dry_run:
        print(json.dumps(preview_plan(prepared_input, plan), sort_keys=True))
        return
    # Lists mutations reject fine-grained PATs, so apply uses a classic PAT.
    client = GitHubGraphQL(os.environ.get("STAR_LISTS_TOKEN", ""))
    print(json.dumps(apply_plan(client, prepared_input, plan), sort_keys=True))


if __name__ == "__main__":
    main()
