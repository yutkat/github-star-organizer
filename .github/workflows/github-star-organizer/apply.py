"""Validate an AI categorization plan and apply it to GitHub Lists."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from github_api import (
    ASSIGN_LIST_MUTATION,
    CREATE_LIST_MUTATION,
    GitHubGraphQL,
    GraphQLExecutor,
    list_memberships,
    paginated_lists,
)
from plan import verified_source

LIST_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
INPUT_FILENAME = "categorization-input.json"


def validate_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    prepared = verified_source(prepared_input, plan)
    new_lists = plan.get("new_lists")
    assignments = plan.get("assignments")
    if not isinstance(new_lists, list) or not isinstance(assignments, list):
        raise TypeError("new_lists and assignments must be arrays")
    if len(new_lists) > 5:
        raise ValueError("a plan may create at most five lists")

    existing_lists = prepared["existing_lists"]
    existing_ids = {item["id"] for item in existing_lists}
    existing_names = {item["name"].casefold() for item in existing_lists}
    new_by_key: dict[str, dict[str, Any]] = {}
    new_names: set[str] = set()
    for item in new_lists:
        key = item.get("key")
        name = item.get("name")
        description = item.get("description")
        is_private = item.get("is_private")
        if not isinstance(key, str) or not LIST_KEY.fullmatch(key):
            raise ValueError(f"invalid new list key: {key!r}")
        if key in new_by_key:
            raise ValueError(f"duplicate new list key: {key}")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50:
            raise ValueError(f"invalid new list name for {key}")
        folded_name = name.strip().casefold()
        if folded_name in existing_names or folded_name in new_names:
            raise ValueError(f"duplicate or existing list name: {name}")
        if not isinstance(description, str) or len(description) > 160:
            raise ValueError(f"invalid description for {key}")
        if not isinstance(is_private, bool):
            raise TypeError(f"is_private must be boolean for {key}")
        normalized = {
            "key": key,
            "name": name.strip(),
            "description": description,
            "is_private": is_private,
        }
        new_by_key[key] = normalized
        new_names.add(folded_name)

    required_ids = {repository["id"] for repository in prepared["repositories"]}
    assigned: dict[str, str] = {}
    for item in assignments:
        repository_id = item.get("repository_id")
        list_ref = item.get("list_ref")
        if repository_id in assigned:
            raise ValueError(f"duplicate assignment for {repository_id}")
        if list_ref not in existing_ids:
            if not isinstance(list_ref, str) or not list_ref.startswith("new:"):
                raise ValueError(f"unknown list reference: {list_ref!r}")
            if list_ref.removeprefix("new:") not in new_by_key:
                raise ValueError(f"unknown new list reference: {list_ref}")
        assigned[repository_id] = list_ref

    if set(assigned) != required_ids:
        missing = sorted(required_ids - set(assigned))
        extra = sorted(set(assigned) - required_ids)
        raise ValueError(f"assignment mismatch; missing={missing}, extra={extra}")
    return list(new_by_key.values()), assigned


def preview_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    new_lists, assignments = validate_plan(prepared_input, plan)
    assignments_by_list: dict[str, int] = {}
    for list_ref in assignments.values():
        assignments_by_list[list_ref] = assignments_by_list.get(list_ref, 0) + 1
    return {
        "assignments": len(assignments),
        "assignments_by_list": dict(sorted(assignments_by_list.items())),
        "new_lists": new_lists,
    }


def apply_plan(
    client: GraphQLExecutor, prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, int]:
    new_lists, assignments = validate_plan(prepared_input, plan)
    _, current_lists = paginated_lists(client)
    current_by_name = {item["name"].casefold(): item for item in current_lists}
    resolved_new: dict[str, str] = {}
    created_count = 0

    for item in new_lists:
        existing = current_by_name.get(item["name"].casefold())
        if existing:
            resolved_new[item["key"]] = existing["id"]
            continue
        result = client.execute(
            CREATE_LIST_MUTATION,
            {
                "input": {
                    "name": item["name"],
                    "description": item["description"],
                    "isPrivate": item["is_private"],
                }
            },
        )
        created = result["createUserList"]["list"]
        resolved_new[item["key"]] = created["id"]
        current_lists.append(created)
        created_count += 1

    current_memberships: dict[str, set[str]] = {}
    all_ids = [item["id"] for item in current_lists]
    for list_id, item_ids in list_memberships(client, all_ids).items():
        for repository_id in item_ids:
            current_memberships.setdefault(repository_id, set()).add(list_id)

    for repository_id, list_ref in assignments.items():
        target_id = (
            resolved_new[list_ref.removeprefix("new:")]
            if list_ref.startswith("new:")
            else list_ref
        )
        list_ids = sorted(current_memberships.get(repository_id, set()) | {target_id})
        client.execute(
            ASSIGN_LIST_MUTATION,
            {"input": {"itemId": repository_id, "listIds": list_ids}},
        )

    return {"assigned": len(assignments), "created_lists": created_count}


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
    client = GitHubGraphQL(os.environ.get("COPILOT_GITHUB_TOKEN", ""))
    print(json.dumps(apply_plan(client, prepared_input, plan), sort_keys=True))


if __name__ == "__main__":
    main()
