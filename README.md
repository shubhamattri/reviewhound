# Reviewhound

[![ci](https://github.com/shubhamattri/reviewhound/actions/workflows/ci.yml/badge.svg)](https://github.com/shubhamattri/reviewhound/actions/workflows/ci.yml)

A pull-request reviewer that developers can trust. The model proposes findings; code checks
every one of them before anything is posted. It measures its own precision on a replay set.

## How it works

```
GitHub webhook ──> webhook.py ──> SQLite job queue (one job per repo + PR + head commit)
                                        │
                                   worker.py (lease, retry with backoff, dead-letter)
                                        │
                                   pipeline.py
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ fetch PR + diff ─> config from BASE branch (.reviewhound.yml, defaults)        │
   │ risk router ─> tier (skip / light / standard / deep), model, byte budget       │
   │ tree-sitter index of head (Python, TypeScript, JS, Go) + learned conventions   │
   │ context: diff with head line numbers, changed functions, callers, referenced   │
   │          types, recent history, all inside the byte budget                     │
   │ model registry (claude -p, fallback, circuit breaker) ─> JSON findings         │
   │ claim checker: quote exists, evidence is on changed lines, symbols exist,      │
   │                calls / not_calls hold. One false claim drops the finding.      │
   │ rule scanners (built-in + Semgrep if installed): secrets, SQL joins, PII logs  │
   │ policy: severity floor, verified only, one comment per line, max comments,     │
   │         never repeat a posted or declined finding, verdict                     │
   │ post (only if config says posting: true and the head did not move) or store    │
   └──────────────────────────────────────────────────────────────────────────────┘
                                        │
                     store.py: reviews, findings with stable fingerprints, feedback
```

Design rules: the engine has no team words in it (everything team-specific is in
`.reviewhound.yml`); a new repo runs in shadow mode (stores findings, posts nothing) until its
config says `posting: true`; config is read from the base branch so a PR cannot change how
it is reviewed.

## Run it

```bash
uv sync
uv run pytest -q                      # no network needed
uv run reviewhound eval               # replay recorded answers: precision / recall
uv run reviewhound eval --no-checker  # same, with the claim checker switched off
uv run reviewhound eval --live        # ask the real model (uses `claude -p`, your subscription)

# Review a real PR and print the exact review it would post. Token from GITHUB_TOKEN or gh.
uv run reviewhound --gh-user <account> review owner/repo 12 --dry-run

# Service mode
REVIEWHOUND_WEBHOOK_SECRET=... uv run reviewhound serve --port 8787
uv run reviewhound worker
uv run reviewhound enqueue owner/repo 12     # manual / missed-PR sweep
uv run reviewhound feedback owner/repo 12 <fingerprint> declined --detail "intended"
```

Model calls go through `claude -p` with `ANTHROPIC_API_KEY` removed from the environment, in an
empty working directory with only local settings loaded, so no other project's instructions
leak into a review.

## Config (`.reviewhound.yml`, all optional)

```yaml
posting: false            # shadow mode until true
languages: [python, typescript, javascript, go]
ignore_paths: ["**/*.lock", "**/dist/**"]
risk_paths:
  - {pattern: "**/billing/**", tier: deep}
  - {pattern: "docs/**", tier: light}
auth:
  guard_functions: [require_role]
personal_data_terms: [email, phone, dob]
tone_examples: ["Short and direct. Say what breaks and for whom."]
policy:
  min_severity_to_post: minor
  max_comments: 10
  blocking_severity: blocker
  drop_unverified: true
budget:
  context_bytes_standard: 60000
```

Unknown keys or bad values fail the review loudly instead of being guessed.

## Evals

`evals/build_cases.py` writes `evals/cases/*`: 32 small PRs (Python, TypeScript, JavaScript, Go
and one Ruby file) with seeded bugs, six clean PRs, expected findings, and a recorded model
answer that includes deliberate hallucinations (invented helpers, `Owner.member` names that do
not exist, quotes that do not exist, bugs in unchanged code, false "missing guard" claims). Two
cases put the bug on a line the PR deleted, and one has two secrets on identically shaped lines.
Any prompt or checker change must not lower precision on this set.

## Install as a GitHub App

Running as an App means each repo is reviewed with a short-lived installation token instead
of one person's token, and developers' reactions on GitHub flow back as feedback.

1. Create the App. Either fill in the form at *Settings > Developer settings > GitHub Apps*,
   or start from [`app-manifest.json`](app-manifest.json) (set your webhook URL first).
   - Permissions: **Pull requests: read and write**, **Contents: read**, **Metadata: read**.
   - Subscribe to events: **`pull_request`**, **`pull_request_review_thread`**,
     **`pull_request_review_comment`**.
   - Webhook URL: `https://<your host>/webhook`, with a webhook secret.
2. Generate a private key for the App and save the `.pem` file where the worker can read it.
3. Install the App on the repos to review.
4. Run both processes with these environment variables:

| Variable | Used by | What it is |
|---|---|---|
| `REVIEWHOUND_WEBHOOK_SECRET` | `serve` | The App's webhook secret. Required. |
| `REVIEWHOUND_APP_ID` | `serve`, `worker` | The App id. `serve` uses it to ignore the App's own replies. |
| `REVIEWHOUND_APP_PRIVATE_KEY_PATH` | `worker` | Path to the App's private key (`.pem`). |
| `GITHUB_TOKEN` | `enqueue` | Reads the PR head for a manual `enqueue`. The worker does not use it in App mode: a job with no installation id (manual `enqueue`, sweep) looks the installation up by repo, and a repo without the App installed fails the job at once. |

```bash
REVIEWHOUND_WEBHOOK_SECRET=... REVIEWHOUND_APP_ID=123456 uv run reviewhound serve --port 8787
REVIEWHOUND_APP_ID=123456 REVIEWHOUND_APP_PRIVATE_KEY_PATH=~/reviewhound.pem uv run reviewhound worker
```

Set both App variables or neither; half a configuration stops the worker at start. Without
them everything works as before with `GITHUB_TOKEN` or `gh`. The key and tokens are never
logged. Tokens are cached per installation and renewed 5 minutes before they expire.

What the App does with the events:

- `pull_request`: queues a review for the new head, remembering the installation.
- After a review posts, the worker reads back the comment ids GitHub assigned and links each
  one to its finding.
- `pull_request_review_thread`: resolving a thread records the finding as `resolved` (it is
  not posted again); unresolving records `reopened`.
- `pull_request_review_comment`: a reply to one of our comments is recorded as `reply`
  feedback (first 500 characters). Replies made by the App itself are ignored.
- When a new head's review has no open blocking finding, our earlier "changes requested"
  reviews on that PR are dismissed. A failed dismissal is logged, recorded, and retried after
  the next posted review; it never fails the review.

The webhook only writes to the local database; it never calls GitHub.

## Contributing and roadmap

See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md). Licensed under Apache-2.0.

## Not in milestone 1

- Proof by generated failing test in a sandbox (milestone 2).
- Feedback from GitHub events and App installation tokens: done in milestone 3, see
  "Install as a GitHub App".
- Comments on deleted lines; findings must sit on lines the PR added.
- A post that times out is recorded as `post-uncertain` and not retried, so it may be missing
  or present on GitHub; nothing posts it twice automatically.
