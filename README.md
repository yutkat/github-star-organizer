# GitHub Star Organizer

This repository uses GitHub Agentic Workflows to categorize the authenticated
user's starred repositories into their actual GitHub Lists once a week.

It reads stars and existing Lists through GitHub's GraphQL API. The AI reuses a
suitable existing List or proposes a broad new one, and a deterministic step
validates the plan before calling `createUserList` and
`updateUserListsForItem`.

## Install

Install the package into any GitHub repository with the guided wizard:

```bash
gh aw add-wizard yutkat/github-star-organizer
```

The installer adds the workflow, compiles its lock file, copies the companion
uv runtime under `.github/workflows/github-star-organizer/`, and records its
source for future updates. Pull upstream releases later with `gh aw update`.

## Setup

1. Enable GitHub Actions in the repository where the package was installed.
2. Create one fine-grained personal access token owned by the user whose stars
   will be categorized. Grant `Copilot Requests`, `Starring: Read and write`,
   and only the private repository access that is needed.
3. Save the token as the Actions secret `COPILOT_GITHUB_TOKEN`.
4. Run `Categorize starred repositories` manually from the Actions tab once.

The same token authenticates the Copilot engine and the deterministic GitHub
Lists steps. Set an expiration appropriate for the deployment and rotate the
repository secret before it expires.

See GitHub's documentation for
[creating a fine-grained PAT](https://docs.github.com/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token).

## Behavior

- Runs weekly on Monday at a time distributed by GitHub Agentic Workflows and
  supports manual dispatch.
- Scheduled runs inspect only stars added during the preceding seven days.
- Manual runs default to the same weekly scope; select `full` explicitly to
  process the complete uncategorized backlog.
- Retrieves the token owner's stars and Lists, without a hard-coded username.
- Skips repositories that already belong to at least one List.
- Preserves existing and concurrent List memberships.
- Lets the AI create at most five broad Lists per run.
- Writes classifications directly to GitHub Lists; no Markdown catalog is used.

To avoid sending thousands of stars to the agent at once, each run normally
processes up to 500 uncategorized repositories. Repeat manual runs to complete
the initial backfill. Run the manual `full` scope after a failed or disabled
scheduled run to recover stars outside the seven-day window.

If you edit the Agentic Workflow source, run `gh aw compile` and commit the
generated `.github/workflows/categorize-stars.lock.yml` as well.

## Development

Python is managed with uv. Run the test suite with:

```bash
uv run --frozen python -m unittest -v
```

GitHub Agentic Workflows and GitHub Lists are evolving features. Recompile and
run the workflow manually after upgrading `gh-aw`.
