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
   │ tree-sitter index of head (Python, TypeScript) + learned conventions           │
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
languages: [python, typescript]
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
  runner: docker          # docker | local (local also needs --allow-local-proof)
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
  major (and is held if that is below `min_severity_to_post`).
- **inconclusive**: anything else, including a timeout, a malformed model answer, Docker not
  running, or a runner crash. Severity is never changed.

A proven finding's comment says so and carries the test in a collapsed `<details>` block. A
proof problem never fails the review.

**This executes code from the pull request.** It is off by default and the repo must turn it on
in `.reviewhound.yml` on its base branch.

- `runner: docker` (default) runs `docker run --rm --network none --read-only --memory 512m
  --cpus 1 --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges` with a tmpfs
  `/tmp`, the tree mounted read-only and copied to a writable dir inside the container. The
  stock `python:3.12-slim` image has no pytest; pass `--proof-image` with an image that does.
  When the Docker daemon is not reachable the proof is inconclusive ("docker unavailable").
- `runner: local` is a plain subprocess with an allowlisted environment (no tokens, `HOME` in
  the temp dir), CPU, file-size and (on Linux) address-space limits, and its process group
  killed on timeout. It is **not a sandbox**. A repo's config cannot enable it alone: the
  operator must also start Reviewhound with `--allow-local-proof`. Use it only for repos whose
  every PR author you would let run code on the machine.
- With `downgrade_unproven: true`, PR code can influence a finding's severity (a test that
  passes downgrades it). Keep it off for repos with untrusted authors.

TypeScript proofs are not built yet (next: a vitest or jest runner behind the same interface).

## Evals

`evals/build_cases.py` writes `evals/cases/*`: 14 small PRs (Python and TypeScript) with seeded
bugs, two clean PRs, expected findings, and a recorded model answer that includes deliberate
hallucinations (invented helpers, quotes that do not exist, bugs in unchanged code, false
"missing guard" claims). Any prompt or checker change must not lower precision on this set.
Cases 13 and 14 turn proof on (local runner, recorded proof tests): 13 must be proven, 14 is a
false blocker whose test passes, so it is downgraded and held. The eval reports any case whose
proof verdict differs from `expected_proofs`.

## Contributing and roadmap

See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md). Licensed under Apache-2.0.

## Not in milestone 1

- Feedback from GitHub events (thread resolved, replies); today `reviewhound feedback` records it.
- GitHub App installation tokens; today a user token (`GITHUB_TOKEN` or `gh`) is used.
- Comments on deleted lines; findings must sit on lines the PR added.
- A post that times out is recorded as `post-uncertain` and not retried, so it may be missing
  or present on GitHub; nothing posts it twice automatically.
