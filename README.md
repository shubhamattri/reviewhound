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
   │ proof (opt-in): a generated pytest that must fail on the head, run in a sandbox│
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
proof:                    # runs PR code: read "Proof by failing test" first
  enabled: false
  min_severity: blocker
  runner: docker          # docker | local (local also needs --allow-local-proof-repo)
  timeout_s: 60
  max_findings: 3
  downgrade_unproven: false
```

Unknown keys or bad values fail the review loudly instead of being guessed.

## Proof by failing test (opt-in, runs PR code)

For each posted finding that is verified, at or above `proof.min_severity`, in a Python file and
from the model (not a scanner), Reviewhound asks the model for one pytest file that asserts the
correct behaviour, writes the PR head plus that test into a fresh temp dir and runs it. The
result is read from pytest's `--junitxml` report, never from its console text:

- **proven**: every test failed with `AssertionError`, or with the exception the model said the
  bug raises. Import, syntax, collection and fixture errors never count.
- **not_proven**: the test passed. With `downgrade_unproven: true` a not-proven blocker becomes
  major (and is held if that is below `min_severity_to_post`), but the review's verdict stays
  what it was before proofs (see the threat model below).
- **inconclusive**: anything else, including a timeout, a malformed model answer, Docker not
  running, or a runner crash. Severity is never changed.

A proven finding's comment says "Reproduced by a generated test" and carries the test in a
collapsed `<details>` block. A held, downgraded blocker is still named in the review body. A
proof problem never fails the review, and all proofs in one review share a time budget
(`--max-proof-seconds`, default 300).

**Threat model.** The generated test runs in the same sandbox as the PR's own code, and that
code (a `conftest.py`, a `sitecustomize.py`, an import side effect) can rewrite the report or
make any assertion pass or fail. A proof result is therefore evidence for a human, never a
security decision: it never dismisses a review, never turns REQUEST_CHANGES into COMMENT and
never removes a finding from the verdict. "Reproduced" means the test failed as claimed on
this head, not that the bug is certain.

**This executes code from the pull request.** It is off by default and the repo must turn it on
in `.reviewhound.yml` on its base branch.

- `runner: docker` (default) starts a container from `reviewhound-proof:latest` (build it from
  `Dockerfile.proof`; it is never pulled) as user 65534 with `--network none --read-only
  --memory 512m --cpus 1 --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges`,
  the tree mounted read-only and copied into a tmpfs `/tmp`, and the report in a size-limited
  tmpfs `/out` that is read back through `docker exec` and never touches a host path. The
  worker needs the Docker CLI and socket; see docs/deploy.md. When the Docker daemon is not
  reachable the proof is inconclusive ("docker unavailable").
- `runner: local` is a plain subprocess with an allowlisted environment (no tokens, `HOME` in
  the temp dir), CPU, file-size and (on Linux) address-space limits, and its process group
  killed on timeout. It is **not a sandbox**. A repo's config cannot enable it: the operator
  names each repo that may use it with `--allow-local-proof-repo owner/name`; every other repo
  gets "proof runner 'local' is not enabled on this server". List only repos whose every PR
  author you would let run code on the machine.
- With `downgrade_unproven: true`, PR code can influence a finding's label and whether its
  comment is posted (a test that passes downgrades it), though never the verdict. Keep it off
  for repos with untrusted authors.

TypeScript proofs are not built yet (next: a vitest or jest runner behind the same interface).

## Evals

`evals/build_cases.py` writes `evals/cases/*`: 34 small PRs (Python, TypeScript, JavaScript, Go
and one Ruby file) with seeded bugs, six clean PRs, expected findings, and a recorded model
answer that includes deliberate hallucinations (invented helpers, `Owner.member` names that do
not exist, quotes that do not exist, bugs in unchanged code, false "missing guard" claims). Two
cases put the bug on a line the PR deleted, and one has two secrets on identically shaped lines.
Any prompt or checker change must not lower precision on this set.
Cases 33 and 34 turn proof on (local runner, recorded proof tests): 33 must be proven, 34 is a
false blocker whose test passes, so it is downgraded and held. The eval reports any case whose
proof verdict differs from `expected_proofs`.

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
| `REVIEWHOUND_ALLOWED_OWNERS` | `serve` | Comma-separated repo owners (users or orgs) to accept events for; `--allowed-owner` adds more. Others get a 202 "ignored" and no job. Unset accepts every owner, and `serve` warns at start. **Set it.** |
| `REVIEWHOUND_METRICS_TOKEN` | `serve` | `/metrics` then needs `Authorization: Bearer <token>`; anything else gets 401. Unset leaves `/metrics` open, and `serve` warns at start. **Set it.** |
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

- Proof by generated failing test: done in milestone 4, see "Proof by failing test".
- Feedback from GitHub events and App installation tokens: done in milestone 3, see
  "Install as a GitHub App".
- Comments on deleted lines: done in milestone 5 (posted on the LEFT side).
- A post that times out is recorded as `post-uncertain` and not retried, so it may be missing
  or present on GitHub; nothing posts it twice automatically.
