# AGENTS.md

## Project context

This repository is a fork of OpenStack Masakari, target branch `stable/2025.1` / OpenStack Epoxy 2025.1.

The goal is to implement staged VM recovery for host failure:
- use custom Masakari TaskFlow recovery tasks;
- evacuate instances with Nova compute API microversion 2.95+ so evacuated instances remain stopped;
- store staged recovery state in etcd;
- implement per-destination-host start limiting using etcd TTL leases;
- guarantee idempotency and safe resume after masakari-engine failure;
- do not introduce Mistral;
- preserve existing Masakari behavior unless explicitly changed.

Primary specification:
- `docs/specs/CODEX_MASAKARI_STAGED_RECOVERY_ETCD.md`

## Implementation rules

- Prefer small, reviewable commits.
- Do not rewrite unrelated Masakari code.
- Do not change global `NOVA_API_VERSION` behavior for existing Masakari calls unless unavoidable.
- Add a separate Nova helper for evacuation with microversion 2.95.
- Do not use in-memory locks for HA-critical state.
- etcd state operations must be idempotent.
- Start slots must have TTL/lease semantics.
- A start slot must remain occupied until the VM reaches ACTIVE, ERROR, or timeout.
- All new config options must have sane defaults and documentation.
- Add unit tests for idempotency, etcd lease handling, task flow behavior, and Nova microversion usage.
- Before finalizing, run targeted unit tests if the environment allows it.

## Preferred workflow

1. Inspect the current Masakari code first.
2. Produce a concise implementation plan.
3. Implement one phase at a time.
4. After each phase, show the changed files and explain the reasoning.
5. Do not push to remote unless explicitly instructed.
