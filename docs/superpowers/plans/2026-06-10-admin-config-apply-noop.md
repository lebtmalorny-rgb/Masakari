# Admin Config No-op Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe no-op apply flow for Masakari admin config drafts so Horizon can drive apply-job UX without changing live configuration.

**Architecture:** Persist apply jobs in the Masakari SQL DB alongside config drafts. `POST /admin-config-drafts/{draft_id}/apply` validates/plans the draft, creates an apply job, marks it `succeeded` for the no-op backend, and updates the draft status to `applied`; no config files, runtime config, services, or etcd keys are changed in this phase.

**Tech Stack:** Masakari OpenStack API extensions, oslo.db/SQLAlchemy, Alembic migrations, oslo.policy, existing stestr unit tests.

---

### Task 1: Apply Job Persistence

**Files:**
- Modify: `masakari/db/sqlalchemy/models.py`
- Modify: `masakari/db/api.py`
- Modify: `masakari/db/sqlalchemy/api.py`
- Modify: `masakari/exception.py`
- Create: `masakari/db/sqlalchemy/migrations/versions/4f2d6c8b91a0_add_admin_config_apply_jobs.py`
- Test: `masakari/tests/unit/db/test_db_api.py`
- Test: `masakari/tests/unit/db/test_migrations.py`

- [x] Write failing DB API tests for apply job create/get/list/update.
- [x] Run the DB apply-job tests and confirm missing DB APIs fail.
- [x] Add `ConfigApplyJobNotFound`, SQLAlchemy model, Alembic migration, migration check, and DB API methods.
- [x] Run DB apply-job and migration tests.

### Task 2: Apply and Apply Job API

**Files:**
- Modify: `masakari/api/openstack/ha/admin_config.py`
- Modify: `masakari/policies/admin_config.py`
- Test: `masakari/tests/unit/api/openstack/ha/test_admin_config.py`

- [x] Write failing controller tests for no-op apply success, invalid draft apply rejection, job list/detail, route status code, and unsupported rollback.
- [x] Add `POST /admin-config-drafts/{draft_id}/apply` with `202 Accepted`.
- [x] Add `GET /admin-config-apply-jobs`, `GET /admin-config-apply-jobs/{job_id}`, and `POST /admin-config-apply-jobs/{job_id}/rollback`.
- [x] Register policy rules for draft apply and apply-job index/detail/rollback.
- [x] Run admin config API tests.

### Task 3: Verification and Commit

**Files:**
- All files touched above.

- [x] Run targeted admin config DB/API tests.
- [x] Run migration walk, policy/extension tests, and flake8 for touched files.
- [x] Run full suite outside sandbox if socket binding is blocked.
- [x] Review staged diff, commit, and push only Phase 3 files.
