# Fix round 3: findings quoted beside a deleted guard, live proof expectations

Branch `fix-round-3`, based on local `main` at be1bebb.

## What was built

**R1. A real bug on a deleted line, quoted by the code around it, is kept.** A live run showed
the model reporting a removed guard (cases 29 and 32) while quoting the surviving lines around
the gap, not the deleted line. The checker dropped both as "evidence quote is only in code this
PR did not change". The new rule is diff geometry only, no wording:

- The parser now records, per hunk, `deleted_at`: the head line just after each run of deleted
  lines. A run that deletes only blank lines does not count (fix round 1's B7 still holds).
- A quote that sits only on unchanged head lines is kept when all of these hold:
  1. every quoted line is a context line of a hunk that deletes code;
  2. the quote touches the deleted run: it straddles it, or only blank lines separate them;
  3. those blank lines number at most `snap_distance`.
- The finding is anchored on the RIGHT side, on the quoted line nearest the deleted run (ties go
  to the line nearest the model's own line, then the lower one). Context lines are commentable.
- The finding gets the note `anchored on context line N, next to lines this PR deleted`.
- Order is unchanged otherwise: a quote on an added line wins, then a quote on deleted base
  lines (LEFT), then this rule. Everything else drops as before.

Eval cases 35 and 36 replay the two live answers exactly, each with one negative finding that
quotes unchanged code outside the hunk.

**R2. Live eval does not count proofs that had nowhere to run.** `run_eval` / `run_case` take
`live` and an optional `runners` map. In live mode the eval gives only the Docker runner (the
proof test is model-written code, so it never runs on the host). When live, the case has proof
expectations, and the runner its config names is absent or Docker is down, the case reports
`proof <file>:<line> skipped (no sandbox)` in `proof_skipped` and no proof mismatch. Recorded mode
checks proofs exactly as before. `DockerRunner.unavailable()` is the public form of the existing
daemon check. `reviewhound eval --live` passes `live=True`.

## Acceptance criteria

| Criterion | Test | Result |
|---|---|---|
| Live case 29 answer survives | `test_checker::test_live_answer_quoting_code_after_a_removed_role_check_is_kept_beside_it` | pass, RIGHT line 5, verified, note set |
| Live case 32 answer survives | `test_checker::test_live_answer_quoting_code_around_a_removed_unlock_is_kept_beside_it` | pass, RIGHT line 11 |
| Anchor on the line nearest the deletion | `test_checker::test_quote_beside_a_deletion_anchors_on_the_line_nearest_the_gap` | pass |
| Needs no base read (geometry only) | `test_checker::test_beside_a_deletion_needs_no_base_reader` | pass |
| Unchanged code 10 lines away drops | `test_checker::test_quote_of_unchanged_code_ten_lines_from_the_deletion_is_dropped`, `..._only_blank_lines_within_snap_distance...[10]` | pass |
| snap_distance boundary (3 kept, 4 dropped) | `test_checker::test_only_blank_lines_within_snap_distance_may_separate_quote_and_deletion` | pass |
| Code between quote and deletion drops | `test_checker::test_code_between_the_quote_and_the_deletion_drops_it` | pass |
| Addition-only hunk drops | `test_checker::test_quote_next_to_an_addition_only_hunk_is_dropped` | pass |
| Quote running past the hunk drops | `test_checker::test_quote_running_past_the_hunk_context_is_dropped` | pass |
| Hallucinated quote drops | `test_checker::test_hallucinated_quote_beside_a_deletion_is_dropped` | pass |
| Parser records deletion points, ignores blank-only runs | `test_diff::test_parse_patch_records_the_head_line_after_each_deleted_run` | pass |
| Live answers in the recorded eval, P/R 1.00 | `test_evals::test_recorded_eval_set_scores_and_checker_earns_its_keep` | pass: 36 cases, P 1.00, R 1.00, 34 dropped |
| Live, no sandbox: skipped, not missed | `test_evals::test_live_eval_without_a_sandbox_skips_proof_expectations` | pass |
| Live gives no local runner | `test_evals::test_live_eval_gives_no_local_runner_by_default` | pass |
| Live with a usable runner still checks | `test_evals::test_live_eval_with_a_usable_runner_still_checks_proofs`, `..._with_docker_up_checks_proofs_and_with_it_down_skips` | pass |
| Recorded mode unchanged without a sandbox | `test_evals::test_recorded_eval_without_a_sandbox_still_counts_proof_mismatches` | pass |

Runs: `pytest` 554 passed, 151 skipped. mypy strict clean (38 files). ruff clean.
Recorded eval: `cases: 36  precision: 1.00  recall: 1.00  dropped by claim checker: 34`.
No-checker baseline: `cases: 36  precision: 0.48  recall: 0.93` (58 reported, 28 true).

## Mutation table

Each mutation was applied, the four affected test files run, the file restored from a backup and
`filecmp`-compared (all 13 restored byte-identical).

| Mutation | Killed by |
|---|---|
| M1 quoted lines need not be hunk context | `test_quote_running_past_the_hunk_context_is_dropped` |
| M2 code may sit between quote and deletion | `test_code_between_the_quote_and_the_deletion_drops_it`, `test_quote_found_nowhere_is_still_dropped_and_head_quotes_stay_right` |
| M3 distance `>=` instead of `>` | `test_only_blank_lines_within_snap_distance...[3-None]` |
| M4 no distance limit | `test_only_blank_lines_within_snap_distance...[4]`, `[10]` |
| M5 added lines count as deletion points | `test_quote_next_to_an_addition_only_hunk_is_dropped`, `test_evidence_only_in_unchanged_code_is_dropped_even_if_line_is_changed`, `test_recorded_eval_set_scores...` |
| M6 blank-only deleted runs count | `test_parse_patch_records_the_head_line_after_each_deleted_run`, `test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left` |
| M7 anchor ignores nearness to the deletion | `test_quote_beside_a_deletion_anchors_on_the_line_nearest_the_gap` |
| M8 no note when the line did not move | both live-answer tests |
| M9 adjacency rule removed | both live-answer tests and 5 more |
| M10 skip proofs in recorded mode too | `test_recorded_eval_without_a_sandbox_still_counts_proof_mismatches` |
| M11 skip every live proof, runner or not | `test_live_eval_with_a_usable_runner_still_checks_proofs`, `..._with_docker_up...` |
| M12 Docker down counts as usable | `test_live_eval_with_docker_up_checks_proofs_and_with_it_down_skips` |
| M13 live mode also gets the local runner | `test_live_eval_gives_no_local_runner_by_default` |

## Not done

- No live run (`--live` is off-limits here). The two live answers are replayed from the lead's
  report; other live phrasings of the same bugs are untested.
- The live answers' claims were not reported, so cases 35 and 36 carry one plausible claim each.

## Risks for whoever merges

- The rule widens what the checker keeps: any quote of context touching a deleted run of code
  now survives, even if the finding is about something else nearby. Claims still have to hold,
  and the finding is posted on an unchanged line, which reviewers may find odd.
- Live eval no longer runs proofs on the host. A live run with Docker down now reports
  `skipped (no sandbox)` for cases 33 and 34 instead of a proof result.
- The eval's recorded counts changed: 36 cases, 30 expected, 34 dropped, baseline 58/28.
