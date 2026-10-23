# Roadmap

Every milestone goes through the same cycle, and is not done until the cycle passes clean.

## The cycle

1. **Build** the milestone, with tests that fail before each change and pass after.
2. **Review**: a fresh reviewer agent and Codex read the code and are told to assume there are
   bugs. Every finding is checked against the code, then fixed with a failing test or rejected
   with a written reason.
3. **QA**:
   - `uv run pytest -q`, `uv run mypy`, `uv run ruff check src tests evals qa`
   - `uv run reviewhound eval` (recorded) and `uv run reviewhound eval --live` (real model):
     precision must not drop.
   - `uv run python qa/github_qa.py --milestone <m>`: opens a fresh PR in the sandbox repo,
     Reviewhound posts a real review, the API check confirms a comment on every planted bug and
     none elsewhere, and Playwright opens the PR in a browser, confirms each comment is shown,
     and records a video.
4. **Review the QA evidence**: watch the video, read the posted comments, read the eval table.
5. **Loop**: anything wrong goes back to step 1. The milestone closes only when steps 2 to 4
   come back clean on the same commit.

## Milestones

| # | Milestone | Done means |
|---|---|---|
| 1 | Core pipeline: queue, index, router, model registry, claim checker, scanners, policy, shadow mode, CLI, eval harness | Real PR reviewed, eval prints precision and recall, suite green |
| 2 | Open source and real-world posting: license, CI, contributing guide, GitHub QA loop with video | Repo public, CI green, QA run passes with video |
| 3 | GitHub App: installation tokens, feedback from review threads and replies, clearing a stale "changes requested", comments on deleted lines | Installed on the sandbox; resolving a thread is stored as feedback and never re-posted |
| 4 | Proof: for blocking bugs, generate a failing test and run it in a sandbox; a proven bug is marked proven | Eval cases with proofs; no unproven blocker posted as proven |
| 5 | Depth: JavaScript and Go, checker fixes deferred from milestone 1 (dotted symbols, blank lines in quotes, unindexed files), eval set of 30+ cases including real public PRs | Live precision >= 0.9 and recall >= 0.8 on the larger set |
| 6 | Operations: Postgres queue and store adapters, metrics (time per stage, cost per PR, failures, backlog), Docker image, deploy guide | Runs from the image against the sandbox; backlog alarm fires in a test |
