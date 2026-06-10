# Admin Config Drafts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Masakari DB-backed config drafts with validate, diff, and plan endpoints for the Horizon plugin.

**Architecture:** Store config draft state in the Masakari SQL DB, not etcd. Extend the existing `admin-config` API extension with a sibling `admin-config-drafts` resource and keep apply/rollback out of this phase.

**Tech Stack:** Masakari OpenStack API extensions, oslo.db/SQLAlchemy, Alembic migrations, oslo.policy, existing stestr unit tests.

---

### Task 1: DB Model and Persistence

**Files:**
- Modify: `masakari/db/sqlalchemy/models.py`
- Modify: `masakari/db/api.py`
- Modify: `masakari/db/sqlalchemy/api.py`
- Create: `masakari/db/sqlalchemy/migrations/versions/9f33c2d21f7a_add_admin_config_drafts.py`
- Test: `masakari/tests/unit/db/test_db_api.py`
- Test: `masakari/tests/unit/db/test_migrations.py`

- [x] Write failing DB API tests for create/get/list/update/delete.
- [x] Add Alembic migration and migration check for `admin_config_drafts`.
- [x] Add SQLAlchemy model and DB API methods.
- [x] Run DB tests.

### Task 2: Draft Validation, Diff, and Plan

**Files:**
- Modify: `masakari/api/openstack/ha/admin_config.py`
- Test: `masakari/tests/unit/api/openstack/ha/test_admin_config.py`

- [x] Write failing controller tests for create, update, validate, diff, and plan.
- [x] Validate only known `[staged_recovery]` options and enforce basic type/min checks.
- [x] Build masked diffs against effective config.
- [x] Build a plan that marks runtime-safe options separately from reconfigure-required options.

### Task 3: Routes and Policies

**Files:**
- Modify: `masakari/api/openstack/ha/admin_config.py`
- Modify: `masakari/policies/admin_config.py`
- Test: `masakari/tests/unit/api/openstack/ha/test_admin_config.py`

- [x] Add `admin-config-drafts` resource and custom routes for validate, diff, and plan.
- [x] Add policy rules for draft list/detail/create/update/delete/validate/diff/plan.
- [x] Run admin config API route tests.

### Task 4: Verification

**Files:**
- All files touched above.

- [x] Run targeted DB and admin config tests.
- [x] Run policy/extension tests.
- [x] Run full suite outside sandbox if socket binding is blocked.
- [x] Review staged diff before commit.
