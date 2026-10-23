# Fix round 1, Track A: sandbox and operations

Branch `fix-track-a`, based on local `main` at 391918d. Files touched: proof.py, heartbeat.py,
worker.py, cli.py, Dockerfile, Dockerfile.proof (new), docker-compose.yml,
docker-compose.app.yml (new), docs/deploy.md, README.md, poster.py (proof label only).
Outside Track A: one minimal edit in **pipeline.py** (see A5) for the `should_post` hook.

## Findings, fixes, tests, results

| # | Finding | Fix | Tests | Result |
|---|---|---|---|---|
| A1 | A repo's own config could turn on local code execution | `--allow-local-proof` removed. `--allow-local-proof-repo owner/name` (repeatable, validated, case-insensitive) is the only way a repo gets `LocalRunner`. Runners are built per job (`_worker_reviewer_factory`, `_app_reviewer_factory`, `cmd_review`), so the check uses the job's repo. `--max-proof-seconds` (default 300) wraps every runner in `BudgetedRunner`: each run's timeout is `min(timeout_s, remaining)`, time is charged even when a run raises, and once under 1s is left the run is refused and `Prover` skips the model call. | `test_proof_budget.py`: `test_local_runner_only_for_repos_the_operator_listed`, `test_repo_config_alone_cannot_get_the_local_runner`, `test_bad_proof_flags_are_refused[5]`, `test_worker_factory_gives_each_job_its_own_runners_and_budget`, `test_budget_shrinks_each_timeout_and_then_refuses`, `test_time_spent_by_a_crashing_run_still_counts`, `test_exhausted_budget_skips_the_model_call` | pass |
| A2a | `_tail` opened a planted FIFO (hang) or followed a symlink (`/dev/zero`: unbounded read) | One reader, `_read_untrusted`: `O_NOFOLLOW \| O_NONBLOCK`, regular files only, reads at most `limit` bytes (tail seeks to `size - limit`). `_read_junit` and `_tail` both use it. | `test_output_tail_refuses_fifo_and_symlink_and_caps_size`, `test_planted_output_file_does_not_hang_or_flood_the_host[fifo,symlink]` (real pytest subprocess that swaps its own output file), existing junit FIFO/symlink tests | pass |
| A2b | A wedged job renewed its lease forever | `LeaseHeartbeat(max_job_seconds=1800)`: measured from `__enter__`; `lost` is true at once when the cap passes (not only at the next beat); the thread stops renewing and logs `lease_lost`. `worker --max-job-seconds` wires it. | `test_heartbeat_stops_renewing_at_the_wall_clock_cap`, `test_heartbeat_thread_ends_by_itself_after_the_cap`, `test_cap_is_measured_from_entering_not_from_construction`, `test_worker_cli_passes_the_job_time_cap` | pass |
| A3 | Docker proof: writable host bind mount, root user, unpinned image | `docker run -d --pull never --user 65534:65534 --network none --read-only --cap-drop ALL --security-opt no-new-privileges`, memory/CPU/pids limits, tree as the only host mount (read-only, chmod 755/644 so nobody can read it whatever the umask), `/tmp` and `/out` as size-limited tmpfs. Test runs via `docker exec`; report and output read back via `docker exec` with a script that refuses symlinks/FIFOs/devices and caps bytes (`head -c`/`tail -c`); a hanging read times out as unreadable; `docker rm -f` always in `finally`, failure logged. `sleep` keepalive outlives the test so a leaked container ends itself. `Dockerfile.proof` (python:3.12-slim + `pytest==8.3.5`, user 65534) is the default `reviewhound-proof:latest`. `_run_command` decodes with `errors="replace"`. | `test_docker_runner_uses_a_locked_down_container_and_cleans_up`, `test_tree_is_readable_by_the_unprivileged_container_user`, `test_docker_timeout_removes_the_container`, `test_container_that_does_not_start_is_inconclusive_and_removed[3]`, `test_report_read_inside_the_container_is_classified[3]`, `test_the_in_container_read_script_refuses_links_and_fifos_and_caps` (runs the real script with local `sh`), `test_a_report_read_that_hangs_is_unreadable_and_the_container_still_removed`, `test_container_left_behind_is_logged`, `test_oversized_junit_written_by_the_test_is_not_parsed`, `test_default_proof_image_is_the_pinned_reviewhound_image` | pass (fake command runner) |
| A4 | PR code in the sandbox can forge a proof and relax the review | `run_proof_stage` saves `decision.event` before proving and restores it in `finally`; the floor is re-applied but the verdict is never recomputed. Label is now "Reproduced by a generated test". A blocker held after an unreproduced proof is still named in the review body. README has the threat model. | `test_not_proven_blocker_is_downgraded_only_when_configured`, `test_downgrade_below_the_posting_floor_holds_the_finding`, `test_floor_and_verdict_are_reapplied_even_if_proving_crashes`, `test_unproven_blocker_is_downgraded_but_the_verdict_is_kept`, `test_held_unreproduced_blocker_keeps_the_verdict_and_is_named_in_the_body`, label tests | pass |
| A5 | Worker ignored `heartbeat.lost`, so a lease-less review still posted | Worker passes `should_post=lambda: not hb.lost`. Outcome `lease-lost` is not completed: the job is failed as retryable (`LeaseLost: ...`), so it is retried or ends `dead`. | `test_worker_past_the_cap_skips_posting_and_requeues_the_job`, `test_worker_within_the_cap_completes`, `test_pipeline_posts_nothing_when_should_post_says_no` | pass |
| A6 | Compose had no App mode; `/metrics` open on the published port | `docker-compose.app.yml` overlay: `REVIEWHOUND_APP_ID` for serve and worker, key file mounted as compose secret `/run/secrets/github_app_key`. Base `GITHUB_TOKEN` optional. Serve gets `REVIEWHOUND_METRICS_TOKEN` (and `REVIEWHOUND_ALLOWED_OWNERS`, Track B's B1) from the environment. Only serve's webhook port is published. | `test_deploy_files.py` (4 tests); `docker compose config -q` passes for token mode, and the App overlay merges to the expected env and secret | pass |
| A7 | `claude-code@latest` in the image | `ARG CLAUDE_CODE_VERSION=2.1.299` (the version installed on this machine from npm). The uv image was already pinned (0.10.9). | `test_dockerfile_pins_uv_and_the_claude_cli` | pass |

## Commands run (final state)

- SQLite: `398 passed, 61 skipped`
- Postgres 14.20 (own `initdb`, TCP 127.0.0.1:55459, stopped afterwards): `459 passed`
- `uv run mypy`: no issues in 37 files. `uv run ruff check src tests evals qa`: all passed.
- Eval (recorded, `run_eval`): `cases: 34  precision: 1.00  recall: 1.00  dropped by claim checker: 32`

## Mutation table

Each mutation applied alone, named tests run, file restored from a backup and `cmp`-identical.

| # | Mutation | Killed by |
|---|---|---|
| M1 | `_read_untrusted` reads non-regular files | `test_output_tail_refuses_fifo_and_symlink_and_caps_size`, `test_planted_fifo_or_symlink_report_is_not_read` |
| M2 | open without `O_NOFOLLOW` | `test_output_tail_refuses...`, `test_report_symlinked_to_a_regular_file_outside_is_not_followed` |
| M3 | tail reads from offset 0 (no cap) | `test_output_tail_refuses_fifo_and_symlink_and_caps_size` |
| M4 | heartbeat `tick` ignores the cap | `test_heartbeat_stops_renewing_at_the_wall_clock_cap`, `test_heartbeat_thread_ends_by_itself_after_the_cap` |
| M5 | `lost` ignores the cap until the next beat | `test_heartbeat_stops_renewing...`, `test_cap_is_measured_from_entering...`, `test_worker_past_the_cap_skips_posting_and_requeues_the_job` |
| M6 | cap measured from construction | `test_cap_is_measured_from_entering_not_from_construction` |
| M7 | worker passes `should_post=lambda: True` | `test_worker_past_the_cap_skips_posting_and_requeues_the_job` |
| M8 | worker completes a `lease-lost` job | `test_worker_past_the_cap_skips_posting_and_requeues_the_job` |
| M9 | pipeline ignores `should_post` | `test_pipeline_posts_nothing_when_should_post_says_no` |
| M10 | proof stage recomputes the verdict after downgrades | 5 tests incl. `test_unproven_blocker_is_downgraded_but_the_verdict_is_kept`, `test_floor_and_verdict_are_reapplied_even_if_proving_crashes` |
| M11 | label back to "Proven" | `test_proven_comment_says_so_and_folds_the_test_in_details`, `test_real_bug_is_proven_posted_with_the_test_and_timed` |
| M12 | held unreproduced blocker not named in the body | `test_held_unreproduced_blocker_keeps_the_verdict_and_is_named_in_the_body` |
| M13 | container without `--user 65534:65534` | `test_docker_runner_uses_a_locked_down_container_and_cleans_up` |
| M14 | tree mounted `:rw` | `test_docker_runner_uses_a_locked_down_container_and_cleans_up` |
| M15 | read script follows symlinks | `test_the_in_container_read_script_refuses_links_and_fifos_and_caps` |
| M16 | container never removed | 7 tests incl. `test_docker_timeout_removes_the_container`, `test_container_that_does_not_start_is_inconclusive_and_removed[*]` |
| M17 | no chmod of the tree | `test_tree_is_readable_by_the_unprivileged_container_user` |
| M18 | `--pull never` dropped | `test_docker_runner_uses_a_locked_down_container_and_cleans_up` |
| M19 | in-container junit not size-checked | `test_oversized_junit_written_by_the_test_is_not_parsed` |
| M20 | local runner for every repo | `test_local_runner_only_for_repos_the_operator_listed`, `test_repo_config_alone_cannot_get_the_local_runner`, `test_worker_factory_gives_each_job_its_own_runners_and_budget` |
| M21 | repo match case-sensitive | `test_local_runner_only_for_repos_the_operator_listed` |
| M22 | per-run timeout not capped by the budget | `test_budget_shrinks_each_timeout_and_then_refuses` |
| M23 | time never charged | `test_budget_shrinks_each_timeout_and_then_refuses`, `test_time_spent_by_a_crashing_run_still_counts` |
| M24 | prover asks the model with no budget left | `test_exhausted_budget_skips_the_model_call` |
| M25 | `--max-proof-seconds 0` / negative accepted | `test_bad_proof_flags_are_refused[argv3,argv4]` |
| M26 | any repo string accepted | `test_bad_proof_flags_are_refused[argv1,argv2]` |
| M27 | `CLAUDE_CODE_VERSION=latest` | `test_dockerfile_pins_uv_and_the_claude_cli` |
| M28 | postgres port published | `test_only_the_webhook_port_is_published` |
| M29 | `GITHUB_TOKEN` required again | `test_token_mode_needs_no_app_and_app_mode_needs_no_token` |
| M30 | metrics token not wired | `test_token_mode_needs_no_app_and_app_mode_needs_no_token` |

## Not verified, and why

- **No real Docker run.** Every DockerRunner test uses the fake command runner; the daemon was
  not used. Unverified on a real daemon: `--pull never` with a local image, `--tmpfs ...,mode=1777`
  being writable by uid 65534, `docker exec` inheriting the container's user, env and workdir,
  `head`/`tail -c` in the proof image, the exec exit code being pytest's, and `rm -f` timing.
  The read-back script itself was run with local `sh` (macOS) and does what the table says.
- **Deviation from the brief, on purpose: no `docker cp`.** A `--tmpfs` mount is discarded when
  the container stops, so the brief's "run, exit, then `docker cp`" would find no report. I
  also believe (not verified) that `docker cp` reads the container's filesystem from the daemon
  side and does not see a tmpfs at all. So the container stays alive on a bounded `sleep`, the
  test runs with `docker exec`, and the report is read with a capped `docker exec head -c`.
  Same guarantees (no host write, size cap, `rm -f`), different mechanism. Check on a real
  daemon before relying on it.
- **`Dockerfile.proof` was not built.** `pytest==8.3.5` is a real release; the build was not run.
- **The worker-in-a-container proof setup** (socket plus same-path TMPDIR) is documented only.
  The shipped worker image has no Docker CLI.
- **Compose** was checked with `docker compose config` only (client-side merge and
  interpolation). Nothing was built or started. The key-file permission advice (uid 10001)
  is untested.
- **A wedged review thread is not killed.** The cap stops renewal so another worker can take
  the job; the stuck thread in the old worker stays stuck until that process restarts.

## Risks for whoever merges

- **pipeline.py** (Track B's file): the only edit is the `should_post` keyword on
  `Reviewer.review` and one `elif ... result.outcome = "lease-lost"` before the head-moved check.
  If Track B's version lands, keep theirs; the tests here need only that signature and outcome.
- `--allow-local-proof` is gone (argparse now rejects it). An operator using it must switch to
  `--allow-local-proof-repo`. Default `--proof-image` changed to `reviewhound-proof:latest`,
  which must be built first; until then Docker proofs are inconclusive ("docker run failed").
- With `downgrade_unproven` and a high posting floor, a review can be REQUEST_CHANGES with no
  inline comment; the held blocker is listed in the body so the reason is visible.
- `REVIEWHOUND_ALLOWED_OWNERS` is passed through compose but implemented by Track B (B1).
- Base image `python:3.12-slim` and apt's Node/npm are pinned by tag/distribution only, not
  digest.
- Process note: my first Postgres attempt chose port 55447, which another session's server was
  already using; one full test run (`459 passed`) went to that server's `rhtest` database before
  I noticed. I did not stop or modify that server. The reported Postgres run is from my own
  server on 55459.
