# Admin Config Runtime Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Admin Config apply perform a real runtime etcd update for `staged_recovery.max_parallel_starts_per_host`.

**Architecture:** Keep the existing Admin Config controller shape and reuse `EtcdStagedRecoveryStore`, the same backend used by `/staged-recovery/start-limit`. Runtime-only drafts apply through `runtime_etcd`; drafts containing reconfigure-required changes keep the existing no-op behavior until a deployment backend exists.

**Tech Stack:** Masakari WSGI API extensions, oslo.config metadata, SQL-backed apply jobs, staged recovery etcd runtime config store, stestr unit tests.

---

### Task 1: RED API Tests

**Files:**
- Modify: `masakari/tests/unit/api/openstack/ha/test_admin_config.py`

- [x] Add a test that patches `masakari.api.openstack.ha.admin_config.staged_state.EtcdStagedRecoveryStore`, applies a draft containing only `staged_recovery.max_parallel_starts_per_host`, and expects `store.set_max_parallel_starts_per_host(4)` plus result backend `runtime_etcd`.
- [x] Add a test that patches the same store, calls `GET /admin-config/effective`, and expects `max_parallel_starts_per_host` to be returned as `{'value': 4, 'source': 'runtime'}`.
- [x] Add a test that applies a mixed draft with `max_parallel_starts_per_host` and `batch_delay`, expects backend `noop`, and asserts `set_max_parallel_starts_per_host` was not called.
- [x] Add failure tests for runtime backend validation errors and unexpected backend failures.
- [x] Run `stestr run masakari.tests.unit.api.openstack.ha.test_admin_config.AdminConfigTestCase` and confirm the new tests fail for missing runtime behavior.

### Task 2: Runtime Apply Implementation

**Files:**
- Modify: `masakari/api/openstack/ha/admin_config.py`

- [x] Import `masakari.engine.drivers.taskflow.staged_state_etcd as staged_state`.
- [x] Add a `_runtime_store()` helper returning `staged_state.EtcdStagedRecoveryStore(CONF, owner='api')`.
- [x] Add a helper that classifies plan steps as runtime-only when every step has `action == 'runtime_update'`.
- [x] Add runtime apply logic that calls `set_max_parallel_starts_per_host(value)` for the supported option and builds `result.backend = 'runtime_etcd'`, `changed = True`, and `runtime_updates`.
- [x] Keep the existing no-op result for mixed or reconfigure-required drafts.
- [x] Update `_staged_recovery_effective()` to use `get_max_parallel_starts_per_host_with_source()` and return value/source for the runtime option.
- [x] Record failed apply jobs when the runtime backend rejects or fails the update.
- [x] Run the admin config test class and confirm it passes.

### Task 3: Documentation and Verification

**Files:**
- Modify: `docs/specs/CODEX_MASAKARI_ADMIN_CONFIG_API_IMPLEMENTATION.md`
- Create: `docs/superpowers/specs/2026-06-10-admin-config-runtime-apply-design.md`
- Create: `docs/superpowers/plans/2026-06-10-admin-config-runtime-apply.md`

- [x] Update the API implementation document so it no longer says apply is always no-op for runtime-only drafts.
- [x] Run targeted tests: admin config API, staged recovery API, staged state store.
- [x] Run flake8 for changed Python files.
- [x] Run `git diff --check`.
- [x] Review staged diff, commit, and push.
