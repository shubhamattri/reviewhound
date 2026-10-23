# Fix round 4: release blockers from the v0.1 readiness review

Branch `fix-round-4`, based on local `main` at 4a86c2f. Store and queue changes are covered by
`backend`-fixture tests on SQLite and on a throwaway Postgres 14.20 (`initdb` in the scratch dir,
TCP only, `-k ''`, port 55480 checked free with `nc -z`, database `rhtest`, stopped afterwards).
New tests are in `tests/test_fix_round4.py` unless named otherwise. "both" means SQLite and Postgres.

## The posting state machine (one job), extended

```
claim job -> [not a dry run] record a landed-but-unrecorded post (before any model call) -> review
  -> [dry run: stop, write nothing] -> lease check -> claim_post
       won  -> [own-PR check] -> per attempt: head check, REMOTE CHECK, lease check -> POST
       lost -> recovered here? / id on the claim? / take over a stale claim / REMOTE CHECK / busy
  -> mark_posted (own tx: id, event, blockers) -> record_review -> follow-ups (link, dismiss)
```

The remote check lists the PR's reviews and looks for one by this account (`viewer_login`; an
App gets `<slug>[bot]` from `GET /app`), on exactly this head, whose body starts with
`SUMMARY_PREFIX` ("Reviewhound ("). When the token cannot name its account, any author counts.
Adopting a review means: `mark_posted` with its id and GitHub's verdict, read its comments back,
record a `posted` row (error `adopted: ...`) where only findings GitHub shows are `posted`.

| Arrow | Failure / case | Job | Head claim (`posts`) | Store | Test |
|---|---|---|---|---|---|
| review | any exception | failed, by class | none taken | nothing | round 1 `test_worker_*` classification tests |
| job start | dry run | - | untouched | **nothing read or written** | `test_dry_run_never_touches_the_store`, `test_dry_run_leaves_an_unrecorded_post_for_the_real_run` (both) |
| job start | closed PR / head superseded / nothing to review, dry run | done | untouched | nothing | `test_dry_run_still_checks_the_pr_state_and_head_first` (2), `test_dry_run_of_a_pr_with_nothing_to_review_writes_nothing` |
| job start | an earlier attempt's post has an id but no `posted` row | continues | kept | `posted` row written before the model call | round 2 `test_record_failure_after_the_post_is_retryable_and_the_retry_recovers`, `test_recovery_happens_even_when_the_head_moved_before_the_retry` |
| lease check (before the claim) | lease lost | failed, retryable | never taken | `lease-lost` row | round 2 `test_lost_lease_takes_no_claim_at_all` |
| claim_post lost | this run recorded that post at job start | done | kept | `recovered` row; **findings `posted` only if GitHub shows their comment, else `stored`; links only from the read-back** | `test_recovery_marks_posted_only_the_comments_github_shows` (both) |
| claim_post lost, recovered | read-back fails | done | kept | `recovered`, every finding `stored`, nothing linked, error logged | `test_recovery_that_cannot_read_back_marks_nothing_posted` (both) |
| claim_post lost | claim has an id, already recorded | done | kept | `already-posted` row | round 2 `test_head_moved_and_already_posted_complete_the_job[already-posted]`, `test_recorded_post_is_not_recovered_twice` |
| claim_post lost | **claim has no id, older than one queue lease** | as for a won claim | **taken over** (its time reset; one taker wins) | - | `test_stale_claim_without_an_id_is_taken_over_and_posted`, `test_two_takers_of_one_stale_claim_only_one_wins`, `test_stale_claim_store_contract` (both) |
| takeover | review already on GitHub (crash after POST) | done | kept, with its id | adopted `posted` row | `test_stale_claim_whose_review_is_on_github_is_adopted_not_posted` (both) |
| takeover | failure before the POST | failed, by class | **freed** | nothing | `test_takeover_that_fails_before_the_post_frees_the_claim` (both) |
| claim_post lost | **claim has no id, not older than one lease; remote check finds our review** | done | the other run's, now with that id | adopted `posted` row | `test_crash_after_the_post_is_adopted_by_the_next_run_not_posted_again`, `test_fresh_claim_whose_review_is_on_github_is_adopted` (both) |
| claim_post lost | **no id, not stale, nothing on GitHub** | **failed, retryable, run_after = when the claim goes stale** (or the backoff if longer) | the other run's | `claim-busy` row, findings `stored` | `test_fresh_claim_with_nothing_on_github_is_busy_and_posts_nothing`, `test_claim_exactly_one_lease_old_is_not_stale`, `test_worker_retries_a_busy_claim_once_it_can_be_taken_over`, `test_fail_waits_the_longer_of_backoff_and_delay` (both) |
| claim_post lost | remote check raises | failed, by class | the other run's | nothing | covered by the same `_landed_review` call as below |
| own-PR check | `viewer_login` raises | failed, by class | **freed** | nothing | round 2 `test_viewer_login_failure_frees_the_claim_and_reraises` |
| head check | raises / head moved | failed by class / done | **freed** | nothing / `head-moved` | round 2 `test_head_lookup_failure_...`, `test_head_moving_after_the_claim_...` |
| **remote check** (every attempt) | list reviews raises | failed, by class | **freed** | nothing | `test_remote_check_failure_frees_the_claim_posts_nothing_and_reraises` (both) |
| **remote check** | our review of this head is on GitHub | done | **kept**, with its id | adopted `posted` row, GitHub's verdict | `test_review_already_on_github_is_adopted_before_the_first_post`, `test_review_landing_during_the_body_retry_is_adopted_not_posted_again`, `test_adopted_review_without_inline_comments_marks_nothing_posted` (both) |
| **remote check** | another author, another head, or a body without our prefix | continues to POST | - | - | `test_only_this_accounts_review_of_this_head_with_our_summary_counts` (4 cases, both), `test_an_account_the_token_cannot_name_matches_any_author` |
| lease check | lost (read after the head and remote checks) | failed, retryable | **freed** | `lease-lost` row | round 2 `test_lease_lost_after_the_claim_...`, `test_lease_lost_during_the_head_lookup_...`; order: `test_remote_check_runs_before_every_post_and_the_lease_is_read_after_it` |
| POST | 400/422 with inline comments, attempt 1 | - | freed for the moment | loops to the checks (remote check included) with comments in the body | round 1 `test_rejected_anchor_is_retried_once_with_comments_in_the_body` |
| POST | 400/422 on the retry, or no comments | dead, `post-rejected: <reason>` | **freed** | `post-rejected` | round 2 `test_post_rejected_after_the_body_retry_is_a_dead_job_with_its_reason` |
| POST | 401/403/408/**429** | requeued (`PostRefused`) | **freed** | nothing | `test_429_on_the_post_frees_the_claim_and_raises_retryable`, `test_429_on_the_body_retry_frees_the_claim_and_raises`, `test_worker_requeues_a_429_and_the_retry_posts` (both); round 2 `test_worker_requeues_auth_and_timeout_refusals_and_kills_the_rest` |
| POST | 404 / other 4xx | dead (`PostRefused`) | **freed** | nothing | round 2 `test_other_refusals_raise_free_the_claim_and_never_retry_in_the_body` |
| POST | 5xx, transport, other exception | done | kept (may have landed) | `post-uncertain`, posted=1 | round 1 `test_only_a_refusal_is_retried` (502, timeout), `test_retry_that_times_out_is_uncertain_and_keeps_the_claim` |
| POST | KeyboardInterrupt / SystemExit | propagates | kept | nothing (next run adopts via the remote check) | round 2 `test_interrupt_during_the_post_keeps_the_claim`; `test_crash_after_the_post_is_adopted_...` |
| POST | landed | done | kept, with id | `posted`, every finding `posted` | `test_a_review_this_run_posted_itself_marks_its_findings_posted` (both) |
| mark_posted | DB error | continues, logged | kept, no id | the review row carries the id | round 2 `test_mark_failure_is_logged_and_the_review_still_records` |
| record_review | DB error after a landed post | requeued (`RecordAfterPostError`) | kept with id | retry recovers at job start | round 2 `test_worker_retries_a_record_failure_and_completes_without_posting_twice` |
| follow-ups | link or dismissal error | done | kept | logged; dismissal failure recorded | round 1/2 dismissal tests |

## Items -> fix -> test -> result

| # | Fix | Tests | Result |
|---|---|---|---|
| N1 | `VCS.list_reviews` (GitHub: paginated `GET /pulls/{n}/reviews`; memory VCS). `Reviewer._landed_review` before every POST (in `_blocked`, between the head and lease checks) and when the claim is held by another run. `_adopt`: `mark_posted` (GitHub's verdict mapped to an event), then a `posted` record. Store: `post_claim` (id, age) and `take_stale_claim` (one conditional `UPDATE`, so one taker wins). `Reviewer(stale_claim_s=)` defaults to the queue lease (1800 s); `cmd_worker` passes `queue.lease_s`. A busy claim is a new outcome `claim-busy`: the worker fails it retryable with `queue.fail(..., delay_s=)` = time until the claim is stale. App mode: `GitHubApp.bot_login()` (`GET /app` slug, cached) is the VCS login. `SUMMARY_PREFIX` lives in `poster.py`. | the N1 rows of the table above, plus `test_list_reviews_reads_every_page`, `test_list_reviews_tolerates_a_deleted_user_and_an_empty_body`, `test_a_login_source_names_the_account_and_its_errors_raise`, `test_app_bot_login_is_its_slug_looked_up_once`, `test_app_bot_login_refuses_a_malformed_answer` (4), `test_worker_factories_use_the_given_stale_threshold_and_the_app_login`, `test_worker_command_passes_its_queue_lease_as_the_stale_threshold` | pass, both |
| N1b | `_refused` now includes 429, so the claim is freed and `PostRefused(429)` raised; worker `POST_RETRYABLE_HTTP` adds 429. | `test_429_on_the_post_*`, `test_429_on_the_body_retry_*`, `test_worker_requeues_a_429_and_the_retry_posts` | pass, both |
| N2 | `_read_back` reads the landed review's comments (adopted or recovered). `review()` splits `decision.post` by `(file, side, line)` in the read-back: shown -> `posted`, the rest passed as new `stored` argument of `record_review` (both backends, via `upsert_order`). `link_comments` uses the read-back when present. A failed read-back stores everything and links nothing (logged). | `test_recovery_marks_posted_only_the_comments_github_shows`, `test_recovery_that_cannot_read_back_marks_nothing_posted`, `test_adopted_review_without_inline_comments_marks_nothing_posted`, `test_a_review_this_run_posted_itself_marks_its_findings_posted` | pass, both |
| N3 | Diff parser records each deleted run (`Hunk.runs`, `FileDiff.run_text(at=, base_line=)`). The checker sets `Finding.deleted_text` for LEFT findings (their run) and beside-deletion findings (the adjacent run). `Blocker.deleted_text` -> new nullable column `review_blockers.deleted_text` (SQLite `_migrate`, Postgres `ADD COLUMN IF NOT EXISTS`). `open_reason`: a row with deleted text is gone only when that text (via `find_quote`, whitespace-normalised) is back in its own file at head; too short to search, or file missing, keeps the verdict. Ordinary blockers keep the quote rule. | `test_runs_are_split_by_added_lines_and_keep_their_blank_lines`, `test_left_and_beside_findings_carry_the_deleted_run_and_others_do_not`, `test_blocker_deleted_text_is_stored_and_read_back`, `test_old_sqlite_review_blockers_table_gets_the_deleted_text_column`, `test_old_postgres_review_blockers_table_gets_the_deleted_text_column`, `test_removed_guard_blocker_is_gone_only_once_the_deleted_code_is_back` (10), `test_restoring_with_other_whitespace_counts_as_back`, `test_deleted_text_too_short_to_look_for_keeps_the_verdict`, `test_ordinary_blocker_keeps_the_quote_rule`, `test_removed_guard_verdict_is_dismissed_when_the_guard_comes_back` (beside and LEFT), `test_removed_guard_verdict_is_kept_while_the_guard_is_still_missing` | pass, both |
| N4 | A beside-deletion finding is verified only if one claim is `true`; otherwise `verified=False` and the note `UNCONFIRMED_BESIDE`, so the default policy holds it as `unverified`. A `quote` claim on such a finding that is false at head is true when it matches the adjacent deleted run (same file, at least 8 characters). | `test_beside_with_a_true_claim_is_verified`, `test_beside_with_a_false_claim_is_dropped`, `test_beside_with_no_claim_is_unverified_and_the_default_policy_holds_it`, `test_beside_with_only_an_unknown_claim_is_unverified`, `test_beside_with_a_quote_of_the_deleted_guard_is_verified`, `test_beside_with_a_quote_that_is_not_the_deleted_code_is_dropped` (3), `test_quote_of_deleted_code_does_not_hold_for_a_finding_on_an_added_line`, `test_left_finding_quote_claim_of_its_deleted_line_is_still_checked_at_head`; round 3 checker tests updated | pass |
| N5 | `review(dry_run=True)` skips `_record_landed` and `known_state`, and `_finish` never records a dry run whatever its outcome (a closed or superseded PR in a dry run used to write a row: found by the new test). The closed and expected-head checks still run first. | `test_dry_run_never_touches_the_store`, `test_dry_run_still_checks_the_pr_state_and_head_first` (2), `test_dry_run_of_a_pr_with_nothing_to_review_writes_nothing`, `test_dry_run_leaves_an_unrecorded_post_for_the_real_run` | pass |
| Docs | README: accuracy section (replay vs live, `LIVE-EVAL-RESULT` placeholder), alternative anchors (cases 24, 29, 32, each matched once), posting table rows for adopted / claim-busy / takeover / 429, open-risks section. | n/a | written |

Changed earlier tests (the contract changed on purpose):
- round 2 `test_claim_without_a_post_id_stays_already_posted`: removed (that arrow is now claim-busy / adopt / takeover, covered above).
- round 2 `test_head_moved_and_already_posted_complete_the_job[already-posted]`: the other run now has an id and a record.
- round 2 `test_failure_before_the_post_requeues_the_job_and_the_retry_posts`: `viewer_login` is now also asked by the remote check.
- round 1 `test_only_a_refusal_is_retried`: 429 removed from the uncertain cases.
- `test_pipeline::test_second_worker_on_same_head_does_not_post_again`: now `claim-busy`.
- round 3 beside-deletion checker tests: the live answers carry their `not_calls` claim; no-claim cases now carry the unverified note.
- blocker dict assertions gained `deleted_text`.

Eval cases 35 and 36 already carry a `not_calls` claim that is true, so the recorded eval is unchanged.

## Final runs

MUTATION_AND_RUNS

## Not done, and why

- Live eval not run (not allowed here). The manager re-runs it to see whether the real model gives
  cases 29 and 32 a confirming claim; N4 is not loosened.
- `post-uncertain` (5xx, timeout) still completes the job with its findings marked posted. Chasing
  it (retry, then remote check) would mark findings `stored` and change a deliberate round-1
  contract; listed as a risk instead. A later run on that head does check GitHub first.
- The quote-on-deleted-lines rule applies only to beside-deletion findings; LEFT findings keep
  head-only quote claims (pinned by a test).

## Risks for whoever merges

REVIEW_AND_RISKS
