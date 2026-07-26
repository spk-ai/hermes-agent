# AA-168 executable concurrent-dispatch proof investigation

Task: `t_e7e54103` (parallel investigation for architect task `t_de17d5f9`).

Question: what exact concurrency harness and test dispositions demonstrate that two concurrent claims/dispatch attempts produce exactly one running task/run/worker, while preserving `t_8aeaa706` without status mutation?

## Sources checked

- `hermes_cli/kanban_db.py:61-65`: documented concurrency contract is SQLite WAL + `BEGIN IMMEDIATE` + CAS on `tasks.status`/`tasks.claim_lock`; at most one claimer wins.
- `hermes_cli/kanban_db.py:3282-3366`: `recompute_ready()` promotes only tasks whose linked parents are `done` or `archived`; links are joined as `task_links.parent_id -> task_links.child_id`.
- `hermes_cli/kanban_db.py:3373-3484`: `claim_task()` performs the parent-completion guard, then the atomic `UPDATE ... WHERE status='ready' AND claim_lock IS NULL`; only the one-row winner inserts a `task_runs` row, sets `current_run_id`, and appends `claimed`.
- `hermes_cli/kanban_db.py:7328-7391`: `dispatch_once()` takes the board-scoped dispatch lock. A loser returns `DispatchResult(skipped_locked=True)` and performs no tick writes.
- `hermes_cli/kanban_db.py:7394-7420, 7462-7498, 7651-7707`: a dispatch tick reaps/recomputes, claims each ready row before spawning, and records the returned PID as `worker_pid`; failed claims are skipped and cannot spawn.
- `tests/hermes_cli/test_kanban_db.py:1142-1156`: existing `test_concurrent_claims_only_one_wins` races eight independent connections and asserts exactly one non-null claim and `running` status.
- `tests/hermes_cli/test_kanban_db.py:1350-1387`: `test_claim_rejects_when_parents_not_done` covers the claim gate, demotion to `todo`, `claim_rejected`, and absence of `claimed`.
- `tests/hermes_cli/test_kanban_db.py:1389-1402`: `test_claim_succeeds_once_parents_done` covers the positive parent-gate path.
- `tests/hermes_cli/test_kanban_db.py:1405-1423`: `test_create_with_parents_stays_todo_until_parents_done` covers creation-time dependency gating and promotion.
- `tests/hermes_cli/test_kanban_db.py:3292-3323`: `test_unlink_tasks_triggers_recompute_ready` covers immediate promotion after unlinking a blocking edge.

Read-only live board checks:

- `kanban_show(t_8aeaa706)`: status was already `done`, `completed_at` was set, and its event history already contained a `completed` event before this investigation. Therefore the plan's old "remains nonterminal" assertion is impossible as a current-state assertion.
- `kanban_show(t_49805b57)`: `done`, explicitly superseded by `t_295c01b3`.
- `kanban_show(t_600b1055)`: `done`, explicitly superseded by `t_b6de7a2d`.
- `kanban_show(t_295c01b3)`: `done`, evidence commit `6618973`.
- `kanban_show(t_b6de7a2d)`: `done`, evidence commit `56e4d3d`.

No live board write, Jira operation, source/test/configuration edit, or product checkout access was performed.

## Executed proof

I ran an isolated disposable-board harness with a temporary `HERMES_HOME`/`HERMES_KANBAN_HOME`, explicitly clearing inherited `HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`, and `HERMES_KANBAN_WORKSPACES_ROOT`. It created one assigned ready task and invoked two `dispatch_once()` calls concurrently through two independent SQLite connections. The spawn function was a harmless stub returning the current PID; `hermes_cli.profiles.profile_exists` was stubbed only inside the disposable Python process so the synthetic assignee could pass the dispatcher’s profile gate.

Command shape (run from `/Users/asyd/.hermes/hermes-agent`; the temporary directory is automatically left outside the repository and can be removed after capture):

```bash
env -u HERMES_KANBAN_DB \
    -u HERMES_KANBAN_WORKSPACES_ROOT \
    -u HERMES_KANBAN_BOARD \
python -c '<isolated harness below>'
```

Harness pseudocode:

```python
home = tempfile.mkdtemp(prefix="aa168-kanban-proof-")
os.environ["HERMES_HOME"] = home
os.environ["HERMES_KANBAN_HOME"] = home
profiles.profile_exists = lambda _name: True
kb.init_db()
with kb.connect() as c:
    task_id = kb.create_task(c, title="synthesis", assignee="investigator")

def attempt(index):
    def spawn(task, workspace):
        return os.getpid()
    with kb.connect() as c:
        result = kb.dispatch_once(c, spawn_fn=spawn, max_spawn=1)
        return result

with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(attempt, (1, 2)))

with kb.connect() as c:
    task = kb.get_task(c, task_id)
    running_task_count = SELECT COUNT(*) FROM tasks WHERE status='running'
    open_run_count = SELECT COUNT(*) FROM task_runs
        WHERE task_id=? AND status='running' AND ended_at IS NULL
    runs = SELECT id, task_id, status, ended_at FROM task_runs WHERE task_id=?
    events = SELECT kind FROM task_events WHERE task_id=? ORDER BY id
```

Observed sanitized output:

```text
attempt 1: skipped_locked=true, spawned=[]
attempt 2: skipped_locked=false,
  spawned=[(t_3546d19a, investigator, <temporary workspace>)]
final task: status=running, current_run_id=1, worker_pid=<current harness PID>
running_task_count=1
open_run_count=1
runs=[{id: 1, task_id: t_3546d19a, status: running, ended_at: null}]
events=[created, claimed, tip_scratch_workspace, spawned]
```

The task ID and temporary path above are disposable harness identifiers, not live-board records. The result proves the operational invariant for concurrent dispatcher ticks: exactly one tick can hold the board dispatch lock; exactly one task is claimed/spawned; the losing tick reports `skipped_locked` and does not create a second run. The underlying claim CAS remains the required defense even if callers bypass the dispatcher; the existing eight-way `test_concurrent_claims_only_one_wins` is the direct proof for that path.

## Recommended executable acceptance case

Add a focused test only if the architect/human approves a regression test beyond the current unit coverage; do not modify it as part of this investigation. Recommended name:

`test_concurrent_dispatch_attempts_create_one_running_task_run_and_worker`

Use the existing `kanban_home` fixture, create one `assignee="..."` task, clear/override board environment as needed, and use two independent connections plus a `ThreadPoolExecutor(max_workers=2)`. Pass a stub `spawn_fn` that increments a thread-safe list and returns a deterministic positive fake PID. If testing `dispatch_once()` itself, make the assignee pass the profile gate without touching real profiles (a local monkeypatch in the test process is acceptable). Assertions must be relational and observable:

1. `sum(len(result.spawned) for result in results) == 1`.
2. `sum(result.skipped_locked for result in results) == 1` (or, if a platform’s in-process lock serializes without a visible skip, still require one spawn and one final run; document the platform behavior).
3. `len(spawn_calls) == 1`.
4. Final task status is exactly `running`; `current_run_id` and `worker_pid` are non-null.
5. `SELECT COUNT(*) FROM task_runs WHERE task_id=? AND status='running' AND ended_at IS NULL` is exactly `1`.
6. `SELECT COUNT(*) FROM tasks WHERE status='running'` is exactly `1` in the disposable board.
7. There is exactly one `claimed` and one `spawned` event for the target; no second run exists.
8. Cleanup closes both connections and removes the disposable board/temp workspace. Do not call `complete_task()` unless the test owns the fake worker lifecycle; if it does, assert the terminal cleanup state separately.

For a true cross-process acceptance variant, run two short Python worker processes with the same explicit temporary DB path and a barrier file/event before `dispatch_once()`. Capture each process’s JSON result, wait for both, then read the DB in a third read-only connection. Require the same one-spawn/one-run assertions. This is stronger than threads for the cross-process lock, but it must remain disposable and must not use the active `spk-sdlc` board.

## Test dispositions

| Existing test | Disposition | Reason |
|---|---|---|
| `test_create_task_with_parent_is_todo_until_parent_done` (`:225-231`) | Keep unchanged | Creation status and promotion contract remains valid. |
| `test_recompute_ready_fan_in_waits_for_all_parents` (`:356-364`) | Keep unchanged | Proves synthesis waits for every evidence parent; not a concurrency test. |
| `test_claim_once_wins_second_loses` (`:371-377`) | Keep unchanged | Sequential CAS sanity check; complements, but does not replace, the race proof. |
| `test_concurrent_claims_only_one_wins` (`:1142-1156`) | Keep and treat as the direct claim-race regression | Already executes eight concurrent independent connections and proves one winner. Do not weaken it into a snapshot or replace it with only a sequential assertion. |
| `test_claim_rejects_when_parents_not_done` (`:1350-1387`) | Keep unchanged | Guards against a stale/racy `ready` status bypassing parent completion; asserts no `claimed` event. |
| `test_claim_succeeds_once_parents_done` (`:1389-1402`) | Keep unchanged | Positive completion gate. |
| `test_create_with_parents_stays_todo_until_parents_done` (`:1405-1423`) | Keep unchanged | Creation/dispatcher-tick sequencing is already covered. |
| `test_unlink_tasks_triggers_recompute_ready` (`:3292-3323`) | Keep unchanged | Correctly tests unlink semantics; does not authorize unlinking the live reverse cards. |
| Proposed concurrent dispatcher case | Add as a new focused regression/operational test if approved | It closes the audit gap by proving dispatch-level one-worker/one-run behavior, not merely one successful claim. |

The requested acceptance proof must not mutate `t_8aeaa706`. Because live state was already `done`, the correct invariant is: the reconciliation harness writes only its disposable board; no reconciliation action changes, reopens, completes, or dispatches `t_8aeaa706`, `t_49805b57`, or `t_600b1055`; the architect uses the already-complete clean reports `t_295c01b3` and `t_b6de7a2d` as evidence. Verify this with before/after read-only `kanban_show` snapshots, not by asserting that `t_8aeaa706` is currently nonterminal.

## Exact impact on architect plan

1. Replace the plan’s single “claim once” acceptance criterion with the dispatch race above: two simultaneous attempts, exactly one `spawned` result/call, one `running` task, one open `task_runs` row, one `worker_pid`, and no duplicate event/run.
2. Retain `test_concurrent_claims_only_one_wins` as existing claim-layer coverage and list the new dispatch-level case separately; the layers are not interchangeable.
3. Make the proof disposable and fail closed on environment contamination by clearing inherited `HERMES_KANBAN_*` path overrides. Never point it at `/Users/asyd/.hermes/kanban/boards/spk-sdlc/kanban.db`.
4. Correct the preservation wording from “`t_8aeaa706` remains nonterminal” to “no reconciliation action mutates its already-done status or uses completion to unblock obsolete reverse cards.”
5. Add the already-done live-state contradiction as a risk/decision gate and preserve the separate human approval requirement for any live-board quarantine/archive action.

No causal uncertainty remains about the concurrency mechanism. The only operational caveat is that a dispatch-level test must account for the assignee/profile gate and must isolate its DB/workspace; the direct claim race is already executable in the current suite.

## Verification status

- The disposable dispatch harness above executed successfully and produced one `spawned` result, one `running` task, one open run, one worker PID, and one `skipped_locked` loser.
- The canonical targeted command was attempted: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k 'concurrent_claims_only_one_wins or claim_rejects_when_parents_not_done or create_with_parents_stays_todo_until_parents_done'`.
- No existing pytest tests executed because the repository `.venv` lacks the `pytest` module (`/Users/asyd/.hermes/hermes-agent/.venv/bin/python: No module named pytest`). This is an environment dependency blocker, not a test failure; no passing pytest result is claimed.
