# ТЗ для Codex: Horizon-плагин Masakari HA и доработки REST API Masakari

**Целевая база:** fork OpenStack Masakari, ветка `stable/2025.1` / OpenStack Epoxy 2025.1.
**Цель:** реализовать operator UI в Horizon для управления Masakari HA, включая просмотр состояния HA, управление failover segments/hosts, просмотр notifications/vmoves, staged recovery, настройку параметров Masakari из UI и безопасный workflow применения конфигурации.
**Ключевой принцип:** Horizon не должен напрямую обращаться к etcd, файлам `/etc/masakari/*.conf`, systemd, контейнерам или SSH. Все операции должны идти через Masakari REST API.

---

## 0. Источники и исходные предпосылки

Codex должен учитывать следующие факты по текущей архитектуре Masakari/Horizon:

1. Штатный Masakari API уже предоставляет управление:
   - failover segments;
   - hosts внутри segments;
   - notifications;
   - vmoves.

2. Штатный Masakari API **не предоставляет** endpoint для полного редактирования `masakari.conf` и `masakari-custom-recovery-methods.conf`.

3. Masakari 2025.1 поддерживает кастомные recovery tasks через entry point `masakari.task_flow.tasks` и конфигурацию `masakari-custom-recovery-methods.conf`.

4. Штатный host failure workflow Masakari использует `EvacuateInstancesTask`, который выполняет evacuation и подтверждение эвакуации в рамках одного workflow. Для staged recovery нужно заменить этот этап на:
   - `reconcile_staged_recovery_task`;
   - `evacuate_to_stopped_task`;
   - `batched_start_instances_task`.

5. Nova Compute API microversion `2.95+` требуется для режима, при котором эвакуированная ВМ остаётся остановленной на destination host до ручного старта.

6. Для защиты от boot storm нужен глобальный per-destination-host limiter, общий для нескольких `masakari-engine`.

7. В качестве state storage для staged recovery и start leases используется etcd.

8. Horizon plugin должен быть отдельным UI-клиентом Masakari Admin API, а не самостоятельным HA-контроллером.

### Нормативные ссылки для Codex

- Masakari API reference: https://docs.openstack.org/api-ref/instance-ha/
- Masakari 2025.1 configuration options: https://docs.openstack.org/masakari/2025.1/configuration/config.html
- Masakari 2025.1 custom recovery workflow guide: https://docs.openstack.org/masakari/2025.1/configuration/recovery_workflow_custom_task.html
- Masakari 2025.1 recovery workflow sample config: https://docs.openstack.org/masakari/2025.1/configuration/recovery_workflow_sample_config.html
- Horizon pluggable panels: https://docs.openstack.org/horizon/latest/configuration/pluggable_panels.html
- Horizon plugin tutorial: https://docs.openstack.org/horizon/latest/contributor/tutorials/plugin.html
- Nova Compute API reference, evacuate microversion 2.95: https://docs.openstack.org/api-ref/compute/
- Masakari dashboard source: https://opendev.org/openstack/masakari-dashboard
- Horizon source: https://opendev.org/openstack/horizon

### Horizon/Masakari dashboard repository strategy

For Masakari API spec cleanup and backend implementation planning, a Horizon
fork is not required. The required contract can be defined from the Masakari
API side first.

Before implementing the Horizon UI, Codex should inspect a read-only checkout
of `openstack/masakari-dashboard` to verify existing panel structure, API
client patterns, policy files, enabled panel registration and test style. A
fork of `masakari-dashboard` is required when UI changes start. A full Horizon
fork is only required if the plugin needs changes in Horizon core; the expected
path is to keep this work inside the plugin.

---

## 1. Основная цель разработки

Нужно реализовать в fork Masakari и отдельном Horizon plugin следующий функциональный набор:

```text
Horizon Masakari HA UI
        ↓
Masakari REST API / Admin API
        ↓
Masakari service layer
        ↓
Masakari DB + etcd + Nova API
        ↓
masakari-engine / masakari-api / masakari-monitors
```

Система должна позволять оператору из UI:

1. Смотреть состояние Masakari HA.
2. Управлять failover segments и hosts.
3. Смотреть notifications и vmoves.
4. Смотреть staged recovery state из etcd.
5. Настраивать staged recovery параметры.
6. Настраивать параметры Masakari через typed UI, а не raw text editor.
7. Создавать черновик конфигурации.
8. Валидировать черновик.
9. Видеть diff и impact analysis.
10. Применять только runtime-safe конфигурацию через controlled apply workflow.
11. Смотреть аудит изменений.
12. Смотреть диагностику Masakari/Nova/etcd/coordination.

Текущий backend scope для этого fork-а:

- `masakari.conf` и `masakari-custom-recovery-methods.conf` immutable для
  Masakari API;
- Admin Config API применяет только runtime-safe override
  `staged_recovery.max_parallel_starts_per_host` через etcd;
- `rolling`, `canary`, deployment/reconfigure backend и rollback API не входят
  в Masakari backend MVP;
- Horizon plugin должен показывать non-runtime параметры как read-only/staged и
  блокировать apply для draft-ов с `apply_supported=false`.

---

## 2. Необходимое разграничение зон ответственности

### 2.1 Horizon plugin

**Ответственность Horizon plugin:**

- отображать UI;
- выполнять формы, таблицы, workflow и actions;
- вызывать только Masakari REST API;
- выполнять Horizon policy checks для скрытия/доступности actions;
- не хранить HA state;
- не хранить конфигурацию Masakari как source of truth;
- не обращаться напрямую к etcd;
- не обращаться напрямую к Nova для HA-sensitive операций;
- не перезапускать сервисы;
- не писать `/etc/masakari/masakari.conf`;
- не писать `masakari-custom-recovery-methods.conf`;
- не показывать secret values.

**Horizon plugin может:**

- кэшировать UI-only данные на время HTTP request;
- хранить локальные static files;
- иметь клиентские JS-компоненты для diff/visualization;
- отображать masked secret status: `configured`, `not_configured`, `changed`, `unchanged`.

**Horizon plugin не должен:**

```text
BAD:
Horizon → etcd
Horizon → SSH to controller
Horizon → systemctl restart masakari-engine
Horizon → write /etc/masakari/masakari.conf
Horizon → Nova evacuation/start directly
```

**Правильная модель:**

```text
GOOD:
Horizon → Masakari Admin REST API → backend logic
```

---

### 2.2 Masakari REST API

**Ответственность Masakari REST API:**

- быть единственной точкой входа для Horizon plugin;
- предоставлять typed Admin API для конфигурации Masakari;
- предоставлять API для staged recovery state/slots;
- предоставлять API для diagnostics;
- предоставлять API для audit trail;
- выполнять policy/RBAC checks;
- валидировать входные данные;
- скрывать secrets;
- возвращать schema-driven metadata для UI;
- создавать config drafts;
- строить config diff;
- строить apply plan;
- запускать apply job;
- возвращать состояние apply job;
- не выполнять долгие операции синхронно в HTTP request;
- не держать long-running locks в API worker;
- обеспечивать idempotency для повторных запросов;
- возвращать понятные ошибки.

**Masakari REST API не должен:**

- напрямую редактировать файлы на всех нодах внутри HTTP request;
- напрямую выполнять `systemctl restart` внутри HTTP request;
- обходить deployment/backend abstraction;
- возвращать secret values в response;
- предоставлять endpoint произвольного удаления etcd keys;
- позволять Horizon редактировать arbitrary Python task list без allowlist/validation;
- менять штатное поведение существующих API, если новые endpoints не используются.

---

### 2.3 Masakari service layer

**Ответственность service layer:**

- инкапсулировать бизнес-логику Admin Config API;
- инкапсулировать работу с etcd;
- инкапсулировать чтение/построение config schema;
- инкапсулировать validation;
- инкапсулировать apply plan;
- инкапсулировать audit events;
- давать контроллерам API простые методы.

Пример сервисов:

```text
masakari/ha/admin_config_api.py
masakari/ha/staged_recovery_api.py
masakari/ha/diagnostics_api.py
masakari/ha/audit_api.py
```

---

### 2.4 masakari-engine

**Ответственность masakari-engine:**

- выполнять HA recovery workflows;
- выполнять staged recovery tasks;
- использовать etcd state store;
- использовать etcd start limiter;
- вызывать Nova 2.95 для evacuation-to-stopped;
- запускать ВМ поэтапно;
- держать start slot до `ACTIVE`, `ERROR` или timeout;
- быть idempotent/retry-safe;
- корректно работать при нескольких engine;
- выполнять reconcile незавершённых staged recoveries, если это реализуется в engine.

**masakari-engine не должен:**

- полагаться на in-memory locks для HA-critical логики;
- хранить staged recovery state только в памяти;
- стартовать ВМ, которые до сбоя были `SHUTOFF`;
- обходить Nova scheduler validation;
- дублировать ВМ на нескольких гипервизорах.

---

### 2.5 etcd

**Ответственность etcd:**

- хранить staged recovery state;
- хранить per-instance staged state;
- хранить start leases/slots;
- хранить distributed locks/leases, если применяется;
- хранить config drafts/apply jobs/audit, если выбран etcd-backed admin config store;
- обеспечивать TTL leases для автоматического освобождения слотов при смерти `masakari-engine`.

**etcd не должен:**

- быть доступен напрямую из Horizon;
- быть доступен пользователям/проектам OpenStack;
- содержать plaintext secrets, если можно использовать secret references;
- быть единственным источником без backup/restore стратегии.

---

### 2.6 Runtime apply boundary

В текущем Masakari backend API не реализует deployment/reconfigure backend.
Apply job создается только для runtime-safe изменений, которые backend может
применить без записи config files, restart/reload сервисов, SSH, systemd или
контейнерных операций.

Поддерживаемый runtime apply MVP:

```text
staged_recovery.max_parallel_starts_per_host -> etcd runtime override
```

Non-runtime параметры остаются частью schema/draft/validate/diff/plan, чтобы
Horizon мог показывать typed UI и impact, но apply для таких draft-ов должен
быть disabled. Если оператору нужно изменить `masakari.conf`, это выполняется
через существующий deployment pipeline вне Masakari API.

---

## 3. Функциональные требования

### 3.1 Overview UI/API

Оператор должен видеть:

- доступность Masakari API;
- состояние `masakari-engine`;
- состояние monitors;
- доступность Nova API;
- поддержку Nova microversion `2.95+`;
- доступность etcd/quorum;
- состояние coordination backend;
- количество active notifications;
- количество failed notifications;
- количество running notifications;
- количество VMoves;
- состояние staged recovery;
- активные start slots;
- риски и warnings.

---

### 3.2 Segments and Hosts

Использовать штатный Masakari API для:

- list/create/show/update/delete failover segments;
- list/create/show/update/delete hosts;
- фильтрации по recovery method, service type, enabled;
- отображения reserved/on_maintenance/control_attributes.

Новые API для segments/hosts не нужны, кроме aggregate health/diagnostics enrichment при необходимости.

---

### 3.3 Recoveries: notifications/vmoves

Использовать штатный Masakari API для:

- list notifications;
- show notification;
- list VMoves for notification;
- show VMove.

Добавить enrichment из staged recovery API:

- staged state per instance;
- original VM state;
- dest host;
- current lease;
- last error;
- retry count;
- owner engine;
- timestamps.

---

### 3.4 Staged Recovery

UI должен позволять:

- включать/выключать staged recovery mode;
- задавать `max_parallel_starts_per_host`;
- задавать `batch_delay`;
- задавать `start_timeout`;
- задавать `slot_lease_ttl`;
- задавать `slot_retry_interval`;
- задавать `etcd_prefix`;
- смотреть active slots;
- смотреть waiting queue;
- смотреть state по notification;
- смотреть state по instance;
- emergency release stale slot только для истёкших/подтверждённо безопасных слотов;
- запускать diagnostics staged recovery.

---

### 3.5 Configuration Management

UI должен позволять настраивать параметры Masakari через schema-driven forms:

```text
Basic mode
Expert mode
All options mode
```

Должны поддерживаться:

- `masakari.conf`;
- `masakari-custom-recovery-methods.conf`;
- staged recovery custom options;
- coordination options;
- taskflow options;
- host failure options;
- notification processing options.

Обязательно:

- typed validation;
- default values;
- help text;
- secret masking;
- deprecated marker;
- mutable/restart-required marker;
- service impact;
- diff current vs draft;
- plan before apply.

---

### 3.6 Workflow Configuration

UI должен поддерживать безопасное переключение host failure workflow:

```ini
[taskflow_driver_recovery_flows]

host_auto_failure_recovery_tasks = {
  'pre': [
    'disable_compute_service_task'
  ],
  'main': [
    'prepare_HA_enabled_instances_task',
    'reconcile_staged_recovery_task',
    'evacuate_to_stopped_task'
  ],
  'post': [
    'batched_start_instances_task'
  ]
}

host_rh_failure_recovery_tasks = {
  'pre': [
    'disable_compute_service_task'
  ],
  'main': [
    'prepare_HA_enabled_instances_task',
    'reconcile_staged_recovery_task',
    'evacuate_to_stopped_task'
  ],
  'post': [
    'batched_start_instances_task'
  ]
}
```

UI не должен давать оператору вписать произвольное имя task без проверки. Должен быть allowlist доступных task-ов из backend schema.

---

### 3.7 Audit

Аудит должен фиксировать:

- кто создал draft;
- кто изменил draft;
- кто выполнил validate;
- кто выполнил apply;
- какие параметры изменились;
- какие secrets были изменены без раскрытия значений;
- результат apply job;
- ошибки;
- emergency release slot;
- manual retry/reconcile actions.

---

### 3.8 Diagnostics

Diagnostics API/UI должен проверять:

- Masakari API reachable;
- Masakari engine services reachable/active;
- monitors status/staleness;
- Nova API reachable;
- Nova max microversion >= 2.95;
- etcd reachable;
- etcd write/read under configured prefix;
- coordination backend configured;
- custom staged recovery tasks registered;
- workflow config includes staged tasks;
- no stale RUNNING notifications older than threshold;
- no expired start leases left visible;
- all segment hosts exist in Nova service list;
- reserved hosts exist for `reserved_host`/`rh_priority` segments.

---

## 4. Нефункциональные требования

### 4.1 Безопасность

- Все новые Admin API endpoints должны быть закрыты policy rules.
- Default policy для write/apply операций: `rule:admin_api`.
- Возможна детализация ролей: `ha_viewer`, `ha_operator`, `ha_admin`.
- Secrets никогда не возвращаются в открытом виде.
- Для secrets использовать:
  - `unchanged`;
  - `set_new_secret`;
  - `secret_ref`;
  - `configured: true/false`.
- Audit обязателен для всех write operations.
- `emergency release slot` должен требовать отдельной policy.

---

### 4.2 Идемпотентность

Все write API должны поддерживать безопасный повтор запроса.

Рекомендуется поддержать `Idempotency-Key` header для операций:

- create draft;
- validate draft;
- apply draft;
- emergency release slot;
- trigger reconcile.

Если header отсутствует, API всё равно должен защищаться от очевидных duplicate operations.

---

### 4.3 Асинхронность

HTTP API не должен выполнять долгие операции синхронно.

Долгие операции:

- future long-running runtime config apply;
- diagnostics full scan;
- reconcile stale recoveries;
- health scan across nodes.

Должны возвращать job id:

```http
202 Accepted
```

и далее:

```http
GET /v1/admin-config-apply-jobs/{job_id}
GET /v1/admin-diagnostics/jobs/{job_id}
```

---

### 4.4 Совместимость

- Existing Masakari public API не должен ломаться.
- Existing endpoints `/segments`, `/segments/{id}/hosts`, `/notifications`, `/notifications/{id}/vmoves` остаются совместимыми.
- Новые Admin API endpoints лучше добавлять как новые API extensions.
- Если меняются response fields существующих endpoints, использовать безопасное добавление и/или API version support.
- Horizon plugin должен корректно работать, если новые endpoints недоступны: показывать понятную ошибку compatibility check.

---

### 4.5 Надёжность при нескольких `masakari-engine`

- Staged recovery state хранится в etcd.
- Start slots используют TTL leases.
- Per-destination-host limiter глобальный.
- Engine death не должен навсегда блокировать slot.
- Retry не должен вызывать duplicate evacuate, если ВМ уже эвакуирована.
- Retry не должен стартовать ВМ, которая уже `ACTIVE`.
- ВМ с original state `SHUTOFF` не стартуются автоматически.

---

## 5. Новые REST API: общий дизайн

Новые endpoint-ы должны добавляться так, чтобы они соответствовали текущему
extension-механизму Masakari API и при этом оставались удобными для Horizon.

В текущем fork Masakari стандартные `ResourceExtension` маршрутизируются как
top-level collections под `/v1/{collection}` и `/v1/{project_id}/{collection}`.
Поэтому рекомендуемый MVP должен использовать extension-style namespace:

```text
/v1/admin-overview
/v1/admin-config/...
/v1/admin-config-drafts/...
/v1/admin-config-apply-jobs/...
/v1/admin-recovery-workflows/...
/v1/staged-recovery/...
/v1/admin-diagnostics/...
/v1/admin-audit/...
```

Существующий `staged-recovery` extension уже реализован и должен
расширяться/переиспользоваться, а не дублироваться новым admin endpoint-ом.

Если Horizon plugin или внешний контракт принципиально требует единый
`/v1/admin/...` namespace, это нужно реализовывать отдельным `admin` extension
с `custom_routes_fn`, который делегирует в те же service classes. Такой путь
считается compatibility layer и не должен дублировать бизнес-логику.

Для сложных nested operations (`drafts/{id}/validate`,
`drafts/{id}/apply` и т.п.) допустимы два варианта:

```text
1. отдельные top-level resources;
2. ResourceExtension с custom_routes_fn.
```

В этом документе endpoint examples используют рекомендуемый MVP namespace.

---

## 6. REST API: Overview / Health

### 6.1 GET `/v1/admin-overview`

**Назначение:** агрегированный snapshot для главного экрана Horizon.

**Policy:** `os_masakari_api:admin-overview:index`

**Response example:**

```json
{
  "overview": {
    "generated_at": "2026-06-10T12:45:00Z",
    "masakari_api": {
      "status": "ok",
      "version": "1.0"
    },
    "masakari_engine": {
      "status": "ok",
      "active_workers": 3,
      "expected_workers": 3
    },
    "monitors": {
      "status": "warn",
      "stale_monitors": 1
    },
    "nova": {
      "status": "ok",
      "max_microversion": "2.100",
      "evacuate_to_stopped_supported": true
    },
    "etcd": {
      "status": "ok",
      "quorum": "3/3",
      "prefix": "/masakari/staged-recovery"
    },
    "notifications": {
      "running": 2,
      "new": 0,
      "failed": 1,
      "finished_last_24h": 12
    },
    "staged_recovery": {
      "enabled": true,
      "workflow_active": true,
      "max_parallel_starts_per_host": 2,
      "active_slots": 4,
      "waiting_instances": 11
    },
    "risks": [
      {
        "severity": "warning",
        "code": "MONITOR_STALE",
        "message": "One hostmonitor has not reported recently."
      }
    ]
  }
}
```

---

### 6.2 GET `/v1/admin-overview/health`

**Назначение:** machine-readable health endpoint для UI и external monitoring.

**Policy:** `os_masakari_api:admin-overview:health`

**Response fields:**

```text
status: ok | warn | error
checks[]:
  name
  status
  message
  details
  duration_ms
```

---

## 7. REST API: Configuration Schema

### 7.1 GET `/v1/admin-config/schema`

**Назначение:** вернуть schema всех управляемых параметров Masakari для UI.

**Policy:** `os_masakari_api:admin-config:schema`

**Query params:**

```text
service=masakari-api|masakari-engine|masakari-monitors|all
file=masakari.conf|masakari-custom-recovery-methods.conf|all
mode=basic|expert|all
include_deprecated=true|false
```

**Response example:**

```json
{
  "schema": {
    "version": "2025.1-1",
    "services": ["masakari-api", "masakari-engine", "masakari-monitors"],
    "groups": [
      {
        "name": "host_failure",
        "title": "Host failure recovery",
        "options": [
          {
            "name": "recovery_threads",
            "full_name": "host_failure.recovery_threads",
            "type": "integer",
            "default": 3,
            "min": 1,
            "required": false,
            "secret": false,
            "mutable": false,
            "restart_required": true,
            "services": ["masakari-engine"],
            "risk": "medium",
            "ui_mode": "basic",
            "help": "Number of threads used for host failure recovery."
          }
        ]
      },
      {
        "name": "staged_recovery",
        "title": "Staged recovery",
        "options": [
          {
            "name": "max_parallel_starts_per_host",
            "full_name": "staged_recovery.max_parallel_starts_per_host",
            "type": "integer",
            "default": 2,
            "min": 1,
            "secret": false,
            "mutable": false,
            "restart_required": true,
            "services": ["masakari-engine"],
            "risk": "medium",
            "ui_mode": "basic",
            "help": "Maximum simultaneously starting instances per destination host."
          }
        ]
      }
    ]
  }
}
```

**Implementation requirements:**

- Schema must be generated from registered oslo.config opts where possible.
- Add manual metadata registry for:
  - restart impact;
  - UI grouping;
  - risk;
  - secret behavior;
  - service applicability;
  - allowed values;
  - option dependencies.
- Do not return plaintext defaults for secret options if defaults contain sensitive values.

---

## 8. REST API: Effective Configuration

### 8.1 GET `/v1/admin-config/effective`

**Назначение:** показать текущую эффективную конфигурацию, видимую Masakari API/backend.

**Policy:** `os_masakari_api:admin-config:effective`

**Query params:**

```text
service=masakari-api|masakari-engine|masakari-monitors|all
host=<controller-hostname>|all
file=masakari.conf|masakari-custom-recovery-methods.conf|all
mask_secrets=true|false
```

`mask_secrets=false` должен игнорироваться или быть запрещён, если нет отдельной super-admin policy. MVP: всегда mask secrets.

**Response example:**

```json
{
  "effective_config": {
    "generated_at": "2026-06-10T12:45:00Z",
    "service": "masakari-engine",
    "host": "controller-1",
    "groups": {
      "staged_recovery": {
        "enabled": {
          "value": true,
          "source": "config-file",
          "secret": false
        },
        "max_parallel_starts_per_host": {
          "value": 2,
          "source": "config-file",
          "secret": false
        }
      },
      "keystone_authtoken": {
        "password": {
          "value": "********",
          "configured": true,
          "source": "config-file",
          "secret": true
        }
      }
    }
  }
}
```

---

## 9. REST API: Config Drafts

### 9.1 POST `/v1/admin-config-drafts`

**Назначение:** создать черновик изменения конфигурации.

**Policy:** `os_masakari_api:admin-config-drafts:create`

**Request example:**

```json
{
  "draft": {
    "name": "enable-staged-recovery",
    "description": "Enable staged recovery with etcd start limiter",
    "base_version": "active",
    "changes": [
      {
        "file": "masakari.conf",
        "group": "staged_recovery",
        "option": "enabled",
        "value": true
      },
      {
        "file": "masakari.conf",
        "group": "staged_recovery",
        "option": "max_parallel_starts_per_host",
        "value": 2
      },
      {
        "file": "masakari.conf",
        "group": "coordination",
        "option": "backend_url",
        "value": "etcd3+http://etcd-1:2379,etcd-2:2379,etcd-3:2379"
      }
    ]
  }
}
```

**Response:**

```json
{
  "draft": {
    "id": "draft-2026-06-10-001",
    "status": "created",
    "created_at": "2026-06-10T12:45:00Z",
    "created_by": "admin",
    "links": [
      {"rel": "self", "href": "/v1/admin-config-drafts/draft-2026-06-10-001"}
    ]
  }
}
```

---

### 9.2 GET `/v1/admin-config-drafts`

**Policy:** `os_masakari_api:admin-config-drafts:index`

Supports filters:

```text
status=created|validated|approved|applying|applied|failed|discarded
created_by=<user>
limit=<n>
marker=<id>
```

---

### 9.3 GET `/v1/admin-config-drafts/{draft_id}`

**Policy:** `os_masakari_api:admin-config-drafts:detail`

Return full draft, masked secrets, validation results, diff summary if available.

---

### 9.4 PATCH `/v1/admin-config-drafts/{draft_id}`

**Назначение:** обновить draft до apply.

**Policy:** `os_masakari_api:admin-config-drafts:update`

Allowed only statuses:

```text
created
validation_failed
validated
```

Not allowed after:

```text
applying
applied
```

---

### 9.5 DELETE `/v1/admin-config-drafts/{draft_id}`

**Назначение:** discard draft.

**Policy:** `os_masakari_api:admin-config-drafts:delete`

Allowed only if not applying/applied.

---

## 10. REST API: Validate / Plan / Diff

### 10.1 POST `/v1/admin-config-drafts/{draft_id}/validate`

**Policy:** `os_masakari_api:admin-config-drafts:validate`

**Validation must check:**

- option exists;
- type correct;
- min/max constraints;
- choices;
- dependencies;
- unknown group/option rejected;
- secrets are not returned;
- workflow task names are registered/allowed;
- staged recovery requires etcd settings;
- `max_parallel_starts_per_host >= 1`;
- `slot_lease_ttl >= start_timeout` or warning if not;
- Nova microversion 2.95 support check if possible;
- active recovery conflict warning;
- restart impact.

**Response example:**

```json
{
  "validation": {
    "draft_id": "draft-2026-06-10-001",
    "status": "passed_with_warnings",
    "errors": [],
    "warnings": [
      {
        "code": "ENGINE_RESTART_REQUIRED",
        "message": "masakari-engine restart is required."
      }
    ],
    "checked_at": "2026-06-10T12:46:00Z"
  }
}
```

---

### 10.2 POST `/v1/admin-config-drafts/{draft_id}/plan`

**Policy:** `os_masakari_api:admin-config-drafts:plan`

**Response example:**

```json
{
  "plan": {
    "draft_id": "draft-2026-06-10-001",
    "status": "planned",
    "steps": [
      {
        "group": "staged_recovery",
        "option": "max_parallel_starts_per_host",
        "action": "runtime_update"
      },
      {
        "group": "staged_recovery",
        "option": "batch_delay",
        "action": "reconfigure_required"
      }
    ],
    "apply_supported": false,
    "diff": [
      {
        "file": "masakari.conf",
        "group": "staged_recovery",
        "option": "max_parallel_starts_per_host",
        "current": 3,
        "draft": 2,
        "secret": false,
        "risk": "medium"
      }
    ],
    "prechecks": [
      {
        "name": "no_active_recovery",
        "status": "ok"
      }
    ]
  }
}
```

---

### 10.3 GET `/v1/admin-config-drafts/{draft_id}/diff`

**Policy:** `os_masakari_api:admin-config-drafts:diff`

Return structured diff for UI. Do not return secrets.

---

## 11. REST API: Runtime Apply

### 11.1 POST `/v1/admin-config-drafts/{draft_id}/apply`

**Назначение:** применить runtime-safe draft.

**Policy:** `os_masakari_api:admin-config-drafts:apply`

Backend принимает apply только для draft-а, plan которого содержит только
`runtime_update` steps. Draft с `reconfigure_required` steps должен вернуть
`409 Conflict` без создания apply job.

**Request example:**

```json
{
  "apply": {
    "comment": "Change staged recovery start limiter"
  }
}
```

`strategy`, `canary` и `allow_apply_with_active_recoveries` не являются
управляющими параметрами текущего Masakari backend API. Новые apply jobs всегда
создаются как `strategy=runtime`, `canary=false`.

**Response:**

```json
{
  "apply_job": {
    "id": "apply-2026-06-10-001",
    "draft_id": "draft-2026-06-10-001",
    "status": "succeeded",
    "strategy": "runtime",
    "canary": false,
    "result": {
      "status": "succeeded",
      "backend": "runtime_etcd",
      "changed": true
    },
    "created_at": "2026-06-10T12:47:00Z",
    "links": [
      {"rel": "self", "href": "/v1/admin-config-apply-jobs/apply-2026-06-10-001"}
    ]
  }
}
```

**HTTP status:** `202 Accepted`

Для текущего `runtime_etcd` backend job завершается в рамках HTTP request.
Apply job все равно сохраняется, чтобы Horizon мог показать результат и ошибки.

---

### 11.2 GET `/v1/admin-config-apply-jobs`

**Policy:** `os_masakari_api:admin-config-apply-jobs:index`

Filters:

```text
status=queued|succeeded|failed
limit
marker
```

---

### 11.3 GET `/v1/admin-config-apply-jobs/{job_id}`

**Policy:** `os_masakari_api:admin-config-apply-jobs:detail`

**Response example:**

```json
{
  "apply_job": {
    "id": "apply-2026-06-10-001",
    "draft_id": "draft-2026-06-10-001",
    "status": "succeeded",
    "strategy": "runtime",
    "canary": false,
    "result": {
      "status": "succeeded",
      "backend": "runtime_etcd",
      "changed": true
    },
    "errors": []
  }
}
```

---

Rollback endpoint намеренно не входит в текущий контракт Masakari backend.
Для `max_parallel_starts_per_host` возврат старого значения выполняется через
новый draft с предыдущим значением и обычный runtime apply.

---

## 12. REST API: Workflow Configuration

### 12.1 GET `/v1/admin-recovery-workflows/schema`

**Назначение:** вернуть список доступных task-ов, workflow templates и allowed modes.

**Policy:** `os_masakari_api:admin-recovery-workflows:schema`

**Response example:**

```json
{
  "schema": {
    "available_tasks": [
      {
        "name": "disable_compute_service_task",
        "entry_point": "masakari.engine.drivers.taskflow.host_failure:DisableComputeServiceTask",
        "builtin": true,
        "allowed": true
      },
      {
        "name": "reconcile_staged_recovery_task",
        "entry_point": "masakari.engine.drivers.taskflow.staged_host_failure:ReconcileStagedRecoveryTask",
        "builtin": false,
        "allowed": true
      },
      {
        "name": "evacuate_to_stopped_task",
        "entry_point": "masakari.engine.drivers.taskflow.staged_host_failure:EvacuateToStoppedTask",
        "builtin": false,
        "allowed": true
      },
      {
        "name": "batched_start_instances_task",
        "entry_point": "masakari.engine.drivers.taskflow.staged_host_failure:BatchedStartInstancesTask",
        "builtin": false,
        "allowed": true
      }
    ],
    "templates": [
      {
        "name": "standard",
        "description": "Default Masakari workflow"
      },
      {
        "name": "staged_recovery_etcd",
        "description": "Evacuate to stopped, then batched start with etcd limiter"
      }
    ]
  }
}
```

---

### 12.2 GET `/v1/admin-recovery-workflows/effective`

Return current effective workflow config.

---

### 12.3 PATCH `/v1/admin-config-drafts/{draft_id}/recovery-workflows`

Update workflow config inside a draft, not live config.

**Validation requirements:**

- only allowed tasks;
- required tasks order;
- no duplicate incompatible tasks;
- `batched_start_instances_task` must not appear before `evacuate_to_stopped_task`;
- staged workflow requires `staged_recovery.enabled=true`;
- if `reserved_host` flow is configured, retry semantics must be preserved.

---

## 13. REST API: Staged Recovery State

### 13.1 GET `/v1/staged-recovery/instances`

**Policy:** `os_masakari_api:staged-recovery:instances:index`

**Query params:**

```text
notification_uuid=<uuid>
instance_uuid=<uuid>
source_host=<host>
dest_host=<host>
step=DISCOVERED|EVACUATING|EVACUATED_STOPPED|WAITING_START_SLOT|STARTING|ACTIVE|FAILED|IGNORED
limit=<n>
```

**Response example:**

```json
{
  "instances": [
    {
      "notification_uuid": "32bc95ac-0000-0000-0000-000000000000",
      "instance_uuid": "vm-db-01-uuid",
      "instance_name": "vm-db-01",
      "source_host": "compute-02",
      "dest_host": "compute-11",
      "original_vm_state": "active",
      "current_nova_status": "SHUTOFF",
      "step": "WAITING_START_SLOT",
      "attempt": 1,
      "lease": null,
      "last_error": null,
      "updated_at": "2026-06-10T12:49:00Z"
    }
  ]
}
```

---

### 13.2 GET `/v1/staged-recovery/instances?notification_uuid={notification_uuid}`

Return staged instance state for one notification using the existing list API.
For MVP Horizon should compute per-notification summary client-side from the
`instances` list. A dedicated notification summary endpoint can be added later
if the UI or API payload size requires it.

**Response:**

```json
{
  "instances": []
}
```

---

### 13.3 GET `/v1/staged-recovery/leases`

**Policy:** `os_masakari_api:staged-recovery:leases:index`

**Query params:**

```text
dest_host=<host>
notification_uuid=<uuid>
instance_uuid=<uuid>
limit=<n>
```

**Response example:**

```json
{
  "leases": [
    {
      "dest_host": "compute-10",
      "instance_uuid": "vm-db-01-uuid",
      "notification_uuid": "32bc95ac-0000-0000-0000-000000000000",
      "owner": "masakari-engine@controller-1",
      "lease_id": "758788342343",
      "lease_until": "2026-06-10T12:51:02Z",
      "expired": false,
      "state": "STARTING"
    }
  ],
  "limits": [
    {
      "dest_host": "compute-10",
      "used": 2,
      "limit": 2,
      "waiting": 8
    }
  ]
}
```

---

### 13.4 POST `/v1/staged-recovery/leases/{slot_id}/release`

**Status:** future extension, not MVP.

**Назначение:** emergency release stale slot.

**Policy:** `os_masakari_api:staged-recovery:leases:release`

**Constraints:**

- allow only expired slot by default;
- non-expired slot release requires `force=true` and stronger policy;
- always audit;
- check Nova current state before release if possible.

**Request:**

```json
{
  "release": {
    "force": false,
    "reason": "Lease expired after masakari-engine crash"
  }
}
```

---

### 13.5 POST `/v1/staged-recovery/reconcile`

**Status:** future extension, not MVP.

**Назначение:** запустить reconcile job для staged recovery.

**Policy:** `os_masakari_api:staged-recovery:reconcile`

**Request:**

```json
{
  "reconcile": {
    "notification_uuid": "32bc95ac-0000-0000-0000-000000000000",
    "dry_run": false
  }
}
```

**Response:** `202 Accepted` with job id.

---

## 14. REST API: Diagnostics

### 14.1 GET `/v1/admin-diagnostics/checks`

Return available diagnostics checks.

---

### 14.2 POST `/v1/admin-diagnostics/run`

**Policy:** `os_masakari_api:diagnostics:run`

**Request:**

```json
{
  "diagnostics": {
    "scope": "full",
    "checks": [
      "nova_microversion",
      "etcd_health",
      "coordination_backend",
      "workflow_tasks",
      "staged_recovery_slots",
      "segments_hosts"
    ]
  }
}
```

**Response:** `202 Accepted` with diagnostics job id.

---

### 14.3 GET `/v1/admin-diagnostics/jobs/{job_id}`

Return status/result.

---

## 15. REST API: Audit

### 15.1 GET `/v1/admin-audit/events`

**Policy:** `os_masakari_api:audit:index`

Filters:

```text
actor=<user_id>
action=<action>
resource_type=<type>
resource_id=<id>
since=<timestamp>
until=<timestamp>
limit=<n>
marker=<cursor>
```

**Response example:**

```json
{
  "events": [
    {
      "id": "audit-001",
      "time": "2026-06-10T12:47:00Z",
      "actor": "admin",
      "action": "config.draft.apply",
      "resource_type": "config_draft",
      "resource_id": "draft-2026-06-10-001",
      "request_id": "req-abc",
      "outcome": "success",
      "details": {
        "changed_options": 4,
        "secrets_changed": 0
      }
    }
  ]
}
```

---

## 16. REST API: RBAC / Policies

Codex должен добавить недостающие policy modules и расширить уже существующие,
если endpoint уже реализован.

### 16.1 Новые policy files/classes

```text
masakari/policies/admin_overview.py
masakari/policies/admin_config.py
masakari/policies/recovery_workflows.py
masakari/policies/diagnostics.py
masakari/policies/audit.py
```

Обновить/расширить:

```text
masakari/policies/__init__.py
masakari/policies/staged_recovery.py
```

### 16.2 Suggested policy names

```text
os_masakari_api:admin-overview:index
os_masakari_api:admin-overview:health

os_masakari_api:admin-config:schema
os_masakari_api:admin-config:effective

os_masakari_api:admin-config-drafts:index
os_masakari_api:admin-config-drafts:detail
os_masakari_api:admin-config-drafts:create
os_masakari_api:admin-config-drafts:update
os_masakari_api:admin-config-drafts:delete
os_masakari_api:admin-config-drafts:validate
os_masakari_api:admin-config-drafts:plan
os_masakari_api:admin-config-drafts:diff
os_masakari_api:admin-config-drafts:apply

os_masakari_api:admin-config-apply-jobs:index
os_masakari_api:admin-config-apply-jobs:detail

os_masakari_api:admin-recovery-workflows:schema
os_masakari_api:admin-recovery-workflows:effective
os_masakari_api:admin-recovery-workflows:update

os_masakari_api:staged-recovery:start_limit:show
os_masakari_api:staged-recovery:start_limit:update
os_masakari_api:staged-recovery:start_limit:delete
os_masakari_api:staged-recovery:instances:index
os_masakari_api:staged-recovery:leases:index

Future only, after safety rules and audit are implemented:
os_masakari_api:staged-recovery:leases:release
os_masakari_api:staged-recovery:leases:force_release
os_masakari_api:staged-recovery:reconcile

os_masakari_api:diagnostics:index
os_masakari_api:diagnostics:run
os_masakari_api:diagnostics:detail

os_masakari_api:audit:index
os_masakari_api:audit:detail
```

### 16.3 Default policy mapping

MVP:

```text
read-only admin endpoints: rule:admin_api
write/apply endpoints: rule:admin_api
force release: rule:admin_api
```

Optional role mapping:

```text
ha_viewer:
  overview, health, config schema/effective masked, staged state, diagnostics results, audit read

ha_operator:
  segments/hosts, config draft create/update/validate, diagnostics run, reconcile dry-run

ha_admin:
  apply, recovery workflow update, slot release, force release
```

---

## 17. Backend storage model in etcd

### 17.1 Staged recovery keys

Use configurable prefix:

```text
staged_recovery.etcd_prefix = /masakari/staged-recovery/v1
```

Keys:

```text
/masakari/staged-recovery/v1/notifications/{notification_uuid}/instances/{instance_uuid}
/masakari/staged-recovery/v1/start-leases/{dest_host}/{instance_uuid}
/masakari/staged-recovery/v1/config/max_parallel_starts_per_host
```

This matches the current `EtcdStagedRecoveryStore` key model. Do not introduce
parallel `/instances`, `/slots` or `/locks` trees unless a migration plan is
added.

Instance state example:

```json
{
  "notification_uuid": "32bc95ac-0000-0000-0000-000000000000",
  "instance_uuid": "vm-db-01-uuid",
  "instance_name": "vm-db-01",
  "source_host": "compute-02",
  "dest_host": "compute-11",
  "original_vm_state": "active",
  "original_task_state": null,
  "original_power_state": 1,
  "step": "EVACUATED_STOPPED",
  "attempt": 1,
  "last_error": null,
  "created_at": "2026-06-10T12:41:02Z",
  "updated_at": "2026-06-10T12:45:02Z"
}
```

### 17.2 Start slot key

```text
/masakari/staged-recovery/v1/start-leases/{dest_host}/{instance_uuid}
```

Value:

```json
{
  "dest_host": "compute-10",
  "instance_uuid": "vm-db-01-uuid",
  "notification_uuid": "32bc95ac-0000-0000-0000-000000000000",
  "owner": "masakari-engine@controller-1",
  "lease_id": "758788342343",
  "state": "STARTING",
  "created_at": "2026-06-10T12:46:00Z",
  "updated_at": "2026-06-10T12:46:00Z"
}
```

The key must be attached to etcd TTL lease.

---

## 18. Backend storage model for admin config

MVP can store admin config drafts/jobs/audit in etcd. If project prefers SQL DB, implement repository abstraction.

### 18.1 Repository interface

```python
class AdminConfigRepository:
    def create_draft(self, context, draft): ...
    def get_draft(self, context, draft_id): ...
    def list_drafts(self, context, filters, limit, marker): ...
    def update_draft(self, context, draft_id, patch): ...
    def delete_draft(self, context, draft_id): ...
    def create_apply_job(self, context, job): ...
    def get_apply_job(self, context, job_id): ...
    def update_apply_job(self, context, job_id, patch): ...
    def create_audit_event(self, context, event): ...
```

### 18.2 etcd keys

```text
/masakari/admin-config/active/{file}
/masakari/admin-config/drafts/{draft_id}
/masakari/admin-config/apply-jobs/{job_id}
/masakari/admin-config/audit/{timestamp}/{event_id}
/masakari/admin-config/idempotency/{idempotency_key}
/masakari/admin-config/locks/apply
```

### 18.3 Secret handling

Secrets in drafts must be represented as:

```json
{
  "secret": true,
  "mode": "unchanged"
}
```

or:

```json
{
  "secret": true,
  "mode": "set_new_secret",
  "value": "<encrypted-or-write-only-value>"
}
```

or:

```json
{
  "secret": true,
  "mode": "secret_ref",
  "secret_ref": "barbican://..."
}
```

MVP: `set_new_secret` may be accepted but never returned back. API must return only:

```json
{
  "secret": true,
  "changed": true,
  "configured": true
}
```

---

## 19. Files to change in Masakari fork

Codex should inspect actual tree first, then implement. Expected files/modules:

### 19.1 API controllers/extensions

Add:

```text
masakari/api/openstack/ha/admin_overview.py
masakari/api/openstack/ha/admin_config.py
masakari/api/openstack/ha/recovery_workflows.py
masakari/api/openstack/ha/diagnostics.py
masakari/api/openstack/ha/audit.py
```

Extend existing:

```text
masakari/api/openstack/ha/staged_recovery.py
```

### 19.2 API schemas

Add:

```text
masakari/api/openstack/ha/schemas/admin_config.py
masakari/api/openstack/ha/schemas/recovery_workflows.py
masakari/api/openstack/ha/schemas/diagnostics.py
```

Extend existing only if new staged recovery write operations are added:

```text
masakari/api/openstack/ha/schemas/staged_recovery.py
```

### 19.3 API views

Add if project style requires builders:

```text
masakari/api/openstack/ha/views/admin_config.py
masakari/api/openstack/ha/views/diagnostics.py
```

### 19.4 Service layer

Add:

```text
masakari/ha/admin_overview_api.py
masakari/ha/admin_config_api.py
masakari/ha/recovery_workflows_api.py
masakari/ha/staged_recovery_api.py
masakari/ha/diagnostics_api.py
masakari/ha/audit_api.py
```

### 19.5 Config management implementation

Add:

```text
masakari/config_admin/__init__.py
masakari/config_admin/schema.py
masakari/config_admin/metadata.py
masakari/config_admin/drafts.py
masakari/config_admin/diff.py
masakari/config_admin/validation.py
masakari/config_admin/plan.py
masakari/config_admin/apply.py
masakari/config_admin/repository.py
masakari/config_admin/etcd_repository.py
masakari/config_admin/secret_masking.py
```

### 19.6 Staged recovery etcd state

Already implemented in this fork:

```text
masakari/engine/drivers/taskflow/staged_host_failure.py
masakari/engine/drivers/taskflow/staged_state_etcd.py
```

Do not add a second staged recovery state implementation for the Horizon API.
The API layer should read through the existing `EtcdStagedRecoveryStore`.

A reusable package can be introduced later only with a migration/refactor plan:

```text
masakari/staged_recovery/state.py
masakari/staged_recovery/limiter.py
masakari/staged_recovery/etcd.py
```

### 19.7 Nova helper

Modify carefully:

```text
masakari/compute/nova.py
```

Add methods without changing existing global `NOVA_API_VERSION` behavior:

```python
evacuate_instance_to_stopped(context, uuid, target=None)
get_server_host(context, uuid)
get_server_status(context, uuid)
```

### 19.8 Policies

Add:

```text
masakari/policies/admin_overview.py
masakari/policies/admin_config.py
masakari/policies/recovery_workflows.py
masakari/policies/diagnostics.py
masakari/policies/audit.py
```

Modify/extend:

```text
masakari/policies/__init__.py
masakari/policies/staged_recovery.py
```

### 19.9 Entry points

Modify:

```text
setup.cfg
```

Add API extensions:

```ini
masakari.api.v1.extensions =
    admin_overview = masakari.api.openstack.ha.admin_overview:AdminOverview
    admin_config = masakari.api.openstack.ha.admin_config:AdminConfig
    recovery_workflows = masakari.api.openstack.ha.recovery_workflows:RecoveryWorkflows
    diagnostics = masakari.api.openstack.ha.diagnostics:Diagnostics
    audit = masakari.api.openstack.ha.audit:Audit
```

Already present in this fork; keep these task entries aligned with
`staged_host_failure`:

```ini
masakari.task_flow.tasks =
    reconcile_staged_recovery_task = masakari.engine.drivers.taskflow.staged_host_failure:ReconcileStagedRecoveryTask
    evacuate_to_stopped_task = masakari.engine.drivers.taskflow.staged_host_failure:EvacuateToStoppedTask
    batched_start_instances_task = masakari.engine.drivers.taskflow.staged_host_failure:BatchedStartInstancesTask
```

### 19.10 Config opts

Already present and may be extended only when new options are actually needed:

```text
masakari/conf/staged_recovery.py
```

Add:

```text
masakari/conf/admin_config.py
```

Modify central opts registration according to project convention:

```text
masakari/conf/__init__.py
masakari/conf/opts.py
```

### 19.11 Exceptions

Modify:

```text
masakari/exception.py
```

Add exceptions:

```text
ConfigDraftNotFound
ConfigDraftInvalidState
ConfigValidationFailed
ConfigApplyJobNotFound
ConfigApplyInProgress
StagedRecoveryStateNotFound
StagedRecoverySlotNotFound
StagedRecoverySlotNotExpired
EtcdBackendUnavailable
DiagnosticsJobNotFound
```

### 19.12 Tests

Add:

```text
masakari/tests/unit/api/openstack/ha/test_admin_overview.py
masakari/tests/unit/api/openstack/ha/test_admin_config.py
masakari/tests/unit/api/openstack/ha/test_recovery_workflows.py
masakari/tests/unit/api/openstack/ha/test_diagnostics.py
masakari/tests/unit/api/openstack/ha/test_audit.py

masakari/tests/unit/config_admin/test_schema.py
masakari/tests/unit/config_admin/test_validation.py
masakari/tests/unit/config_admin/test_diff.py
masakari/tests/unit/config_admin/test_etcd_repository.py
masakari/tests/unit/policies/test_admin_config.py
```

Extend existing:

```text
masakari/tests/unit/api/openstack/ha/test_staged_recovery.py
```

Add `masakari/tests/unit/staged_recovery/` tests only if the later reusable
`masakari/staged_recovery/` package is introduced.

---

## 20. Horizon plugin file structure

Create separate repository/package, for example:

```text
masakari-ha-dashboard/
  masakari_ha_dashboard/
    __init__.py
    enabled/
      _60_masakari_ha.py
    api/
      __init__.py
      masakari.py
      admin_config.py
      staged_recovery.py
      diagnostics.py
      audit.py
    dashboards/
      masakari_ha/
        dashboard.py
        overview/
          panel.py
          tables.py
          views.py
          urls.py
          templates/overview/index.html
        segments/
          panel.py
          tables.py
          workflows.py
          views.py
          urls.py
        recoveries/
          panel.py
          tables.py
          views.py
          urls.py
        staged_recovery/
          panel.py
          forms.py
          tables.py
          views.py
          urls.py
        config/
          panel.py
          forms.py
          tables.py
          workflows.py
          views.py
          urls.py
        apply/
          panel.py
          tables.py
          views.py
          urls.py
        audit/
          panel.py
          tables.py
          views.py
          urls.py
        diagnostics/
          panel.py
          tables.py
          views.py
          urls.py
    static/
      dashboard/admin/masakari_ha/
        masakari_ha.scss
        config_editor.js
        recovery_status.js
    locale/
  setup.cfg
  pyproject.toml
  README.rst
  AGENTS.md
```

Panels:

```text
Masakari HA
├── Overview
├── Segments and Hosts
├── Recoveries
├── Staged Recovery
├── Configuration
├── Changes and Apply
├── Audit
└── Diagnostics
```

---

## 21. Horizon UI requirements by panel

### 21.1 Overview panel

Show cards:

```text
Masakari API
Masakari Engine
Monitors
etcd
Nova API
Boot storm protection
```

Show tables:

```text
Active notifications
Risk checks
Recent recoveries
```

API calls:

```text
GET /v1/admin-overview
GET /v1/admin-overview/health
```

---

### 21.2 Segments and Hosts panel

Use existing Masakari API:

```text
GET /v1/segments
POST /v1/segments
GET /v1/segments/{segment_id}
PUT /v1/segments/{segment_id}
DELETE /v1/segments/{segment_id}

GET /v1/segments/{segment_id}/hosts
POST /v1/segments/{segment_id}/hosts
GET /v1/segments/{segment_id}/hosts/{host_id}
PUT /v1/segments/{segment_id}/hosts/{host_id}
DELETE /v1/segments/{segment_id}/hosts/{host_id}
```

Add diagnostics enrichment optionally:

```text
POST /v1/admin-diagnostics/run
```

---

### 21.3 Recoveries panel

Use existing API:

```text
GET /v1/notifications
GET /v1/notifications/{notification_id}
GET /v1/notifications/{notification_id}/vmoves
GET /v1/notifications/{notification_id}/vmoves/{vmove_id}
```

Use new API for staged details:

```text
GET /v1/staged-recovery/instances?notification_uuid={notification_uuid}
GET /v1/staged-recovery/instances?notification_uuid={uuid}
```

---

### 21.4 Staged Recovery panel

Use:

```text
GET /v1/staged-recovery/instances
GET /v1/staged-recovery/leases
GET /v1/admin-config/effective?group=staged_recovery
POST /v1/admin-config-drafts
POST /v1/admin-config-drafts/{draft_id}/validate
POST /v1/admin-config-drafts/{draft_id}/apply
```

Actions:

```text
Edit staged recovery settings
View active slots
Release expired slot
Run reconcile
Run diagnostics
```

---

### 21.5 Configuration panel

Use:

```text
GET /v1/admin-config/schema
GET /v1/admin-config/effective
POST /v1/admin-config-drafts
PATCH /v1/admin-config-drafts/{draft_id}
POST /v1/admin-config-drafts/{draft_id}/validate
POST /v1/admin-config-drafts/{draft_id}/plan
GET /v1/admin-config-drafts/{draft_id}/diff
```

Modes:

```text
Basic
Expert
All options
```

Secret behavior:

```text
never show current value
show configured/changed indicators only
```

---

### 21.6 Changes and Apply panel

Use:

```text
GET /v1/admin-config-drafts
GET /v1/admin-config-drafts/{draft_id}
POST /v1/admin-config-drafts/{draft_id}/apply
GET /v1/admin-config-apply-jobs
GET /v1/admin-config-apply-jobs/{job_id}
```

---

### 21.7 Audit panel

Use:

```text
GET /v1/admin-audit/events
```

---

### 21.8 Diagnostics panel

Use:

```text
GET /v1/admin-diagnostics/checks
POST /v1/admin-diagnostics/run
GET /v1/admin-diagnostics/jobs/{job_id}
```

---

## 22. Staged recovery settings managed by UI

Use the existing config group:

```ini
[staged_recovery]
enabled = true
state_backend = etcd
etcd_backend_url = <optional direct etcd3 gateway url>
etcd_timeout = 5
etcd_ca_cert = <optional path>
etcd_cert_file = <optional path>
etcd_key_file = <optional path>
etcd_prefix = /masakari/staged-recovery/v1
nova_evacuate_microversion = 2.95
max_parallel_starts_per_host = 2
batch_delay = 30
start_timeout = 900
slot_lease_ttl = 990
slot_retry_interval = 5
reconcile_interval = 60
stale_recovery_timeout = 300
start_only_originally_active = true
```

Validation rules:

```text
enabled: bool
state_backend: enum, currently etcd
etcd_backend_url: optional string, direct etcd3 gateway URL
etcd_timeout: int >= 1
etcd_ca_cert: optional file path
etcd_cert_file: optional file path
etcd_key_file: optional file path
etcd_prefix: non-empty absolute etcd key prefix
nova_evacuate_microversion: string, must be >= 2.95
max_parallel_starts_per_host: int >= 1
batch_delay: int >= 0
start_timeout: int >= 60
slot_lease_ttl: int >= 60
slot_retry_interval: int >= 1
reconcile_interval: int >= 10
stale_recovery_timeout: int >= 60
start_only_originally_active: bool
```

Deployment timing:

```text
Before deploy / reconfigure required:
  enabled
  state_backend
  etcd_backend_url
  etcd_timeout
  etcd_ca_cert
  etcd_cert_file
  etcd_key_file
  etcd_prefix
  nova_evacuate_microversion
  start_timeout
  batch_delay
  slot_lease_ttl
  slot_retry_interval
  reconcile_interval
  stale_recovery_timeout
  start_only_originally_active

After deploy / runtime-safe:
  max_parallel_starts_per_host
```

`max_parallel_starts_per_host` is runtime-safe because the current staged
recovery store supports an etcd-backed runtime override via
`/staged-recovery/start-limit`. Other options should be changed through a
deployment pipeline outside Masakari API; Horizon should keep apply disabled
for draft plans with `apply_supported=false`.

Warnings:

```text
slot_lease_ttl < start_timeout -> warning
coordination.backend_url unset -> warning/error depending mode
Nova max microversion < 2.95 -> error when enabled=true
custom workflow not active -> warning
```

---

## 23. Recovery workflow behavior requirements

When staged recovery workflow is enabled:

```text
host failure notification
  ↓
disable_compute_service_task
  ↓
prepare_HA_enabled_instances_task
  ↓
reconcile_staged_recovery_task
  ↓
evacuate_to_stopped_task
  ↓
batched_start_instances_task
```

### 23.1 `reconcile_staged_recovery_task`

Must:

- read VMoves for notification;
- create/update etcd state per instance;
- read current Nova state;
- detect already evacuated instances;
- detect already active instances;
- release expired slots when safe;
- be idempotent.

### 23.2 `evacuate_to_stopped_task`

Must:

- use Nova microversion `2.95+`;
- not change global default Nova API version for existing flows;
- evacuate instance;
- leave instance stopped on destination;
- record dest_host;
- update VMove;
- update etcd state;
- not start VM.

### 23.3 `batched_start_instances_task`

Must:

- process only instances whose original state requires restart;
- default: restart only original `ACTIVE`;
- never auto-start original `SHUTOFF`;
- acquire etcd per-dest-host slot;
- call Nova start;
- wait until `ACTIVE`, `ERROR`, or timeout;
- keep slot until final state/timeout;
- release slot in finally;
- update etcd state;
- update VMove if appropriate.

---

## 24. Split-brain protection requirements

Staged recovery must not weaken existing split-brain protections.

Requirements:

- source compute service must be disabled/forced down according to existing Masakari/Nova behavior;
- evacuation must occur only after failure workflow confirms source is failed/disabled;
- never start duplicate instance manually outside Nova;
- all lifecycle operations go through Nova API;
- no direct libvirt operations;
- no direct hypervisor operations from Horizon or Masakari Admin API;
- if source host returns while notification running, diagnostics must raise warning;
- reconcile must check current host/status before starting.

---

## 25. Placement rules requirements

- Prefer Nova scheduler selection for destination host in `auto` mode.
- If reserved host is used, do not use `force=True` in evacuate.
- Do not bypass scheduler validation.
- Record final `dest_host` after Nova places the instance.
- Apply start limiter after destination host is known.
- Do not implement custom placement decisions in Horizon.

---

## 26. Preservation of state/configuration at failure

For each instance, staged recovery state must record:

```text
instance_uuid
instance_name
notification_uuid
source_host
dest_host
original_vm_state
original_task_state
original_power_state
original_locked_state
original_metadata relevant to HA
started_by_recovery: true|false
attempt
last_error
timestamps
```

Behavior:

```text
original ACTIVE  -> evacuate stopped -> staged start
original SHUTOFF -> evacuate stopped -> keep stopped
original ERROR   -> follow policy, default manual check / failed
other states     -> follow existing Masakari reset rules carefully, record original state
```

---

## 27. Config apply lifecycle

Required state machine:

```text
DRAFT_CREATED
  ↓
VALIDATING
  ↓
VALIDATED | VALIDATION_FAILED
  ↓
PLANNED
  ↓
APPROVED
  ↓
APPLY_QUEUED
  ↓
APPLYING
  ↓
APPLIED | APPLY_FAILED
  ↓
ROLLBACK_QUEUED
  ↓
ROLLING_BACK
  ↓
ROLLED_BACK | ROLLBACK_FAILED
```

Rules:

- Draft can be modified only before applying.
- Apply requires successful validation.
- Apply should be blocked if active recoveries exist unless explicitly allowed by policy and request flag.
- Rollback should use previous active config snapshot.
- Every state transition audited.

---

## 28. API error model

Use standard OpenStack-style error responses.

Examples:

### Validation error

```json
{
  "badRequest": {
    "code": 400,
    "message": "Invalid value for staged_recovery.max_parallel_starts_per_host: must be >= 1"
  }
}
```

### Conflict

```json
{
  "conflict": {
    "code": 409,
    "message": "Cannot apply draft while another config apply job is running."
  }
}
```

### Forbidden

```json
{
  "forbidden": {
    "code": 403,
    "message": "Policy does not allow os_masakari_api:admin-config-drafts:apply."
  }
}
```

### Not found

```json
{
  "itemNotFound": {
    "code": 404,
    "message": "Config draft draft-2026-06-10-999 could not be found."
  }
}
```

---

## 29. Compatibility checks for Horizon plugin

On plugin load/open overview:

1. Discover Masakari endpoint from Keystone catalog.
2. Call `GET /v1/` or root version endpoint.
3. Call `GET /v1/admin-overview/health`.
4. If Admin API missing, show:

```text
Masakari Admin API extension is not available.
Install/upgrade Masakari fork with admin_config and staged_recovery extensions.
```

5. If staged recovery endpoints missing, hide Staged Recovery panel or mark unavailable.

---

## 30. Codex implementation phases

Codex must implement in small reviewable phases.

### Phase 0 — Inspection only

Do not modify code. Inspect:

```text
setup.cfg
masakari/api/openstack/ha/*.py
masakari/api/openstack/ha/schemas/*.py
masakari/policies/*.py
masakari/ha/api.py
masakari/compute/nova.py
masakari/engine/drivers/taskflow/host_failure.py
masakari/conf/*.py
masakari/tests/unit/api/openstack/ha/
```

Return exact plan.

---

### Phase 1 — API skeleton and policies

Implement:

- API extension skeletons;
- controllers returning basic stub responses;
- schemas for empty/minimal requests;
- policy modules;
- entry points;
- unit tests for route/policy registration.

Do not implement config apply yet.

---

### Phase 2 — Config schema/effective API

Implement:

- config schema builder;
- metadata registry;
- effective config response;
- secret masking;
- tests.

---

### Phase 3 — Config drafts/diff/validation

Implement:

- draft repository abstraction;
- etcd-backed repository or fake/in-memory for tests;
- create/list/show/update/delete draft;
- validation;
- diff;
- tests.

---

### Phase 4 — Apply jobs API

Implement:

- apply plan;
- apply job model;
- runtime etcd apply backend;
- audit events;
- tests.

MVP applies only `staged_recovery.max_parallel_starts_per_host` through etcd
runtime override. Non-runtime config remains immutable for Masakari API.

---

### Phase 5 — Staged recovery state API

Implement:

- read state from etcd;
- list state;
- notification details;
- slots list;
- release expired slot;
- tests.

Do not allow arbitrary etcd delete.

---

### Phase 6 — Diagnostics API

Implement checks:

- Nova API reachability;
- Nova microversion 2.95 support;
- etcd read/write;
- workflow tasks registered;
- workflow active;
- coordination backend configured;
- stale notifications;
- tests.

---

### Phase 7 — Horizon plugin MVP

Implement panels:

- Overview;
- Segments and Hosts;
- Recoveries;
- Staged Recovery;
- Configuration;
- Apply Jobs;
- Diagnostics.

MVP can use Django-based Horizon panels first.

---

### Phase 8 — Hardening

Add:

- idempotency keys;
- audit completeness;
- RBAC tests;
- API functional tests;
- Horizon tests;
- docs;
- release notes.

---

## 31. Unit test requirements

### 31.1 API tests

Test:

- every new endpoint route;
- policy allowed/denied;
- validation errors;
- secret masking;
- draft lifecycle;
- apply lifecycle;
- staged state list;
- slots list/release;
- diagnostics job.

### 31.2 Service tests

Test:

- schema generation;
- metadata merge;
- diff generation;
- dependency validation;
- idempotency behavior;
- audit event generation.

### 31.3 etcd tests

Use fake/mocked etcd client.

Test:

- read/write JSON;
- prefix list;
- CAS/create semantics;
- TTL lease behavior;
- expired slot behavior;
- malformed state handling;
- backend unavailable.

### 31.4 Staged recovery tests

Test:

- original ACTIVE starts;
- original SHUTOFF does not start;
- duplicate retry does not evacuate twice;
- duplicate retry does not start twice;
- concurrent notifications share same dest_host limit;
- engine death releases slot by TTL.

### 31.5 Horizon tests

Test:

- panel registration;
- policy visibility;
- API client calls;
- form validation;
- secret field masking;
- action success/failure messages.

---

## 32. Functional acceptance criteria

A build is acceptable only if:

1. Existing Masakari API tests pass.
2. Existing Masakari recovery behavior is unchanged when staged workflow is not configured.
3. Horizon plugin never talks directly to etcd.
4. Horizon plugin never writes config files directly.
5. Masakari Admin API exposes config schema and effective config with secrets masked.
6. Config draft can be created, validated, planned, applied through noop backend.
7. Apply job has observable status.
8. Rollback job can be created for applied draft.
9. Staged recovery API can list staged state from etcd.
10. Slots API can list active slots.
11. Release slot refuses non-expired slot unless `force=true` and policy permits.
12. Diagnostics detects missing Nova microversion 2.95.
13. Diagnostics detects unavailable etcd.
14. Policy denies non-admin access to write/apply endpoints.
15. Audit records all write operations.
16. Staged recovery limiter ensures no more than `N` starts per destination host across multiple `masakari-engine`.
17. VM that was `SHUTOFF` before failure is not auto-started.
18. Nova evacuation-to-stopped uses microversion `2.95+` without changing global existing Nova client behavior.

---

## 33. Suggested first Codex prompt

Use this as the first message to Codex from repository root:

```text
Прочитай этот файл полностью:
CODEX_MASAKARI_HORIZON_ADMIN_API_REQUIREMENTS.md

Пока не меняй код.

Изучи fork Masakari stable/2025.1 и подготовь план реализации Masakari Admin REST API и Horizon plugin для Masakari HA.

Особое внимание удели:
- существующим API extensions в masakari.api.openstack.ha;
- setup.cfg entry points;
- policy modules;
- schemas/views/controllers;
- masakari.ha.api service layer;
- masakari.compute.nova;
- taskflow host_failure workflow;
- tests layout.

Верни на русском языке:
1. точный список файлов для изменения;
2. список новых модулей/классов/функций;
3. phased implementation plan;
4. риски совместимости;
5. минимальный первый patch.

Код на этом шаге не редактируй.
```

---

## 34. Suggested second Codex prompt: API skeleton

```text
Реализуй только Phase 1 из CODEX_MASAKARI_HORIZON_ADMIN_API_REQUIREMENTS.md.

Цель:
добавить skeleton Masakari Admin REST API extensions без реальной бизнес-логики.

Сделай:
1. API extension controllers для:
   - admin_overview;
   - admin_config;
   - recovery_workflows;
   - staged_recovery;
   - diagnostics;
   - audit.
2. Entry points в setup.cfg.
3. Policy modules и регистрацию в masakari/policies/__init__.py.
4. Минимальные schemas, где нужны POST/PATCH.
5. Unit tests на доступность routes/controllers/policies.

Ограничения:
- не меняй существующие endpoints;
- не добавляй долгую бизнес-логику;
- не подключай Horizon plugin на этом шаге;
- не делай direct etcd calls в controllers.

В конце покажи diff summary и команды тестов.
```

---

## 35. Suggested third Codex prompt: config schema/effective API

```text
Реализуй только Phase 2.

Цель:
добавить schema-driven Config API для Horizon UI.

Сделай:
1. config schema builder на основе зарегистрированных oslo.config opts;
2. manual metadata registry для UI/risk/restart/secret/service impact;
3. GET /v1/admin-config/schema;
4. GET /v1/admin-config/effective;
5. secret masking;
6. tests.

Не реализуй apply jobs на этом шаге.
```

---

## 36. Suggested fourth Codex prompt: drafts/validation/diff

```text
Реализуй только Phase 3.

Цель:
добавить config drafts, validation и diff.

Сделай:
1. repository abstraction;
2. etcd repository implementation или fake implementation plus interface, если etcd client ещё не подключён;
3. POST/GET/PATCH/DELETE drafts;
4. validate endpoint;
5. diff endpoint;
6. plan endpoint без фактического apply;
7. audit events for write operations;
8. tests.

Secrets не возвращать в ответах.
```

---

## 37. Suggested fifth Codex prompt: staged recovery API

```text
Реализуй Phase 5.

Цель:
проверить и при необходимости расширить существующий API для чтения staged
recovery state и start leases из etcd.

Сделай:
1. staged_recovery service API wrapper over existing `EtcdStagedRecoveryStore`;
2. verify existing `GET /v1/staged-recovery/instances`;
3. verify existing `GET /v1/staged-recovery/instances?notification_uuid={notification_uuid}`;
4. verify existing `GET /v1/staged-recovery/leases`;
5. verify existing `GET|PUT|DELETE /v1/staged-recovery/start-limit`;
6. policy checks;
7. tests.

Future extension:
add audited `POST /v1/staged-recovery/leases/{slot_id}/release` only after
release safety rules are implemented.

Не разрешай произвольное удаление etcd keys.
```

---

## 38. Notes for Codex: what not to do

Do not:

- implement Horizon direct etcd client;
- put secrets in browser responses;
- modify existing public API response contracts without compatibility review;
- change global `NOVA_API_VERSION` from `2.53` to `2.95` for all calls;
- run long apply operations in API request thread;
- use in-memory locks for distributed HA decisions;
- allow arbitrary task names in workflow config;
- auto-start original SHUTOFF instances;
- use Nova evacuate with `force=True`;
- bypass Nova scheduler;
- implement raw `masakari.conf` textarea as the only config UI.

---

## 39. Definition of done

The implementation is done when:

```text
[ ] Masakari Admin REST API extensions exist and are covered by tests.
[ ] Config schema/effective endpoints return usable typed metadata.
[ ] Config drafts/validate/plan/diff/apply-job skeleton work.
[ ] Secrets are masked everywhere.
[ ] Staged recovery state and slots are readable through Masakari API.
[ ] Slot release is protected and audited.
[ ] Diagnostics API provides Nova/etcd/workflow/coordination checks.
[ ] Horizon plugin displays overview, recoveries, staged recovery, config and diagnostics.
[ ] Horizon plugin never talks directly to etcd or files.
[ ] RBAC policies exist and are tested.
[ ] Existing Masakari behavior remains unchanged unless staged workflow is configured.
[ ] Documentation and sample configs are added.
```
