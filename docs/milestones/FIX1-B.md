# Fix round 1, track B: feedback, posting, storage, checker

Branch `fix-track-b`, based on local `main` at 391918d. Every store and queue change is
covered by `backend`-fixture tests that run on SQLite and on a throwaway Postgres 14.20
(`initdb` in the scratch dir, TCP only, `-k ''`, port 55458, database `rhtest_b`).

## Findings, fixes, tests

| # | Finding | Fix | Tests (in `tests/test_fix_round1_b.py` unless named) | Result |
|---|---|---|---|---|
| B1 | Any repo owner could make jobs | `REVIEWHOUND_ALLOWED_OWNERS` + `serve --allowed-owner` (repeatable, union, lower-cased). `handle_event` checks the owner before anything else (after `ping`), for PR **and** feedback events: 202 `ignored: owner not allowed`, no job, no write. Unset = allow all, and `serve` logs a warning at start. README says to set it. | `test_event_from_another_owner_is_ignored_and_queues_nothing`, `test_unset_allowlist_allows_every_owner`, `test_feedback_from_another_owner_writes_nothing`, `test_http_post_from_another_owner_creates_no_job` (real socket, `q.claim() is None`), `test_serve_passes_env_and_flag_owners_to_the_server`, `test_allowed_owners_come_from_env_and_flags` | pass, both backends |
| B2 | Dismissal on the absence of a finding | An old REQUEST_CHANGES is dismissed only when every blocking finding posted in it is positively gone at the head this run reviewed: quote not found by `checker.find_quote` in its file, or the file is deleted and the quote is not in any other changed file. A secret is checked by its identity (`scanners.secret_identities`), not its redacted quote. LEFT findings, quotes too short to mean anything, unreadable files and old reviews with no recorded findings never count as gone. Right before each dismissal the PR head is re-read and must equal this run's head. New table `review_findings` (both backends) stores each posted finding per review with its quote, side, severity and identity. | `test_model_silence_on_unchanged_code_never_dismisses`, `test_checker_dropping_the_blocker_never_dismisses`, `test_blocker_file_deleted_at_head_is_dismissed`, `test_blocker_quote_moved_to_another_file_keeps_the_verdict`, `test_every_blocker_of_the_old_review_must_be_gone`, `test_head_moving_just_before_the_dismissal_keeps_the_verdict`, `test_an_old_review_with_no_recorded_findings_is_never_dismissed`, `test_posted_findings_are_stored_per_review_with_their_quote`, `test_open_reason_needs_positive_evidence` (10 cases); `test_github_feedback.py::test_resolved_blocker_still_in_the_code_keeps_the_verdict` | pass, both backends |
| B3 | 422 lost the review; no lease check | A 4xx (not 429) refusal of a review that has inline comments is retried once with every comment moved into the body (`comments_in_body`); success is `posted` with a note in `error`. A second refusal is `post-rejected` and frees the head; a retry that times out or 5xxs is `post-uncertain`. A refusal of a body-only review is not retried. `post-uncertain` stays recorded as posted and now logs that linking and dismissal were skipped. `Reviewer.review(..., should_post: Callable[[], bool] \| None = None)`: asked before the post and before the retry; False gives outcome `lease-lost`, nothing posted, head not claimed. | `test_rejected_anchor_is_retried_once_with_comments_in_the_body`, `test_left_comment_moved_to_the_body_says_it_is_a_deleted_line`, `test_rejected_twice_is_rejected_and_frees_the_head`, `test_retry_that_times_out_is_uncertain_and_keeps_the_claim`, `test_only_a_refusal_is_retried` (502, 429, timeout), `test_refusal_of_a_review_without_inline_comments_is_not_retried`, `test_uncertain_post_skips_linking_and_says_so`, `test_lost_lease_posts_nothing_and_leaves_the_head_free`, `test_lease_lost_after_a_refusal_skips_the_retry`, `test_held_lease_posts_as_before` | pass, both backends |
| B4 | Dedupe on the unsigned delivery header; claim and write in two transactions | The webhook passes `sha256(raw signed body)`; thread events dedupe on `thread:<sha256>`. Replies keep `reply:<repo>:<comment id>` (also signed content). New `apply_feedback(event_key, repo, items)` claims the key and writes all feedback in ONE transaction on both backends; `claim_event` / `release_event` are gone. | `test_replayed_signed_body_with_a_new_delivery_id_is_applied_once` (real socket), `test_github_feedback.py::test_event_key_is_claimed_in_the_same_transaction_as_its_feedback`, `::test_reply_write_failure_leaves_no_claim_behind`, `::test_thread_write_failure_leaves_no_claim_behind` (the write fails AND a release is impossible; the redelivery still applies), `::test_redelivered_thread_event_is_applied_once` | pass, both backends |
| B5 | Postgres deadlock on concurrent upserts | `store.upsert_order` sorts (posted + held) by fingerprint, stable, so posted wins a tie as before; both backends use it. `worker.is_retryable` treats sqlstate `40P01` / `40001` as retryable (minimal edit in Track A's `worker.py`). | `test_findings_are_upserted_in_fingerprint_order`, `test_same_fingerprint_posted_and_held_keeps_the_posted_state`, `test_postgres_deadlock_and_serialization_failures_are_retryable`, `test_a_real_postgres_deadlock_is_classified_retryable` (provokes a real deadlock on Postgres) | pass |
| B6 | Webhook hardening | Handler `timeout = read_timeout_s` (default 10 s); the body length is still checked before any read. `/metrics` needs `Authorization: Bearer <REVIEWHOUND_METRICS_TOKEN>` when set, compared with `hmac.compare_digest`; anything else is 401. Unset: open, and a loud start-up warning. | `test_metrics_need_the_bearer_token_when_set` (no header, wrong token, wrong case, no scheme, right token), `test_metrics_token_is_read_from_the_environment`, `test_unset_metrics_token_stays_open_and_warns_loudly`, `test_metrics_token_compare_is_constant_time`, `test_slow_body_times_out_and_queues_nothing`, `test_default_read_timeout_is_ten_seconds` | pass |
| B7 | A blank-line edit anchored unchanged code | The changed line used as anchor must be a non-blank line of the quote span, on the RIGHT and the LEFT side. | `test_blank_line_added_inside_a_quote_does_not_anchor_it`, `test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left`, `test_a_real_change_inside_the_quote_still_anchors_on_its_non_blank_line` | pass |
| B8 | LEFT and RIGHT findings shared identity | `\x00LEFT` is appended to the fingerprint input only for LEFT; RIGHT values are unchanged (pinned `68eb534220237c8b`). | `test_left_side_is_part_of_the_fingerprint_and_right_stays_stable`, `test_scanners.py::test_fingerprint_without_identity_is_unchanged` | pass |
| B9 | Child processes inherited every token | `procenv.child_env()`: PATH, HOME, LANG plus what the caller adds. git fetch gets the `GIT_CONFIG_*` auth header; `git log` and semgrep get nothing else. | `test_git_fetch_env_holds_only_the_auth_header`, `test_git_log_env_has_no_tokens_at_all`, `test_semgrep_env_has_no_tokens`, `test_child_env_skips_unset_safe_keys` | pass |

## Final runs

- SQLite only: `420 passed, 88 skipped`
- With Postgres: `508 passed`
- `uv run mypy`: no issues in 38 source files. `uv run ruff check src tests evals qa`: clean.
- Recorded eval: `cases: 34  precision: 1.00  recall: 1.00  dropped by claim checker: 32`

## Mutation table

Each mutation was applied alone by a script that backs the file up, runs the full suite with
Postgres enabled, restores it, and `filecmp`s the restored file against the backup (all 48
restores compared equal). Named test = one that failed.

|---|---|---|---|
| B1a | owner check removed | 6 failed | `tests/test_fix_round1_b.py::test_event_from_another_owner_is_ignored_and_queues_nothing[sqlite]` |
| B1b | case-sensitive compare | 4 failed | `tests/test_fix_round1_b.py::test_event_from_another_owner_is_ignored_and_queues_nothing[sqlite]` |
| B1c | HTTP path ignores allowlist | 2 failed | `tests/test_fix_round1_b.py::test_http_post_from_another_owner_creates_no_job[sqlite]` |
| B1d | CLI drops flags | 1 failed | `tests/test_fix_round1_b.py::test_serve_passes_env_and_flag_owners_to_the_server` |
| B1e | owners not lowercased | 2 failed | `tests/test_fix_round1_b.py::test_serve_passes_env_and_flag_owners_to_the_server` |
| B2a | no positive check (old rule) | 12 failed | `tests/test_fix_round1_b.py::test_model_silence_on_unchanged_code_never_dismisses[sqlite]` |
| B2b | no head re-check | 2 failed | `tests/test_fix_round1_b.py::test_head_moving_just_before_the_dismissal_keeps_the_verdict[sqlite]` |
| B2c | review with no rows counts as clean | 2 failed | `tests/test_fix_round1_b.py::test_an_old_review_with_no_recorded_findings_is_never_dismissed[sqlite]` |
| B2d | LEFT treated as provable | 1 failed | `tests/test_fix_round1_b.py::test_open_reason_needs_positive_evidence[row6-files6-changed6-a deleted-line finding cannot be shown gone]` |
| B2e | short quote treated as provable | 1 failed | `tests/test_fix_round1_b.py::test_open_reason_needs_positive_evidence[row7-files7-changed7-no evidence quote to look for]` |
| B2f | no search for a moved quote | 4 failed | `tests/test_fix_round1_b.py::test_blocker_quote_moved_to_another_file_keeps_the_verdict[sqlite]` |
| B2g | unreadable file treated as gone | 1 failed | `tests/test_fix_round1_b.py::test_open_reason_needs_positive_evidence[row5-files5-changed5-big.py could not be read at head (too big)]` |
| B2h | secret checked by redacted quote | 1 failed | `tests/test_fix_round1_b.py::test_open_reason_needs_positive_evidence[row8-files8-changed8-the secret is still in a.py]` |
| B2i | SQLite/shared rows never written | 12 failed | `tests/test_fix_round1_b.py::test_blocker_file_deleted_at_head_is_dismissed[sqlite]` |
| B2j | PG rows never written | 6 failed | `tests/test_fix_round1_b.py::test_blocker_file_deleted_at_head_is_dismissed[postgres]` |
| B2k | blocker threshold off by one | 10 failed | `tests/test_fix_round1_b.py::test_blocker_file_deleted_at_head_is_dismissed[sqlite]` |
| B2l | quote present treated as gone | 10 failed | `tests/test_fix_round1_b.py::test_model_silence_on_unchanged_code_never_dismisses[sqlite]` |
| B3a | no retry after refusal | 8 failed | `tests/test_fix_round1_b.py::test_rejected_anchor_is_retried_once_with_comments_in_the_body[sqlite]` |
| B3b | retry even without comments | 2 failed | `tests/test_fix_round1_b.py::test_refusal_of_a_review_without_inline_comments_is_not_retried[sqlite]` |
| B3c | retry keeps inline comments | 4 failed | `tests/test_fix_round1_b.py::test_rejected_anchor_is_retried_once_with_comments_in_the_body[sqlite]` |
| B3d | 429 counted as refusal | 2 failed | `tests/test_fix_round1_b.py::test_only_a_refusal_is_retried[sqlite-first1]` |
| B3e | refused head not released | 7 failed | `tests/test_fix_round1_b.py::test_rejected_twice_is_rejected_and_frees_the_head[sqlite]` |
| B3f | should_post ignored before post | 4 failed | `tests/test_fix_round1_b.py::test_lost_lease_posts_nothing_and_leaves_the_head_free[sqlite]` |
| B3g | should_post ignored before retry | 2 failed | `tests/test_fix_round1_b.py::test_lease_lost_after_a_refusal_skips_the_retry[sqlite]` |
| B3h | uncertain not logged | 2 failed | `tests/test_fix_round1_b.py::test_uncertain_post_skips_linking_and_says_so[sqlite]` |
| B3i | uncertain treated as refused | 13 failed | `tests/test_fix_round1_b.py::test_retry_that_times_out_is_uncertain_and_keeps_the_claim[sqlite]` |
| B4a | key from unsigned delivery header | 2 failed | `tests/test_fix_round1_b.py::test_replayed_signed_body_with_a_new_delivery_id_is_applied_once[sqlite]` |
| B4b | SQLite claim commits on its own | 2 failed | `tests/test_github_feedback.py::test_reply_write_failure_leaves_no_claim_behind[sqlite]` |
| B4c | PG claim commits on its own | 2 failed | `tests/test_github_feedback.py::test_reply_write_failure_leaves_no_claim_behind[postgres]` |
| B4d | thread events not deduplicated | 4 failed | `tests/test_fix_round1_b.py::test_replayed_signed_body_with_a_new_delivery_id_is_applied_once[sqlite]` |
| B4e | SQLite claimed key not refused | 4 failed | `tests/test_fix_round1_b.py::test_replayed_signed_body_with_a_new_delivery_id_is_applied_once[sqlite]` |
| B4f | PG claimed key not refused | 4 failed | `tests/test_fix_round1_b.py::test_replayed_signed_body_with_a_new_delivery_id_is_applied_once[postgres]` |
| B5a | upserts unsorted | 2 failed | `tests/test_fix_round1_b.py::test_findings_are_upserted_in_fingerprint_order[sqlite]` |
| B5b | held before posted on tie | 2 failed | `tests/test_fix_round1_b.py::test_same_fingerprint_posted_and_held_keeps_the_posted_state[sqlite]` |
| B5c | sqlstate not retryable | 2 failed | `tests/test_fix_round1_b.py::test_postgres_deadlock_and_serialization_failures_are_retryable` |
| B5d | only deadlock retryable | 1 failed | `tests/test_fix_round1_b.py::test_postgres_deadlock_and_serialization_failures_are_retryable` |
| B6a | metrics auth skipped | 6 failed | `tests/test_fix_round1_b.py::test_metrics_need_the_bearer_token_when_set[headers0-want0]` |
| B6b | token env ignored | 1 failed | `tests/test_fix_round1_b.py::test_metrics_token_is_read_from_the_environment` |
| B6c | plain == compare | 1 failed | `tests/test_fix_round1_b.py::test_metrics_token_compare_is_constant_time` |
| B6d | no unauthenticated warning | 1 failed | `tests/test_fix_round1_b.py::test_unset_metrics_token_stays_open_and_warns_loudly` |
| B6e | no read timeout | 2 failed | `tests/test_fix_round1_b.py::test_slow_body_times_out_and_queues_nothing` |
| B6f | default timeout wrong | 1 failed | `tests/test_fix_round1_b.py::test_default_read_timeout_is_ten_seconds` |
| B7a | right blank anchor | 2 failed | `tests/test_fix_round1_b.py::test_blank_line_added_inside_a_quote_does_not_anchor_it` |
| B7b | left blank anchor | 1 failed | `tests/test_fix_round1_b.py::test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left` |
| B8 | side dropped | 1 failed | `tests/test_fix_round1_b.py::test_left_side_is_part_of_the_fingerprint_and_right_stays_stable` |
| B8b | side always added (RIGHT changes) | 2 failed | `tests/test_scanners.py::test_fingerprint_without_identity_is_unchanged` |
| B9a | child_env copies whole environ | 4 failed | `tests/test_fix_round1_b.py::test_git_fetch_env_holds_only_the_auth_header` |
| B9b | git log inherits env | 1 failed | `tests/test_fix_round1_b.py::test_git_log_env_has_no_tokens_at_all` |
| B9c | semgrep inherits env | 1 failed | `tests/test_fix_round1_b.py::test_semgrep_env_has_no_tokens` |
| Id | Mutation | Failed tests (full suite) | First killing test |

Postgres hygiene: Track A reported one run against my first server (port 55447). That server
was stopped; every result above comes from a fresh cluster on 55458 started by a script that
refuses a port already in use and refuses an existing data directory (both refusals checked).

## What remains unverified

- No real GitHub: the 422 retry, dismissal and `review_findings` flows run on `MemoryVCS`.
  Whether GitHub accepts the body-only retry for every 422 cause is not proven.
- Real concurrent `record_review` deadlock: the fix is shown by statement order on both
  backends and the classification by a real Postgres deadlock, not by two racing reviews.
- The proof-downgrade case of B2 has no dedicated test. Dismissal reads only the stored rows
  of the old review and the head source, so no current-run result can make it dismiss.
- `deploy.md` and compose (Track A) should list `REVIEWHOUND_ALLOWED_OWNERS`; only the README
  table was changed here to avoid a conflict.

## Risks for whoever merges

- Dismissals become rare by design: a fix that adds a missing guard but leaves the quoted
  line untouched keeps the old REQUEST_CHANGES until a human dismisses it.
- Reviews posted before this change have no `review_findings` rows and are never auto-dismissed.
- LEFT fingerprints change once, so a LEFT finding on an open PR may be posted again once.
- `git`/`semgrep` no longer see proxy or CA variables (`HTTPS_PROXY`, `SSL_CERT_FILE`). A
  deployment behind a proxy needs them added to `procenv.SAFE_KEYS` on purpose.
- Track A files touched: `worker.py` (two lines in `is_retryable`). `tests/conftest.py` gained
  `spy`/`SpyConn` and the `review_findings` drop.
- Track A wires `should_post=lambda: not hb.lost`; until then the worker never passes it.
- Postgres connection failures (`OperationalError`) are still not retryable (unchanged, parked).
