from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import urllib.error
from datetime import UTC, datetime
from importlib import util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

RUNTIME_PATH = (
    Path(__file__).parents[1] / ".github" / "workflows" / "github-star-organizer"
)


def load_runtime_module(name: str) -> ModuleType:
    module_path = RUNTIME_PATH / f"{name}.py"
    spec = util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {module_path}")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


github_api = load_runtime_module("github_api")
plan_envelope = load_runtime_module("plan")
prepare = load_runtime_module("prepare")
apply = load_runtime_module("apply")

ASSIGN_LIST_MUTATION = github_api.ASSIGN_LIST_MUTATION
CREATE_LIST_MUTATION = github_api.CREATE_LIST_MUTATION
LIST_ITEMS_QUERY = github_api.LIST_ITEMS_QUERY
LISTS_QUERY = github_api.LISTS_QUERY
STARS_QUERY = github_api.STARS_QUERY
GitHubGraphQL = github_api.GitHubGraphQL
list_memberships = github_api.list_memberships
starred_repositories = github_api.starred_repositories
create_input = plan_envelope.create_input
prepare_input = prepare.prepare_input
apply_plan = apply.apply_plan
preview_plan = apply.preview_plan
validate_plan = apply.validate_plan


def empty_plan(prepared_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "assignments": [],
        "new_lists": [],
        "source_sha256": prepared_input["source_sha256"],
    }


class FakeGraphQL:
    def __init__(self, page_size: int | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.page_size = page_size
        self.lists = [
            {
                "id": "UL_tools",
                "name": "Developer Tools",
                "description": "Tools",
                "isPrivate": False,
            }
        ]
        self.items = {"UL_tools": {"R_listed"}}
        self.star_edges = [
            {"starredAt": "2026-01-02T00:00:00Z", "node": repo("R_new", "new/tool")},
            {
                "starredAt": "2026-01-01T00:00:00Z",
                "node": repo("R_listed", "old/tool"),
            },
        ]

    def items_page(self, list_id: str, cursor: str | None) -> dict[str, Any]:
        item_ids = sorted(self.items.get(list_id, set()))
        start = int(cursor) if cursor else 0
        end = len(item_ids) if self.page_size is None else start + self.page_size
        return {
            "nodes": [{"id": item} for item in item_ids[start:end]],
            "pageInfo": {
                "hasNextPage": end < len(item_ids),
                "endCursor": str(end),
            },
        }

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if query == LISTS_QUERY:
            return {
                "viewer": {
                    "login": "octocat",
                    "lists": {
                        "nodes": self.lists,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        if query == LIST_ITEMS_QUERY:
            if variables["id"] not in self.items:
                return {"node": None}
            return {
                "node": {"items": self.items_page(variables["id"], variables["cursor"])}
            }
        if query == STARS_QUERY:
            return {
                "viewer": {
                    "starredRepositories": {
                        "edges": self.star_edges,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": len(self.star_edges),
                    }
                }
            }
        if query == CREATE_LIST_MUTATION:
            created = {
                "id": "UL_ai",
                "name": variables["input"]["name"],
                "description": variables["input"]["description"],
                "isPrivate": variables["input"]["isPrivate"],
            }
            self.lists.append(created)
            self.items[created["id"]] = set()
            return {"createUserList": {"list": created}}
        if query == ASSIGN_LIST_MUTATION:
            return {"updateUserListsForItem": {"lists": []}}
        raise AssertionError("unexpected query")


def repo(repository_id: str, name: str) -> dict[str, Any]:
    return {
        "id": repository_id,
        "nameWithOwner": name,
        "url": f"https://github.com/{name}",
        "description": "A useful tool",
        "isArchived": False,
        "isFork": False,
        "isPrivate": False,
        "primaryLanguage": {"name": "Python"},
        "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]},
    }


class ListMembershipTests(unittest.TestCase):
    def test_fetches_each_list_with_its_own_query(self):
        client = FakeGraphQL()
        client.lists.append(
            {"id": "UL_two", "name": "Two", "description": "", "isPrivate": False}
        )
        client.items["UL_two"] = {"R_a", "R_b"}

        memberships = list_memberships(client, ["UL_tools", "UL_two"])

        self.assertEqual(
            {"UL_tools": {"R_listed"}, "UL_two": {"R_a", "R_b"}}, memberships
        )
        self.assertEqual(
            [LIST_ITEMS_QUERY, LIST_ITEMS_QUERY], [call[0] for call in client.calls]
        )
        self.assertEqual(
            ["UL_tools", "UL_two"], [call[1]["id"] for call in client.calls]
        )

    def test_paginates_lists_with_more_than_one_page(self):
        client = FakeGraphQL(page_size=1)
        client.items["UL_tools"] = {"R_a", "R_b", "R_c"}

        memberships = list_memberships(client, ["UL_tools"])

        self.assertEqual({"UL_tools": {"R_a", "R_b", "R_c"}}, memberships)
        pages = [call for call in client.calls if call[0] == LIST_ITEMS_QUERY]
        self.assertEqual(3, len(pages))

    def test_rejects_deleted_lists(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            list_memberships(FakeGraphQL(), ["UL_gone"])


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/graphql",
        code,
        "Bad Gateway",
        None,
        io.BytesIO(b"<html>"),
    )


class RetryTests(unittest.TestCase):
    def run_execute(self, responses: list[Any]) -> tuple[Any, list[float]]:
        client = GitHubGraphQL("github_pat_example")
        sleeps: list[float] = []

        def fake_urlopen(request, timeout=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with (
            mock.patch.object(github_api.urllib.request, "urlopen", fake_urlopen),
            mock.patch.object(github_api.time, "sleep", sleeps.append),
        ):
            return client.execute("query", {}), sleeps

    def test_execute_retries_transient_server_errors(self):
        success = io.BytesIO(json.dumps({"data": {"ok": True}}).encode())

        data, sleeps = self.run_execute([http_error(502), http_error(503), success])

        self.assertEqual({"ok": True}, data)
        self.assertEqual([2, 4], sleeps)

    def test_execute_gives_up_after_max_attempts(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
            self.run_execute([http_error(502)] * 4)

    def test_execute_does_not_retry_client_errors(self):
        client = GitHubGraphQL("github_pat_example")
        sleeps: list[float] = []
        with (
            mock.patch.object(
                github_api.urllib.request,
                "urlopen",
                mock.Mock(side_effect=http_error(401)),
            ),
            mock.patch.object(github_api.time, "sleep", sleeps.append),
            self.assertRaisesRegex(RuntimeError, "HTTP 401"),
        ):
            client.execute("query", {})
        self.assertEqual([], sleeps)


class PartialResponseTests(unittest.TestCase):
    def execute_with_payload(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        client = GitHubGraphQL("github_pat_example")
        response = io.BytesIO(json.dumps(payload).encode())
        stderr = io.StringIO()
        with (
            mock.patch.object(
                github_api.urllib.request, "urlopen", mock.Mock(return_value=response)
            ),
            contextlib.redirect_stderr(stderr),
        ):
            return client.execute("query", {}), stderr.getvalue()

    def test_execute_keeps_partial_data_despite_node_errors(self):
        payload = {
            "data": {"node": {"items": {"nodes": [None, {"id": "R_ok"}]}}},
            "errors": [{"type": "FORBIDDEN", "message": "org token policy"}],
        }

        data, stderr = self.execute_with_payload(payload)

        self.assertEqual(payload["data"], data)
        self.assertIn("FORBIDDEN", stderr)

    def test_execute_raises_when_errors_leave_no_data(self):
        with self.assertRaisesRegex(RuntimeError, "GraphQL errors"):
            self.execute_with_payload({"data": None, "errors": [{"message": "boom"}]})

    def test_stars_skip_inaccessible_repositories(self):
        client = FakeGraphQL()
        client.star_edges.insert(0, {"starredAt": "2026-01-03T00:00:00Z", "node": None})

        stars = starred_repositories(client)

        self.assertEqual(["R_new", "R_listed"], [star["id"] for star in stars])


class GitHubListTests(unittest.TestCase):
    def test_rejects_non_fine_grained_tokens(self):
        with self.assertRaisesRegex(ValueError, "fine-grained PAT"):
            GitHubGraphQL("ghu_example")

    def test_prepare_only_returns_unlisted_stars(self):
        prepared = prepare_input(FakeGraphQL(), 100, "full")

        self.assertEqual("octocat", prepared["viewer_login"])
        self.assertEqual(["R_new"], [item["id"] for item in prepared["repositories"]])
        self.assertEqual(1, prepared["stats"]["uncategorized_total"])

    def test_prepare_fetches_memberships_per_list(self):
        client = FakeGraphQL()
        client.lists.append(
            {"id": "UL_two", "name": "Two", "description": "", "isPrivate": False}
        )
        client.items["UL_two"] = {"R_a"}

        prepare_input(client, 100, "full")

        queries = [call[0] for call in client.calls]
        self.assertEqual(2, queries.count(LIST_ITEMS_QUERY))

    def test_apply_fetches_memberships_per_list(self):
        client = FakeGraphQL()
        prepared_input = create_input(prepare_input(client, 100, "full"))
        plan = empty_plan(prepared_input)
        plan["assignments"] = [{"repository_id": "R_new", "list_ref": "UL_tools"}]
        client.calls.clear()

        apply_plan(client, prepared_input, plan)

        queries = [call[0] for call in client.calls]
        self.assertEqual(1, queries.count(LIST_ITEMS_QUERY))

    def test_weekly_scope_excludes_stars_older_than_seven_days(self):
        prepared = prepare_input(
            FakeGraphQL(), 100, "weekly", datetime(2026, 1, 10, tzinfo=UTC)
        )

        self.assertEqual([], prepared["repositories"])
        self.assertEqual(0, prepared["stats"]["starred_in_scope"])

    def test_plan_requires_each_repository_exactly_once(self):
        prepared_input = create_input(prepare_input(FakeGraphQL(), 100, "full"))
        plan = empty_plan(prepared_input)
        with self.assertRaisesRegex(ValueError, "assignment mismatch"):
            validate_plan(prepared_input, plan)

    def test_dry_run_previews_without_graphql_mutations(self):
        prepared_input = create_input(prepare_input(FakeGraphQL(), 100, "full"))
        plan = empty_plan(prepared_input)
        plan["assignments"] = [{"repository_id": "R_new", "list_ref": "UL_tools"}]

        preview = preview_plan(prepared_input, plan)

        self.assertEqual(1, preview["assignments"])
        self.assertEqual({"UL_tools": 1}, preview["assignments_by_list"])

    def test_apply_creates_list_and_assigns_repository(self):
        client = FakeGraphQL()
        prepared_input = create_input(prepare_input(client, 100, "full"))
        plan = empty_plan(prepared_input)
        plan["new_lists"] = [
            {
                "key": "ai-ml",
                "name": "AI and Machine Learning",
                "description": "AI tools",
                "is_private": False,
            }
        ]
        plan["assignments"] = [{"repository_id": "R_new", "list_ref": "new:ai-ml"}]

        result = apply_plan(client, prepared_input, plan)

        self.assertEqual({"assigned": 1, "created_lists": 1}, result)
        assignment_calls = [
            call for call in client.calls if call[0] == ASSIGN_LIST_MUTATION
        ]
        self.assertEqual(["UL_ai"], assignment_calls[0][1]["input"]["listIds"])

    def test_apply_preserves_concurrent_membership(self):
        client = FakeGraphQL()
        prepared_input = create_input(prepare_input(client, 100, "full"))
        plan = empty_plan(prepared_input)
        client.items["UL_tools"].add("R_new")
        plan["new_lists"] = [
            {
                "key": "ai-ml",
                "name": "AI and Machine Learning",
                "description": "AI tools",
                "is_private": False,
            }
        ]
        plan["assignments"] = [{"repository_id": "R_new", "list_ref": "new:ai-ml"}]

        apply_plan(client, prepared_input, plan)

        assignment_calls = [
            call for call in client.calls if call[0] == ASSIGN_LIST_MUTATION
        ]
        self.assertEqual(
            ["UL_ai", "UL_tools"], assignment_calls[0][1]["input"]["listIds"]
        )

    def test_rejects_modified_plan_source(self):
        prepared_input = create_input(prepare_input(FakeGraphQL(), 100, "full"))
        plan = empty_plan(prepared_input)
        prepared_input["repositories"] = []

        with self.assertRaisesRegex(ValueError, "input was modified"):
            validate_plan(prepared_input, plan)


if __name__ == "__main__":
    unittest.main()
