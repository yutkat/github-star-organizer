"""GitHub GraphQL client and queries shared by preparation and application."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Protocol

RETRYABLE_STATUS = {502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2


def urlopen_with_retry(request: urllib.request.Request) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS or attempt + 1 == MAX_ATTEMPTS:
                raise
            time.sleep(BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


LISTS_QUERY = """
query Lists($cursor: String) {
  viewer {
    login
    lists(first: 100, after: $cursor) {
      nodes { id name description isPrivate }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

LIST_ITEMS_QUERY = """
query ListItems($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on UserList {
      items(first: 100, after: $cursor) {
        nodes { ... on Repository { id } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

LIST_ITEMS_BATCH_QUERY = """
query ListItemsBatch($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on UserList {
      id
      items(first: 100) {
        nodes { ... on Repository { id } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

STARS_QUERY = """
query Stars($cursor: String) {
  viewer {
    starredRepositories(
      first: 100
      after: $cursor
      orderBy: {field: STARRED_AT, direction: DESC}
    ) {
      edges {
        starredAt
        node {
          id
          nameWithOwner
          url
          description
          isArchived
          isFork
          isPrivate
          primaryLanguage { name }
          repositoryTopics(first: 20) { nodes { topic { name } } }
        }
      }
      pageInfo { hasNextPage endCursor }
      totalCount
    }
  }
}
"""

CREATE_LIST_MUTATION = """
mutation CreateList($input: CreateUserListInput!) {
  createUserList(input: $input) { list { id name } }
}
"""

ASSIGN_LIST_MUTATION = """
mutation AssignList($input: UpdateUserListsForItemInput!) {
  updateUserListsForItem(input: $input) { lists { id name } }
}
"""


class GraphQLExecutor(Protocol):
    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class GitHubGraphQL:
    def __init__(self, token: str, endpoint: str = "https://api.github.com/graphql"):
        if not token:
            raise ValueError("COPILOT_GITHUB_TOKEN is required")
        if not token.startswith("github_pat_"):
            raise ValueError("COPILOT_GITHUB_TOKEN must be a fine-grained PAT")
        self.token = token
        self.endpoint = endpoint

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "github-star-organizer",
            },
            method="POST",
        )
        try:
            with urlopen_with_retry(request) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            raise RuntimeError(f"GitHub GraphQL HTTP {error.code}: {body}") from error
        if result.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {result['errors']}")
        return result["data"]


def paginated_lists(client: GraphQLExecutor) -> tuple[str, list[dict[str, Any]]]:
    cursor = None
    login = ""
    lists: list[dict[str, Any]] = []
    while True:
        viewer = client.execute(LISTS_QUERY, {"cursor": cursor})["viewer"]
        login = viewer["login"]
        connection = viewer["lists"]
        lists.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return login, lists
        cursor = connection["pageInfo"]["endCursor"]


# nodes(ids:) accepts at most 100 ids per request.
LIST_BATCH_SIZE = 100


def list_memberships(
    client: GraphQLExecutor, list_ids: list[str]
) -> dict[str, set[str]]:
    memberships: dict[str, set[str]] = {}
    for start in range(0, len(list_ids), LIST_BATCH_SIZE):
        chunk = list_ids[start : start + LIST_BATCH_SIZE]
        nodes = client.execute(LIST_ITEMS_BATCH_QUERY, {"ids": chunk})["nodes"]
        for list_id, node in zip(chunk, nodes, strict=True):
            if not node or "items" not in node:
                raise ValueError(f"GitHub list no longer exists: {list_id}")
            memberships[list_id] = remaining_item_ids(client, list_id, node["items"])
    return memberships


def remaining_item_ids(
    client: GraphQLExecutor, list_id: str, connection: dict[str, Any]
) -> set[str]:
    item_ids = {item["id"] for item in connection["nodes"] if item}
    while connection["pageInfo"]["hasNextPage"]:
        cursor = connection["pageInfo"]["endCursor"]
        connection = client.execute(
            LIST_ITEMS_QUERY, {"id": list_id, "cursor": cursor}
        )["node"]["items"]
        item_ids.update(item["id"] for item in connection["nodes"] if item)
    return item_ids


def starred_repositories(
    client: GraphQLExecutor, cutoff: datetime | None = None
) -> list[dict[str, Any]]:
    cursor = None
    repositories: list[dict[str, Any]] = []
    while True:
        connection = client.execute(STARS_QUERY, {"cursor": cursor})["viewer"][
            "starredRepositories"
        ]
        for edge in connection["edges"]:
            starred_at = datetime.fromisoformat(edge["starredAt"])
            if cutoff is not None and starred_at < cutoff:
                return repositories
            repository = edge["node"]
            repositories.append(
                {
                    "id": repository["id"],
                    "name_with_owner": repository["nameWithOwner"],
                    "url": repository["url"],
                    "description": repository["description"],
                    "language": (
                        repository["primaryLanguage"]["name"]
                        if repository["primaryLanguage"]
                        else None
                    ),
                    "topics": [
                        item["topic"]["name"]
                        for item in repository["repositoryTopics"]["nodes"]
                    ],
                    "archived": repository["isArchived"],
                    "fork": repository["isFork"],
                    "private": repository["isPrivate"],
                    "starred_at": edge["starredAt"],
                }
            )
        if not connection["pageInfo"]["hasNextPage"]:
            return repositories
        cursor = connection["pageInfo"]["endCursor"]
