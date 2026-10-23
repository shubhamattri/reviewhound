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

Model calls go through `claude -p` in an empty working directory with only local settings loaded,
so no other project's instructions leak into a review. The `claude` process gets only `PATH`,
`HOME`, `USER`, `LOGNAME`, `LANG`, `TMPDIR`, `SHELL`, `CLAUDE_CODE_OAUTH_TOKEN` and
`CLAUDE_CONFIG_DIR`: never `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, the database URL or the webhook
secret. `git` and `semgrep` get `PATH`, `HOME`, `LANG`, the proxy variables (`HTTPS_PROXY`,
`HTTP_PROXY`, `NO_PROXY`, either case) and `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.

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
(`--max-proof-seconds`, default 300). The budget counts the Docker runner's own worst case
(`docker info`, start, exec slack, two read-backs and `rm`: 115 s with the default 30 s start
grace) on top of each test's timeout, so a budget under about 116 s runs no Docker proof.

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

### Accuracy: two different numbers

- **Replayed-answer eval** (`reviewhound eval`): the model's answers are recorded, so this
  measures the claim checker and policy only. It reads `precision 1.00, recall 1.00` on 36
  cases. That is a regression test for the checker, **not** the tool's accuracy: the recorded
  answers were written with the seeded bugs in mind.
- **Live eval** (`reviewhound eval --live`): asks the real model on the same cases, so it
  measures model plus checker. This is the number to quote.

  Live eval on 2026-10-24 (real model through `claude -p`, checker on, 36 cases): precision 1.00, recall 0.97 (29 of 30 planted bugs; the miss was a JavaScript loop that skips its first row, which the model did not report), about $0.018 per PR. Two proof cases were skipped because no Docker sandbox was available. Live results vary from run to run; runs during development scored recall between 0.86 and 1.00 as the checker changed.

### Alternative fair anchors

A few bugs can fairly be flagged on more than one line. An expected finding may list `also`
anchors; a report on any of them counts, and each expected finding matches **once only**, so
reporting both anchors gives one true positive and one false alarm. Three cases use it:

- **24 (Go response body not closed):** the leak can be flagged where the response is opened
  or where its body is read without being closed.
- **29 (Python role check removed):** the guard is gone, so it can be flagged on the deleted
  line (LEFT) or on the head line right after the gap.
- **32 (Go deferred unlock removed):** same shape: the deleted `defer` line, or the `Lock()`
  line it used to follow.

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
| `REVIEWHOUND_ALLOWED_OWNERS` | `serve` | Comma-separated repo owners (users or orgs) to accept events for; `--allowed-owner` adds more. Others get a 202 "ignored" and no job. Unset (or empty) accepts every owner, and `serve` warns at start. Set to only spaces or commas, `serve` refuses to start. **Set it.** |
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

The webhook only writes to the local database; it never calls GitHub. A request body must
arrive within 30 seconds in total (and no single read may stall past 10 seconds); a slower
client gets 408 and its connection is closed.

How one posting job ends:

| What happened | Review outcome | Job | Head claim |
|---|---|---|---|
| Posted (or posted with comments moved into the body after a 400/422) | `posted` | done | kept, with the review id |
| Before each POST, GitHub already shows our review of this head (an earlier run crashed after posting) | `posted` (adopted: not posted again; only findings GitHub shows are marked posted) | done | kept, with that review's id |
| PR head moved, checked right before each POST | `head-moved` | done | freed |
| Lease lost, checked right before each POST | `lease-lost` | retried | freed |
| Another run holds the claim with no id, younger than one queue lease, nothing on GitHub | `claim-busy` | retried once the claim can be stale | the other run's |
| Another run's claim with no id is older than one queue lease | taken over; the GitHub check runs before the POST | as for a normal post | this run's |
| 400/422 again after the body retry | `post-rejected` | dead, with GitHub's reason | freed |
| 401, 403, 408, 429 on the POST | (raised) | retried | freed |
| 404 or another 4xx on the POST | (raised) | dead | freed |
| 5xx, timeout: it may have landed | `post-uncertain` | done | kept, never posted twice |
| Anything raised before the POST (token lookup, head lookup) | (raised) | by error class | freed |
| Posted, but recording it failed | (raised) | retried; before any model call the retry records the post with the verdict that was posted, then finishes the follow-ups (outcome `recovered`) without posting again | kept |

Known limit: a resolve event redelivered byte for byte after an unresolve of the same thread is
treated as already applied, so the finding stays `reopened`. GitHub sends a fresh body (new
timestamps) for a real second resolve, so this needs a manual redelivery to happen.

## Open risks

Known gaps that remain after fix round 4, in plain words:

- **An uncertain post is not chased.** A POST that times out or gets a 5xx is recorded as
  `post-uncertain`, its findings as posted, and the job ends. If it did not land, that head has
  no review until the next push.
- **The GitHub check trusts what GitHub lists.** If GitHub accepted a review but does not list
  it yet, a run that took over a stale claim could post a second one.
- **Unknown account.** When the token cannot say which account it is (`GET /user` failed), any
  author's review of this head whose body starts with "Reviewhound (" counts as ours.
- **A stalled worker.** If a worker freezes for longer than one queue lease and then wakes right
  before its POST, another run may already have taken over its claim; both could post.
- **A busy claim uses up attempts.** Each `claim-busy` retry counts as an attempt; with a very
  short lease and many retries a job can go dead (its error says why).
- **Adopted or recovered reviews use this run's answer.** The verdict, id and comments come from
  GitHub, but the blockers and the finding list come from this run's model answer. A comment
  GitHub shows that matches none of them stays unlinked (logged), so replies to it are ignored.
- **Recording can fail twice.** If saving the post id and saving the review both fail after a
  POST, the next run finds the review on GitHub and adopts it; with a VCS that cannot list
  reviews it would not.
- **Dismissal is evidence based but narrow.** A blocker's quote that only survives in an ignored,
  non-indexed or past-the-5,000-file-cap file counts as gone. A removed-guard blocker counts as
  fixed only when the same deleted lines come back in the same file; an equivalent fix written
  differently keeps the old verdict.
- **Findings next to a deletion need a true claim.** A real removed-guard finding without a
  confirming claim is held as unverified; whether the live model supplies one is measured by
  the live eval, not assumed.
- **Docker proofs and budget.** `--max-proof-seconds` below about 116 s disables Docker proofs
  (warned at start). The `claude` child gets no proxy or CA variables. Header bytes trickled
  slowly can still hold a webhook thread.
- **Byte-identical resolve redelivery** after an unresolve is treated as already applied (see
  the known limit above).

## Contributing and roadmap

See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md). Licensed under Apache-2.0.

## Not in milestone 1

- Proof by generated failing test: done in milestone 4, see "Proof by failing test".
- Feedback from GitHub events and App installation tokens: done in milestone 3, see
  "Install as a GitHub App".
- Comments on deleted lines: done in milestone 5 (posted on the LEFT side).
- A post that times out is recorded as `post-uncertain` and not retried, so it may be missing
  or present on GitHub; nothing posts it twice automatically. A later run for that head checks
  GitHub first and adopts the review if it is there.
