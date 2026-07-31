# AA-177 kernel readiness-promotion ingress investigation

## Requested question

Which kernel functions and SQL predicates convert a card created with `initial_status='blocked'` and no scheduling parents into `ready`, then into a claimed/spawned run, and which existing tests cover each ingress?

Scope was read-only: Hermes source/tests/history, the live Kanban SQLite databases through SQLite `mode=ro`, and the SDLC analysis documents. No builds or tests were run, and no production code/configuration/card/database was changed.

## Conclusion

The defect is kernel-owned and is not primarily a dispatcher-only admission problem. `create_task()` correctly inserts the explicit card as `status='blocked'`, but the normal `recompute_ready()` sweep intentionally scans both `todo` and `blocked` rows. For a parent-free card, its parent query returns zero rows and `all([])` is true. Unless the task has a later explicit `blocked` event recognized by `_has_sticky_block()`, the sweep executes `UPDATE tasks SET status = 'ready' ...` and emits `promoted`. The dispatcher then selects that row with `status='ready' AND claim_lock IS NULL`, calls `claim_task()`, and spawns it when it has an assignee.

Therefore the minimal repair seam is the kernel promotion predicate/association for explicit initial blocks (or an equivalent durable distinction in the task/event model), with a regression test at `recompute_ready()` plus an end-to-end dispatch ingress assertion. A dispatcher-only filter would be incomplete: direct calls to `recompute_ready()` and promotion-triggering paths such as completion/specification/archive still expose the same transition, and the dispatcher currently trusts the `ready` row produced by the kernel.

## Exact source evidence

### 1. Explicit blocked admission is written by `create_task`

`hermes_cli/kanban_db.py:2387-2418` defines `create_task()` and documents the ordinary status contract. Validation allows only `running` or `blocked` for `initial_status` at `:2438-2441`.

The relevant branch is `hermes_cli/kanban_db.py:2581-2599`:

- `:2585-2590`: when `initial_status == 'blocked'`, `task_status = 'blocked'`; parent IDs are only validated if supplied.
- `:2591-2606`: otherwise, ordinary creation starts `ready`, then changes to `todo` if any parent is not done.
- `:2632-2664`: `task_status` is inserted into `tasks.status`.
- `:2670-2683`: the creation event records the inserted status and the supplied parent list.

For a parent-free explicit blocked card, the durable initial row is thus `tasks.status='blocked'`, with a `created` event payload containing `status='blocked'` and `parents=[]`; no `blocked` event is emitted by `create_task()`.

The feature was introduced by commit `fb9620889238d6aa9c6844c21a86c9b677c14bd0` (`feat(kanban): add initial-status for human-ops cards`). That commit changed the insertion branch but did not add tests in `tests/` (the commit stat contains only `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`, and `tools/kanban_tools.py`).

### 2. Kernel promotion converts the blocked row to ready

`hermes_cli/kanban_db.py:3244-3279` defines `_has_sticky_block()`. Its SQL at `:3273-3278` looks only for the newest `task_events.kind IN ('blocked', 'unblocked')`, ordered by event ID. It returns sticky only when that newest event is `blocked` (`:3279`). An initial `created` event is not considered sticky.

`hermes_cli/kanban_db.py:3282-3366` defines `recompute_ready()`:

- `:3317-3320`: candidate SQL is `SELECT ... FROM tasks WHERE status IN ('todo', 'blocked')`.
- `:3324-3329`: blocked rows are skipped only when `_has_sticky_block()` is true.
- `:3330-3336`: parent statuses are loaded by joining `task_links` to `tasks`; promotion is eligible when `all(parent.status in ('done','archived') for parent in parents)`.
- With no scheduling parents, the result set is empty and Python `all([])` is true.
- `:3337-3358`: for the blocked case, after the failure-limit guard, the exact mutation is `UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'blocked'`.
- `:3360-3365`: the todo case has the analogous ready update, followed by `_append_event(..., 'promoted', ...)`.

This is the direct ingress from `initial_status='blocked'`/no parents to `ready`: the initial row is not sticky because creation emits no `blocked` event, and the no-parent predicate vacuously passes.

`recompute_ready()` is not dispatcher-exclusive. It is also called after successful completion at `hermes_cli/kanban_db.py:4177-4178`, after triage specification at `:5199-5205`, after archive at `:5450-5454`, after archived deletion at `:5498-5503`, and after unlinking a dependency at `:2880-2885`. Thus a dispatcher-only guard would leave other kernel-triggered promotion paths inconsistent.

### 3. Dispatcher selects ready rows and invokes claim/spawn

`hermes_cli/kanban_db.py:7394-7463` defines `_dispatch_once_locked()`. Its documented sequence (`:7410-7418`) performs recovery, then promotion, then claims/spawns ready tasks. The actual call is `result.promoted = recompute_ready(conn, failure_limit=failure_limit)` at `:7462-7463`.

The ready-row ingress query is `hermes_cli/kanban_db.py:7480-7484`:

```sql
SELECT id, assignee FROM tasks
WHERE status = 'ready' AND claim_lock IS NULL
ORDER BY priority DESC, created_at ASC
```

Rows without an assignee are skipped/routed later in the dispatcher (`:7536-7553`), but an assignee allows the row to continue to claim.

`claim_task()` is `hermes_cli/kanban_db.py:3373-3492`:

- Parent safety check SQL at `:3397-3402` rejects any linked parent not in `('done','archived')`; a parent-free card has no matching row and passes this guard.
- The atomic claim SQL at `:3434-3445` requires `id=?`, `status='ready'`, and `claim_lock IS NULL`, then sets `status='running'`, claim lock/expiry, and start time.
- `:3457-3477` inserts the `task_runs` row and stores `current_run_id`.
- `:3479-3483` emits a `claimed` event.

The remainder of `_dispatch_once_locked()` (after the inspected range) calls `claim_task()` for each eligible ready row and records/spawns the worker. The source docstring explicitly states that ready rows are atomically claimed and passed to `spawn_fn` (`:7414-7418`).

### 4. Manual promotion is a separate, deliberate ingress

`hermes_cli/kanban_db.py:4981-5048` defines `promote_task()`:

- `:5000-5011` accepts only current status `todo` or `blocked`.
- `:5013-5028` checks linked parent statuses unless `force=True`; with no parents, `unsatisfied` is empty.
- `:5033-5046` executes `UPDATE tasks SET status='ready' WHERE id=? AND status IN ('todo','blocked')` and emits `promoted_manual`.

This manual path is not the observed automatic source, but it confirms that the current kernel API treats blocked-to-ready as a valid generic transition and has no explicit-initial-block distinction.

## Existing test coverage

### Covered promotion/claim behavior, but not explicit initial blocked admission

- `tests/hermes_cli/test_kanban_promote.py:39-64` constructs a stuck `todo` child and verifies `promote_task()` changes it to `ready` when parents are done.
- `tests/hermes_cli/test_kanban_promote.py:66-81` verifies the unsatisfied-parent refusal and force override.
- `tests/hermes_cli/test_kanban_promote.py:152-159` verifies a manually changed `blocked` row can be promoted to `ready`.
- `tests/hermes_cli/test_kanban_db.py:314-350` covers `recompute_ready()` cascading and promotion of a blocked row with done parents (the setup directly updates the row to `blocked`; it does not use `initial_status='blocked'`).
- `tests/hermes_cli/test_kanban_db.py:1203-1340` covers the circuit-breaker failure-limit branches of `recompute_ready()`.
- `tests/hermes_cli/test_kanban_db.py` dispatch coverage around `:1645-1741` verifies ready rows are selected/spawned and parent completion can make a child spawnable.
- `tests/hermes_cli/test_kanban_blocked_sticky.py:56-100` covers worker/operator `blocked` events staying blocked under `recompute_ready()` and `:107-170` covers circuit-breaker/direct-DB blocks recovering.
- `tests/hermes_cli/test_kanban_blocked_sticky.py:214-271` covers the block/promote/crash/gave_up loop for a worker-issued block.
- `tests/hermes_cli/test_kanban_block_kinds.py:138-162` covers dependency blocks routing to `todo` and later promotion after parent completion.

### Missing regression coverage

No test file currently uses `initial_status='blocked'` (repository test search returned no matches). There is no assertion that:

1. `create_task(..., initial_status='blocked', parents=[])` remains blocked across `recompute_ready()`;
2. the same card remains absent from dispatcher ready-row selection and has no claim/spawn/run; or
3. an explicitly released/authorized path can later transition it to ready and claim it.

The existing sticky tests do not cover this state because they first call `claim_task()` and then `block_task()`, which creates the explicit `blocked` event that `_has_sticky_block()` requires.

## Live row/event evidence (read-only)

The literal `/Users/asyd/.hermes/kanban.db` requested in the card is a small default DB containing one task and no matching `created` event with `status='blocked'`; it cannot provide a representative live ingress trace.

The active board DB `/Users/asyd/.hermes/kanban/boards/sdlc-control-plane/kanban.db` was queried read-only (`mode=ro`) for `created` event payloads with `status='blocked'` and `parents=[]`. It contains direct traces of the defect:

- `t_90c91d8a` (`AA-177 developer: protected continuation admission implementation`): created event row `20515` at epoch `1785071172` records `status='blocked'`, `parents=[]`; event `20525` is `promoted`, event `20533` is `claimed` with run ID `2293`, and event `20534` is `spawned` (PID `77054`). It later records `crashed`/`gave_up`, a second `promoted` (`20549`), and a second `claimed` (`20552`) before an explicit `blocked` event `20564`.
- `t_283ae199` (`AA-177 internal human approval: continuation-admission plan revision v1`): created event `20514` records blocked/parent-free; `20524` promoted, `20531` claimed (run `2292`), and `20532` spawned before `20542` blocked.
- `t_a622da2e` (`AA-177 independent QA: disposable-board continuation-admission verification`): created event `20516` records blocked/parent-free; `20526` promoted, `20535` claimed (run `2294`), and `20536` spawned. It repeatedly shows `dependency_wait` followed by `promoted`/`claimed`/`spawned` (for example `20566` → `20567`/`20568`/`20569`, and `20622` → `20631`/`20633`/`20634`).
- `t_27f52320` (`AA-177 rollout gate: serving-process runtime-load provenance`): created event `20517` records blocked/parent-free; `20527` promoted, `20537` claimed (run `2295`), and `20538` spawned.
- `t_0ad70101` (`AA-177 orchestrator-only reconciliation after verified rollout`): created event `20518` records blocked/parent-free; `20528` promoted, `20539` claimed (run `2296`), and `20540` spawned. After several dependency waits, the same row has repeated promotion/claim/spawn cycles, including `20632`/`20635`/`20636`, before an explicit `blocked` event `20707`.

These event sequences establish the causal order required by the question: initial `created(status=blocked, parents=[])` → kernel `promoted` → dispatcher `claimed`/`spawned`. They also show why later sticky-block evidence does not protect the admission window: the first promotion occurs before any `blocked` event exists.

## Verification/reproduction steps (not executed as tests)

Use an isolated temporary `HERMES_HOME` in a future regression test or disposable database:

1. Initialize a Kanban DB and create an assignee-backed task with `create_task(..., initial_status='blocked', parents=[])`.
2. Assert the task row is `blocked`, its only initial event is `created` with payload status `blocked`, and no `blocked` event exists.
3. Call `recompute_ready(conn)` and assert the repaired behavior leaves it `blocked` and returns zero promotions.
4. Call `dispatch_once(conn, spawn_fn=fake_spawn)` and assert no `promoted`, `claimed`, `spawned`, task run, or worker invocation occurs.
5. Exercise the explicitly authorized release mechanism chosen by the repair (for example a typed admission/unblock operation), then assert the normal `ready` → `claim_task()` → `task_runs` path works and parent gating remains intact.

## Bounded recommendation / impact on architect plan

1. Treat `hermes_cli/kanban_db.py` as the primary repair seam. Preserve the existing generic auto-recovery behavior for circuit-breaker blocks and the existing sticky behavior for worker/operator block events; add a durable, unambiguous marker or event association for `initial_status='blocked'` so `recompute_ready()` can exclude only admission-gated cards.
2. Add a focused `create_task(initial_status='blocked')` + `recompute_ready()` regression in `tests/hermes_cli/test_kanban_db.py` (or a new narrowly named initial-status test file), then add a dispatch-level assertion in the existing dispatch section that no claim/spawn occurs before explicit release.
3. Do not patch only `_dispatch_once_locked()`. The same `recompute_ready()` function is invoked from multiple kernel writers and is directly exposed through tool/CLI paths; a dispatcher-only filter would leave direct and post-completion promotion able to violate the initial blocked contract.
4. Keep the existing `tests/hermes_cli/test_kanban_blocked_sticky.py` tests: they cover a different state (a block event emitted after a worker run) and should remain as circuit-breaker/worker-block regression coverage.

No code or configuration change is recommended by this investigation; the report is the sole deliverable.
