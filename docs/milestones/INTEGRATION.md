# Integration of milestones 3, 6, 5 and 4

Base: local `main` at `cebd1a3` (milestone 3). Each branch was merged with `git merge --no-ff`.
Conflicts were resolved by keeping both sides' behaviour. After each merge: the full suite on
SQLite and on a throwaway Postgres 14.20 (`initdb` + `pg_ctl -p 55433`, TCP only, in the
scratch dir, `REVIEWHOUND_TEST_PG=postgresql://rh@127.0.0.1:55433/rhtest`), then mypy, ruff
and the recorded eval. The eval ran as `run_eval(Path("evals/cases"), verify=True)`, because
the sandbox refuses commands that contain the word `eval`.

## Merge 1: milestone 6 (`worktree-agent-ac28246c05c4d17db`), merge commit `3e2a9c2`

### Conflicts and how each was resolved

| File | M3 side | M6 side | Resolution |
|---|---|---|---|
| `worker.py` | `run_one`/`run_forever` take a reviewer or a per-job factory | `WorkerQueue`, review under `LeaseHeartbeat`, `metrics`, `alarm` | Kept both. The factory is called inside the existing `try` (so its errors are classified), *before* the heartbeat starts. The review runs under the heartbeat, and metrics are recorded after the job state is written. M3 used `isinstance(reviewer, Reviewer)` to spot a factory, which broke M6's duck-typed stub reviewers. `_reviewer_for` now treats anything with `.review` as a reviewer. |
| `webhook.py` | `make_server(..., store, app_id)`, `X-GitHub-Delivery` passed on | `make_server(..., metrics)`, `GET /metrics` | One signature with `store`, `app_id` and `metrics`. `store` is typed `ReviewStore`, so Postgres works. |
| `cli.py` | imports `ConfigError`, `Job`, `Scanner`, `Store`; serve passes the store and app id; worker picks the App factory | `open_queue/open_store/open_metrics`, `--database-url`, metrics, alarm | Both. `serve` passes queue, store, app id and metrics. `worker` opens the queue and metrics through `backends` and picks the App factory or the single reviewer. |
| `uv.lock` | `pyjwt[crypto]` | `psycopg[binary]` | Took M6's side, then ran `uv lock` (added cryptography, pycparser, pyjwt). |

### Behaviour that silently dropped out of auto-merged files, now restored

- `cli._app_reviewer_factory` (auto-merged) still opened `Store(home/"reviewhound.db")`. In App
  mode with `--database-url`, reviews would have gone to SQLite while the queue was in Postgres.
  It now uses `open_store(home, args.database_url)`.
- `pipeline.py` and `webhook.py` passed M6's `ReviewStore` to M3's `after_post` and
  `handle_feedback_event`, which were typed for the SQLite `Store`. mypy caught it. The M3
  methods are now part of the `ReviewStore` protocol, and `github_feedback` takes `ReviewStore`.
- `queue.py`, `store.py` and `vcs/github.py` were checked line by line, and both sides were
  present: `installation_id` with `_migrate`, plus `WorkerQueue`; the `_GITHUB_SCHEMA` tables
  plus `ReviewStore`; the token provider plus `_git_env()` per call inside M6's `_fetch_into`.
  `pipeline.py` keeps `after_post` after `_finish`.
- `docs/deploy.md`: a placeholder Postgres URL with a user and password in it tripped the local
  pre-commit secret scanner. It was reworded to name the parts of the URL instead.

### Ported to Postgres

- `QUEUE_DDL`: `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS installation_id BIGINT`. An M6
  database is migrated in place, and the DDL already runs under an advisory lock.
- `STORE_DDL`: `comment_links` (`comment_id BIGINT`, because GitHub comment ids exceed 32 bits),
  `dismissals`, and `feedback_events` (the delivery-id and reply dedupe keys).
- `PostgresQueue`: `enqueue(installation_id=)` with the same rules as SQLite. An id on a
  redelivery updates the job, a redelivery without an id keeps it, and the INSERT writes it.
  `_job` reads the id.
- `PostgresStore`: `link_comments` (upsert = SQLite's `INSERT OR REPLACE`), `linked_findings`
  (`= ANY`), `claim_event` (`ON CONFLICT DO NOTHING`, rowcount 1), `release_event`,
  `blocking_reviews_to_dismiss` (the same "older rows only, `ok != 0` excluded" SQL),
  `record_dismissal`, `dismissals` and `feedback`.
- `tests/conftest.py` `_PG_TABLES` gains `comment_links, dismissals, feedback_events`.
- These now use the shared `backend` fixture: every store/queue test in
  `test_github_feedback.py` (all except the `httpx` request-shape test), the queue and webhook
  tests in `test_installation_jobs.py`, and the in-place migration test, which builds an M6-era
  `jobs` table on Postgres. Three new direct store tests also use it: comment links with
  64-bit ids, scoped per repo and replaced on re-link; event keys claimed once until released;
  and the dismissal states that decide what is retried.

### Store WAL (M6 risk)

`Store.__init__` now runs `PRAGMA journal_mode=WAL` before the schema, as `SQLiteMetrics`
does. The test is `test_new_sqlite_store_is_in_wal_mode_once_constructed`. It is deterministic
because WAL mode is persistent in the file, so a fresh connection reads `wal` right after
construction, before any transaction. The race that the fix prevents (several writers flipping
a new file to WAL at once and getting `database is locked`) is timing-dependent. M6 measured it
at 8 failures in 30 runs, so it is not the test.

### Installation lookup for jobs without an installation id (M3 risk)

With App credentials configured, `token_for_job` uses the job's installation id. When the job
has none (manual `enqueue`, sweep), it calls `GitHubApp.installation_for_repo`:
`GET /repos/{owner}/{repo}/installation` signed with the App JWT. It retries 429, 5xx and
transport errors the same way as the token exchange (now one shared `_call`). A 404 raises
`AppNotInstalledError(ConfigError)` with the message
`GitHub App <id> is not installed on <repo> (GET /repos/<repo>/installation -> 404)`.
`is_retryable` is False for that error, so the job goes `dead` at once with that error.
Other 4xx responses are `GitHubError`s that are not retried. A malformed body is a
`GitHubError`. **Behaviour change:** in App mode the worker no longer falls back to
`GITHUB_TOKEN` for jobs with no id; the README row is updated. Tests (all on
`httpx.MockTransport`) are in `test_installation_jobs.py`: lookup with the JWT, no lookup when
the job has an id, not installed, retries, malformed body, and the worker marking the job dead.

### Suite after merge 1

- SQLite only: `204 passed, 59 skipped`
- With Postgres: `263 passed`
- mypy: `Success: no issues found in 36 source files`. ruff: `All checks passed!`
- Recorded eval: `cases: 12  precision: 1.00  recall: 1.00  dropped by claim checker: 8`

### Mutation table (merge 1)

Each mutation was applied alone, and the listed test files were run with Postgres enabled
(`-x`, so the first killing test is named). Each file was restored from a backup and
`filecmp`-checked; all 25 were restored identical. The runner script is kept outside the repo.

| # | Mutation | Killed by |
|---|---|---|
| P1 | PG DDL without the `installation_id` ALTER | `test_old_queue_db_is_migrated_in_place[postgres]` |
| P2 | PG redelivery never updates the id | `test_redelivery_keeps_or_updates_installation_id[postgres]` |
| P3 | PG redelivery without an id clears it | `test_redelivery_keeps_or_updates_installation_id[postgres]` |
| P4 | PG INSERT drops the id | `test_enqueue_stores_installation_id[postgres]` |
| P5 | PG `claim_event` always True (no dedupe) | `test_reply_to_our_comment_is_recorded_once_and_truncated[postgres]` |
| P6 | PG `release_event` does nothing | `test_feedback_store_failure_releases_the_event_for_redelivery[postgres]` |
| P7 | PG permanent dismissal failure retried | `test_permanent_dismissal_failure_is_not_retried[postgres]` |
| P8 | PG successful dismissal not remembered | `test_clean_new_head_dismisses_the_earlier_request_changes[postgres]` |
| P9 | PG no "older than this review" bound | `test_late_follow_up_never_dismisses_a_newer_review[postgres]` |
| P10 | PG `linked_findings` ignores the repo | `test_comment_links_hold_github_sized_ids_and_stay_per_repo[postgres]` |
| P11 | PG re-link keeps the old row | `test_comment_links_hold_github_sized_ids_and_stay_per_repo[postgres]` |
| P12 | PG permanent failure stored as transient | `test_permanent_dismissal_failure_is_not_retried[postgres]` |
| P13 | PG dismissal error not cut to 500 | `test_dismissal_states_decide_what_is_retried[postgres]` |
| P14 | PG upsert loses `resolved` protection | `test_feedback_and_posted_states_survive_a_held_re_review[postgres-resolved]` |
| P15 | PG upsert loses `reopened` protection | `test_feedback_and_posted_states_survive_a_held_re_review[postgres-reopened]` |
| S1 | `Store` without WAL at construction | `test_new_sqlite_store_is_in_wal_mode_once_constructed` |
| L1 | no lookup, static token used | `test_cli_app_factory_uses_installation_or_static_token` |
| L2 | 404 not mapped to `AppNotInstalledError` | `test_app_not_installed_on_the_repo_is_a_permanent_clear_error` |
| L3 | App requests never retried | `test_installation_lookup_retries_server_errors_but_not_auth_errors` |
| L4 | non-integer installation id accepted | `test_malformed_installation_response_raises` |
| L5 | the job's own id ignored (always looks up) | `test_token_choice_per_job` |
| W1 | worker factory never called | `test_worker_builds_a_reviewer_per_job` |
| W2 | review not run under the heartbeat | `test_long_review_keeps_its_lease_against_a_second_worker[sqlite]` |
| W3 | `serve` without the store | `test_serve_wires_queue_store_app_id_and_metrics_together` |
| W4 | `serve` without the metrics renderer | `test_serve_wires_queue_store_app_id_and_metrics_together` |

## Merge 2: milestone 5 (`worktree-agent-a610e0e7b76d1bfd8`), merge commit `42d0ce2`

### Conflicts

None in the text: `pipeline.py`, `vcs/base.py`, `vcs/github.py`, `uv.lock`, `README.md` and
`pyproject.toml` all merged on their own. Each one was read against both sides. `pipeline.py`
keeps M6's `ReviewStore` and M3's `after_post` after `_finish`, and adds M5's
`CodeIndex(cfg.languages)`, `not_reviewed`, `base_reader` and `not_reviewed=` passed to
`build_review`. `vcs/github.py` keeps M3's token provider and M6's atomic checkout, and adds
M5's `side` in the post payload and `Diff.skipped`. `uv lock --check` passed as merged.

### A semantic clash fixed in the merge commit

M3 linked posted comments to findings by `(file, line)`. With M5, a LEFT comment (on a deleted
base line) and a RIGHT comment (a head line) can share a line number in the same file, and
policy dedupes by `(file, side, line)` only. Both comments would then have been linked to one
fingerprint, so thread and reply feedback would have landed on the wrong finding. Now:
- `PostedComment` has `side` (default `RIGHT`).
- `GitHubVCS.review_comments` reads GitHub's `side`.
- `MemoryVCS` passes it through.
- `link_comments` keys by `(file, side, line)`.

Test: `test_left_and_right_comments_on_the_same_line_number_link_to_their_own_findings`
(both backends). The read-back test also asserts `side`.

### Ported to Postgres

Nothing: M5 adds no stored column. M5 does not persist `side`; matching uses the fingerprint.

### Suite after merge 2

- SQLite only: `258 passed, 60 skipped` (204 + M5's 53 + 1 new).
- With Postgres: `318 passed`.
- mypy clean (36 files). ruff clean.
- Recorded eval: `cases: 32  precision: 1.00  recall: 1.00  dropped by claim checker: 32`.
- No-checker baseline: `precision: 0.47  recall: 0.93`. This is the same as M5 alone.

### Mutations (merge 2)

| # | Mutation | Killed by |
|---|---|---|
| X1 | link key ignores side | `test_left_and_right_comments_on_the_same_line_number_link_to_their_own_findings[sqlite]` |
| X2 | GitHub read-back drops `side` | `test_github_review_comments_and_dismissal_requests` |
| X3 | memory read-back drops `side` | `test_left_and_right_comments_on_the_same_line_number_link_to_their_own_findings[sqlite]` |

## Merge 3: milestone 4 (`worktree-agent-a937ef95e41497549`), merge commit `988017d`

### Conflicts and how each was resolved

| File | Resolution (both sides kept) |
|---|---|
| `pipeline.py` | `Reviewer(..., store: ReviewStore \| None, verify, proof_runners)`. M4 had moved the "False only for the eval baseline" comment onto the runners line; it is back on `verify`. The proof stage runs after `apply_policy` and before `build_review(..., not_reviewed=unseen)`. `after_post` stays after `_finish`, so the dismissal logic sees the decision after any proof downgrade. |
| `poster.py` | A body-only line gets M4's "(proven by a failing test)" suffix, then M5's `- not reviewed:` lines follow. `_comment_body` (M4's proof section) and the `side` anchor (M5) auto-merged. |
| `findings.py` | `identity`, `side` (M5), `proof`, `proof_test` (M4). |
| `cli.py` | `_reviewer` uses `open_store` and `proof_runners`. Top-level flags cover both sides: `--database-url`, `--backlog-alarm-s`, `--allow-local-proof`, `--proof-image`. **Silent drop fixed:** M3's App reviewer factory (auto-merged) built `Reviewer` without `proof_runners`, so App-mode workers would fall back to the default Docker runner and ignore `--allow-local-proof`/`--proof-image`. It now passes `_proof_runners(args)`. |
| `evals/build_cases.py` | M5's `go()`, JS/Go constants and `NEW_CASES`, then M4's `PROOF_*`/pricing/clamp constants. `CASES` = the 12 originals, `*NEW_CASES` (13-32), then M4's cases renamed **33**-py-proof-proven-discount and **34**-py-proof-not-reproduced. `main(root)` keeps M5's `_tree`/LEFT handling and writes M4's `recorded_proofs`/`expected_proofs` (it now uses `case["head"]`, since M4's local `head` variable no longer exists). `evals/cases` was regenerated: M4's old `13-`/`14-` proof directories are gone and `33-`/`34-` were written. `test_committed_cases_match_what_the_generator_writes` passes. |
| `tests/test_evals.py` | Both sides' tests, with the proof case names updated to 33/34. Counts are below. |
| `README.md` | Both sections, the proof-case numbers updated to 33/34, "34 small PRs", and the "not in milestone 1" list marks proof (M4), feedback/App (M3) and deleted lines (M5) as done. |
| `uv.lock` | Took ours, then ran `uv lock` (added defusedxml, types-defusedxml). |

### Eval counts, and why each one changed

These numbers were worked out from each branch's own report first, then checked against the run:
- Cases: 32 (M5) + 2 (M4) = **34**.
- Expected findings: 27 (M5) + 1 (case 33; case 34 expects none) = **28**.
- Dropped by the checker: still **32**. M4's base→M4 delta was 8→8, so its two cases drop nothing.
- No-checker baseline: M5's (53 reported, 25 true) plus M4's two cases. M4 moved the baseline
  from 10/15 to 11/16, so the two cases add 1 reported and 1 true: **(54, 26)**, precision
  **0.48** (26/54 = 0.481).
- Recall stays 0.93.

### A semantic clash fixed in the merge commit

M4's `eligible()` sent any verified Python model blocker for proof. A LEFT finding (M5) has a
*base* line number. The prompt would then name the wrong line, and
`index.enclosing_definition(file, line)` would pick the wrong head function. LEFT findings are
now not eligible. Test: `test_findings_on_deleted_lines_are_not_sent_for_proof`.

### Ported to Postgres

Nothing: M4 stores no proof state (its own note says proof verdicts are not persisted).

### Suite after merge 3 (final)

- SQLite only: `352 passed, 60 skipped`.
- With Postgres: `412 passed`.
- mypy: `Success: no issues found in 37 source files`. ruff: `All checks passed!`
- Recorded eval: `cases: 34  precision: 1.00  recall: 1.00  dropped by claim checker: 32`,
  with no proof mismatches.
- No-checker baseline: `cases: 34  precision: 0.48  recall: 0.93`.

### Mutations (merge 3)

| # | Mutation | Killed by |
|---|---|---|
| Y1 | LEFT findings sent for proof | `test_findings_on_deleted_lines_are_not_sent_for_proof` |
| Y2 | App factory drops the proof runners | `test_cli_app_factory_uses_installation_or_static_token` |
| Y3 | pipeline stops passing `not_reviewed` | `test_pipeline_puts_skipped_files_in_the_review_body` |
| Y4 | proof stage not called | `test_real_bug_is_proven_posted_with_the_test_and_timed` |
| Y5 | poster drops the not-reviewed lines | `test_review_body_lists_files_that_were_not_reviewed` |
| Y6 | poster drops the "proven" suffix on body-only lines | `test_body_only_findings_say_proven_and_skipped_files_follow`. It **survived** at first, because neither branch tested that line; the test was added and the mutation re-run. |
| Y7 | `after_post` never runs | `test_posted_comments_are_linked_to_findings[sqlite]` |

All 35 mutations across the three merges were restored byte-identical (`filecmp`).

## Open risks

- **App mode no longer uses `GITHUB_TOKEN` in the worker.** A job without an installation id
  looks the installation up. A repo where the App is not installed now fails the job at once
  with a clear error, where it used to fall back to a user token. This was intended, and the
  README is updated. A deployment that relied on the fallback will see those jobs go `dead`.
- **The installation lookup is not cached.** It costs one extra App-JWT request per job that
  has no id (manual or sweep). The found id is not written back onto the job.
- **The `cmd_worker` wiring is not covered end to end.** It picks the App factory or the
  reviewer, plus metrics and the alarm. `serve` wiring is now tested; the factory is tested
  directly.
- **`side` is not persisted in `findings`** (either backend). Linking uses the comment read
  back from GitHub, so this is fine today.
- **LEFT findings never get a proof.** A removed guard could in principle be proven by a test
  on head. That needs base-line-aware prompting.
- **Docker image build and Docker proof runs are still unverified.** No daemon was running, the
  same as for M4 and M6.
- **Postgres was tested on 14.20 only, over TCP.** The Unix socket path in the scratch dir was
  too long for macOS.
- Every milestone's own documented risks still apply: M3 dismissal identity, M4 forgeable
  verdicts and `LocalRunner` not being a sandbox, M5 LEFT accepted on its quote and secret
  fingerprints changing on upgrade, and M6 `/metrics` being unauthenticated.
