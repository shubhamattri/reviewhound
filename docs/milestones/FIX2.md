# Fix round 2: the posting path as one guarded unit, dismissal evidence, hardening

Branch `fix-round-2`, based on local `main` at a9a639c. Every store and queue change is covered
by `backend`-fixture tests that run on SQLite and on a throwaway Postgres 14.20 (`initdb` in the
scratch dir, TCP only, `-k ''`, port 55477, database `rhtest`; port checked free with `nc -z`).

## The posting state machine (one job)

```
claim job -> review -> claim_post -> [own-PR check] -> per attempt: head check, lease check -> POST
          -> mark_posted (own tx) -> record_review (+ review_blockers) -> follow-ups (link, dismiss)
```

| Arrow | Failure | Job | Head claim (`posts`) | Store |
|---|---|---|---|---|
| review | any exception | failed, retryable by class | none taken | nothing |
| claim_post | DB error | failed, by class | rolled back | nothing |
| claim_post | already held, `post_id` set, no posted record | done (recovery) | kept | review recorded, comments linked, stale verdicts dismissed |
| claim_post | already held, no `post_id` | done | kept | `already-posted` row |
| own-PR check | `viewer_login` raises | failed, by class (token lookup is retryable) | **freed** (finally) | nothing |
| head check | head lookup raises | failed, by class | **freed** | nothing |
| head check | head moved (before either attempt) | done | **freed** | `head-moved` row |
| lease check | lease lost (before either attempt; read after the head lookup) | failed, retryable | **freed** | `lease-lost` row |
| POST | 400/422 with inline comments, attempt 1 | - | freed for the moment | loops to the checks with comments in the body (capped at 60,000 chars) |
| POST | 400/422 on the retry, or with no comments | dead, `post-rejected: <reason>` | **freed** | `post-rejected` row |
| POST | 401/403/408 | requeued (`PostRefused`) | **freed** | nothing |
| POST | 404 / other non-429 4xx | dead (`PostRefused`) | **freed** | nothing |
| POST | 429, 5xx, transport, other exception | done | kept (may have landed) | `post-uncertain`, posted=1 |
| POST | KeyboardInterrupt / SystemExit | propagates | kept (`keep` is set before the request) | nothing |
| mark_posted | DB error | continues, logged | kept, no id | the review row carries the id |
| record_review | DB error after a landed post | requeued (`RecordAfterPostError`) | kept, with id | retry recovers (row 3) |
| follow-ups | link or dismissal error | done | kept | logged; dismissal failure recorded |

## Items -> fix -> test -> result

Tests are in `tests/test_fix_round2.py` unless named. "both" = SQLite and Postgres.

| # | Fix | Tests | Result |
|---|---|---|---|
| P1 | The head and lease checks moved from before the claim into `_blocked()`, run right before EVERY POST (first try and body retry). Head is looked up first, the lease is read last. Failures give `head-moved` / `lease-lost`, nothing posted. | `test_head_moving_after_the_claim_posts_nothing_and_frees_the_claim`, `test_lease_lost_after_the_claim_posts_nothing_and_frees_the_claim`, `test_lease_lost_during_the_head_lookup_posts_nothing`, `test_body_retry_never_posts_against_a_superseded_head`, `test_head_is_read_after_the_claim_not_before`; round 1's `test_lease_lost_after_a_refusal_skips_the_retry` | pass, both |
| P2 | `_post` wraps everything after `claim_post` in try/finally with a `_Claim.keep` flag. `keep` is set just before the request is sent and cleared on a 4xx refusal, so any exception before the POST (viewer_login, token, head lookup) frees the claim and re-raises. | `test_viewer_login_failure_frees_the_claim_and_reraises`, `test_head_lookup_failure_frees_the_claim_and_reraises`, `test_failure_before_the_post_requeues_the_job_and_the_retry_posts`, `test_interrupt_during_the_post_keeps_the_claim` | pass, both |
| P3 | Only 400/422 trigger the body retry. Other non-429 4xx raise `PostRefused(GitHubError)`, claim freed. `is_retryable`: `PostRefused` 401/403/408 retry, others permanent. Plain `GitHubError` 401 (installation lookup) stays permanent. `comments_in_body` caps the body at 60,000 chars with an "N more findings not shown." line, trimming an oversized base body too. | `test_http_400_is_retried_in_the_body`, `test_other_refusals_raise_free_the_claim_and_never_retry_in_the_body` (401, 403, 408, 404, 409, 410), `test_refusal_of_the_body_retry_with_403_raises_and_frees_the_claim`, `test_worker_requeues_auth_and_timeout_refusals_and_kills_the_rest`, `test_installation_lookup_401_stays_permanent`, `test_body_retry_is_capped_and_counts_what_it_left_out`, `test_body_retry_keeps_every_comment_that_fits`, `test_body_retry_fills_up_to_the_exact_limit`, `test_body_retry_trims_an_oversized_body_itself`, `test_capped_retry_is_what_gets_posted` | pass, both |
| P4 | Worker: `post-rejected` fails the job as permanent with `post-rejected: <GitHub reason>`; `lease-lost` still fails retryable; every other outcome completes. | `test_post_rejected_after_the_body_retry_is_a_dead_job_with_its_reason`, `test_head_moved_and_already_posted_complete_the_job`, `test_lease_lost_still_fails_retryable` | pass, both |
| P5 | Nullable `posts.post_id` (SQLite `ALTER TABLE` in `Store._migrate`, Postgres `ADD COLUMN IF NOT EXISTS`). `mark_posted` writes it in its own transaction right after the POST, before `record_review`. A record failure after a landed post raises retryable `RecordAfterPostError`. On the retry, `claim_post` is false and `unrecorded_post` finds the id with no `posted` review row: outcome `posted` (error says recovered), the review is recorded and the follow-ups run. No id: `already-posted` as before. | `test_post_id_is_written_before_the_review_record`, `test_record_failure_after_the_post_is_retryable_and_the_retry_recovers`, `test_recovery_runs_the_dismissal_too`, `test_worker_retries_a_record_failure_and_completes_without_posting_twice`, `test_mark_failure_is_logged_and_the_review_still_records`, `test_claim_without_a_post_id_stays_already_posted`, `test_recorded_post_is_not_recovered_twice`, `test_unrecorded_post_store_contract`, `test_mark_posted_without_a_claim_still_records_the_id`, `test_old_sqlite_posts_table_gets_the_post_id_column`, `test_old_postgres_posts_table_gets_the_post_id_column` | pass, both |
| D1 | `blocking_snapshot()` copies each blocking finding (fingerprint, file, side, category, quote, identity) right after `apply_policy`, before the proof stage. `record_review(..., blockers)` writes them to `review_blockers` keyed by post id (posted reviews with an id only). Dismissal reads `review_blockers`; reviews recorded before this table fall back to `review_findings` filtered by severity. | `test_blocker_changed_by_a_proof_is_still_dismissed_once_gone` (downgrade, hold), `test_blocker_changed_by_a_proof_keeps_the_verdict_while_present` (downgrade, hold), `test_only_request_changes_reviews_keep_blockers`, `test_blockers_are_written_only_for_a_posted_review_with_an_id`, `test_legacy_review_without_blocker_rows_falls_back_to_posted_findings` | pass, both |
| D2 | `open_reason(row, head, changed, sources)`: an absent quote in its own file is not enough; every other file in the head index (`ReviewResult.head_sources`) and every changed file is searched. | `test_quote_still_in_an_unchanged_file_keeps_the_verdict`, `test_open_reason_searches_every_indexed_file` (6 cases); round 1's 10 `open_reason` cases | pass, both |
| H1 | `serve` exits with a clear message when `REVIEWHOUND_ALLOWED_OWNERS` is non-empty but names no owner. Empty string is treated as unset, because compose passes `${REVIEWHOUND_ALLOWED_OWNERS:-}`. | `test_serve_refuses_an_allowlist_that_names_no_owner` (4 values), `test_serve_starts_when_the_allowlist_is_unset_empty_or_named` | pass |
| H2 | `_read_body` reads with `read1` against a 30 s total deadline (`make_server(body_deadline_s=30.0)`), each read's socket timeout capped by what is left. Past the deadline: 408, connection closed. A per-read stall still closes as before. | `test_body_trickled_past_the_deadline_gets_408_and_frees_the_thread` (real socket, one byte per 0.2 s), `test_default_body_deadline_is_thirty_seconds`, `test_body_sent_in_pieces_within_the_deadline_is_accepted`; round 1's `test_slow_body_times_out_and_queues_nothing` | pass |
| H3 | `procenv.CLAUDE_KEYS` + `claude_env()`; `ClaudeCliProvider._env` uses it for `complete` and `healthy`. | `test_claude_child_gets_only_its_login_and_shell_basics`, `test_claude_child_skips_unset_keys`; `test_providers.py` | pass. Live `claude -p` NOT run (manager verifies). |
| H4 | `SAFE_KEYS` adds the six proxy variables and `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`. | `test_git_and_semgrep_keep_proxy_and_ca_settings`; round 1's exact-env git/semgrep tests | pass |
| H5 | `_Pg._write(body)` runs one transaction and reruns it once on sqlstate 40P01/40001; a second failure or any other error raises. Every Postgres write (queue, store incl. webhook `enqueue` and `apply_feedback`, metrics, migrations) goes through it. | `test_postgres_write_retries_once_and_commits` (both sqlstates), `test_postgres_write_failing_twice_raises_and_writes_nothing` (both), `test_postgres_write_does_not_retry_other_errors`, `test_webhook_enqueue_rides_out_one_deadlock` | pass, Postgres |
| H6 | `DockerRunner.overhead_s` = info 15 + start grace + exec slack 10 + 2 reads x 15 + rm 30 (named constants, the same ones the calls use). `BudgetedRunner` gives the test `min(timeout, remaining - overhead)` and refuses below 1 s, so a worst-case run never exceeds the budget. | `test_docker_overhead_is_the_sum_of_its_step_timeouts`, `test_worst_case_docker_run_stays_inside_the_budget`, `test_budget_smaller_than_docker_overhead_refuses_before_any_docker_call`; `test_proof_budget.py` | pass |
| Doc | Byte-identical resolve re-delivery after an unresolve: README "Known limit". | n/a | documented |

Changed round-1 tests: `test_rejected_twice_is_rejected_and_frees_the_head` now refuses with 400
twice (a 403 on the retry now raises, covered by the new 403 test); `open_reason` gained its
`sources` argument.

## Final runs

- SQLite: see the final report line; Postgres: see the final report line.
- `uv run mypy`: no issues in 38 source files. `uv run ruff check src tests evals qa`: clean.
- Recorded eval: `cases: 34  precision: 1.00  recall: 1.00  dropped by claim checker: 32`

## Mutation table

Script: back the file up, apply one mutation, run `tests/test_fix_round2.py` (plus the named
extra file) with Postgres on, `-x`, restore from the backup, `cmp` the file against the backup.
48 mutations, 47 killed. The survivor (D1c) showed the `REQUEST_CHANGES` check in
`blocking_snapshot` was redundant (`apply_policy` requests changes exactly when a blocking
finding is posted), so the check was removed rather than pinned.

| Mutation | Killed by | Restore |
|---|---|---|
| P1a head check removed | `test_head_moving_after_the_claim_posts_nothing_and_frees_the_claim` | cmp ok |
| P1b lease check removed | `test_lease_lost_after_the_claim_posts_nothing_and_frees_the_claim` | cmp ok |
| P1c checks only before attempt 1 | `test_body_retry_never_posts_against_a_superseded_head` | cmp ok |
| P1d lease read before the head lookup | `test_lease_lost_during_the_head_lookup_posts_nothing` | cmp ok |
| P2a no release in finally | `test_head_moving_after_the_claim_posts_nothing_and_frees_the_claim` | cmp ok |
| P2b keep flag set after the POST | `test_interrupt_during_the_post_keeps_the_claim` | cmp ok |
| P2c refusal does not clear keep | `test_body_retry_never_posts_against_a_superseded_head` | cmp ok |
| P3a every 4xx retried in body | `test_other_refusals_raise_free_the_claim_and_never_retry_in_the_body[401]` | cmp ok |
| P3b 400 not in body-retry set | `test_http_400_is_retried_in_the_body` | cmp ok |
| P3c 401/403/408 not retryable | `test_other_refusals_raise_..._in_the_body[401]` | cmp ok |
| P3d PostRefused treated as plain GitHubError | `test_other_refusals_raise_..._in_the_body[401]` | cmp ok |
| P3e no body cap | `test_body_retry_is_capped_and_counts_what_it_left_out` | cmp ok |
| P3f trailer not reserved | `test_body_retry_fills_up_to_the_exact_limit` | cmp ok |
| P3g oversized base body not trimmed | `test_body_retry_trims_an_oversized_body_itself` | cmp ok |
| P3h all-fit shortcut removed | `test_body_retry_fills_up_to_the_exact_limit` | cmp ok |
| P4a post-rejected completes the job | `test_post_rejected_after_the_body_retry_is_a_dead_job_with_its_reason` | cmp ok |
| P4b post-rejected requeued | `test_post_rejected_after_the_body_retry_is_a_dead_job_with_its_reason` | cmp ok |
| P5a post id never marked | `test_post_id_is_written_before_the_review_record` | cmp ok |
| P5b record failure not wrapped | `test_record_failure_after_the_post_is_retryable_and_the_retry_recovers` | cmp ok |
| P5c RecordAfterPostError not retryable | `test_record_failure_after_the_post_is_retryable_and_the_retry_recovers` | cmp ok |
| P5d no recovery on already-posted | `test_record_failure_after_the_post_is_retryable_and_the_retry_recovers` | cmp ok |
| P5e SQLite recovery ignores the review record | `test_record_failure_..._recovers[sqlite]` | cmp ok |
| P5f Postgres recovery ignores the review record | `test_record_failure_..._recovers[postgres]` | cmp ok |
| P5g SQLite migration skipped | `test_old_sqlite_posts_table_gets_the_post_id_column` | cmp ok |
| P5h Postgres migration skipped | `test_head_moved_and_already_posted_complete_the_job[postgres-already-posted]` | cmp ok |
| D1a no blocker snapshot | `test_blocker_changed_by_a_proof_is_still_dismissed_once_gone[downgrade]` | cmp ok |
| D1b dismissal reads posted findings only | `test_blocker_changed_by_a_proof_is_still_dismissed_once_gone[downgrade]` | cmp ok |
| D1c snapshot ignores the verdict | SURVIVED: redundant check, removed | cmp ok |
| D1d blockers stored for any outcome | `test_blockers_are_written_only_for_a_posted_review_with_an_id` | cmp ok |
| D1e no legacy fallback | `test_legacy_review_without_blocker_rows_falls_back_to_posted_findings` | cmp ok |
| D2a only changed files searched | `test_quote_still_in_an_unchanged_file_keeps_the_verdict` | cmp ok |
| D2b own file present ends the search | `test_quote_still_in_an_unchanged_file_keeps_the_verdict` | cmp ok |
| H1 empty allowlist allowed | `test_serve_refuses_an_allowlist_that_names_no_owner[' ']` | cmp ok |
| H1b empty string refused | `test_serve_starts_when_the_allowlist_is_unset_empty_or_named['']` | cmp ok |
| H2a no total deadline | `test_body_trickled_past_the_deadline_gets_408_and_frees_the_thread` | cmp ok |
| H2b deadline default not 30 | `test_default_body_deadline_is_thirty_seconds` | cmp ok |
| H3a claude gets the full environment | `test_claude_child_gets_only_its_login_and_shell_basics` | cmp ok |
| H3b GITHUB_TOKEN allowed to claude | `test_claude_child_gets_only_its_login_and_shell_basics` | cmp ok |
| H3c CLAUDE_CONFIG_DIR dropped | `test_claude_child_gets_only_its_login_and_shell_basics` | cmp ok |
| H4 proxy keys dropped | `test_git_and_semgrep_keep_proxy_and_ca_settings` | cmp ok |
| H5a no retry | `test_postgres_write_retries_once_and_commits[deadlock]` | cmp ok |
| H5b no retry cap | `test_postgres_write_failing_twice_raises_and_writes_nothing[deadlock]` | cmp ok |
| H5c retries every error | `test_postgres_write_does_not_retry_other_errors` | cmp ok |
| H5d serialization failure not retried | `test_postgres_write_retries_once_and_commits[serialization]` | cmp ok |
| H5e enqueue bypasses the retry | `test_postgres_write_retries_once_and_commits[deadlock]` | cmp ok |
| H6a overhead ignored | `test_worst_case_docker_run_stays_inside_the_budget` | cmp ok |
| H6b rm not counted | `test_docker_overhead_is_the_sum_of_its_step_timeouts` | cmp ok |

## Not done, and why

- Live `claude -p` with the scrubbed env (H3): not allowed to run it; the manager verifies.
- The recovery run re-asks the model, so the recorded findings and event are the retry's, not
  byte-for-byte the posted review's. Storing the posted payload would fix it; out of scope.
- `post-uncertain` still completes the job (not listed in P4; a retry could only say
  `already-posted`).

## Risks for whoever merges

- Race kept from before: worker A holds the claim, loses its lease, and frees the claim after
  worker B already saw the claim taken and completed the job as `already-posted`. Nothing is
  posted for that head until the next push. Narrow window (between B's claim and A's check).
- A record failure leaves the job retrying with a full model call; if every retry fails to
  record, the job dies with the review on GitHub but not in the store (logged with the id).
- Recovery records the event the retry computed. If it differs from the posted one, a later
  dismissal either skips a real REQUEST_CHANGES (safe) or tries to dismiss a COMMENT (GitHub
  refuses; recorded as permanent).
- `review_findings` is still written and used only as the legacy fallback for dismissal.
- The `claude` child no longer sees proxy or CA variables (H3's list is exact). A worker behind
  a proxy needs them added to `CLAUDE_KEYS`.
- `--max-proof-seconds` below ~116 s now disables Docker proofs (they report the budget used up).
- D2 searches every indexed file per blocker; up to 5,000 files in memory, linear per blocker.
