# Masakari Horizon Admin API MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add the first Masakari-side API surface required by the Horizon Masakari HA plugin.

**Architecture:** Add extension-style API resources that match the existing Masakari router: `admin-overview`, `admin-config`, `admin-recovery-workflows`, `admin-diagnostics`, and `admin-audit`. Keep behavior read-only or lightweight for the first patch; defer config draft/apply/rollback and unsafe staged lease release to later work.

**Tech Stack:** Masakari OpenStack API extensions, oslo.policy, oslo.config, stevedore task entry points, existing unit test framework.

---

### Task 1: Admin Overview API

**Files:**
- Create: `masakari/api/openstack/ha/admin_overview.py`
- Create: `masakari/policies/admin_overview.py`
- Modify: `masakari/policies/__init__.py`
- Modify: `setup.cfg`
- Test: `masakari/tests/unit/api/openstack/ha/test_admin_overview.py`

- [x] Write failing controller and route tests for `GET /v1/admin-overview` and `GET /v1/admin-overview/health`.
- [x] Add controller returning API status, staged recovery enabled flag, and simple health check summary.
- [x] Add policy rules and extension entry point.
- [x] Run targeted admin overview tests.

### Task 2: Admin Config API

**Files:**
- Create: `masakari/api/openstack/ha/admin_config.py`
- Create: `masakari/policies/admin_config.py`
- Modify: `masakari/policies/__init__.py`
- Modify: `setup.cfg`
- Test: `masakari/tests/unit/api/openstack/ha/test_admin_config.py`

- [x] Write failing tests for `GET /v1/admin-config/schema` and `GET /v1/admin-config/effective`.
- [x] Return staged recovery config schema from registered oslo.config opts.
- [x] Return effective staged recovery values with secret-like options masked.
- [x] Run targeted admin config tests.

### Task 3: Recovery Workflows API

**Files:**
- Create: `masakari/api/openstack/ha/recovery_workflows.py`
- Create: `masakari/policies/recovery_workflows.py`
- Modify: `masakari/policies/__init__.py`
- Modify: `setup.cfg`
- Test: `masakari/tests/unit/api/openstack/ha/test_recovery_workflows.py`

- [x] Write failing tests for `GET /v1/admin-recovery-workflows/schema` and `GET /v1/admin-recovery-workflows/effective`.
- [x] Return known built-in task entry points and staged recovery workflow task ordering.
- [x] Add policy rules and extension entry point.
- [x] Run targeted recovery workflow tests.

### Task 4: Diagnostics and Audit API

**Files:**
- Create: `masakari/api/openstack/ha/diagnostics.py`
- Create: `masakari/api/openstack/ha/audit.py`
- Create: `masakari/policies/diagnostics.py`
- Create: `masakari/policies/audit.py`
- Modify: `masakari/policies/__init__.py`
- Modify: `setup.cfg`
- Test: `masakari/tests/unit/api/openstack/ha/test_diagnostics.py`
- Test: `masakari/tests/unit/api/openstack/ha/test_audit.py`

- [x] Write failing tests for diagnostics checks, diagnostics run, diagnostics job lookup, and audit event list.
- [x] Implement in-memory request-shaped responses only; no long-running backend job yet.
- [x] Add policy rules and extension entry points.
- [x] Run targeted diagnostics and audit tests.

### Task 5: Verification

**Files:**
- All files touched above.

- [x] Run all new API tests.
- [x] Run the existing staged recovery API tests to verify no regression.
- [x] Run the full test suite if targeted tests pass.
- [x] Review `git diff` for unrelated changes.
