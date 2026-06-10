# Masakari Admin Config API: реализованный функционал, API и оставшиеся задачи

Документ описывает текущую реализацию Admin Config API в fork-е Masakari для
интеграции с Horizon Masakari HA plugin. Основное ТЗ находится в
`docs/specs/CODEX_MASAKARI_HORIZON_ADMIN_API_REQUIREMENTS.md`; этот файл
фиксирует, что уже реализовано в backend, как этим пользоваться и какие части
контракта еще нужно довести до production-уровня.

## Текущий статус

Реализован backend MVP для schema-driven настройки Masakari staged recovery из
Horizon:

- получение схемы поддерживаемых настроек;
- получение текущих effective values с маскированием secret-like полей;
- CRUD для config drafts;
- validation, diff и apply plan для draft;
- no-op apply workflow с сохранением apply job в SQL DB;
- просмотр apply jobs;
- rollback endpoint как явная заглушка с `409 Conflict`;
- policy rules для всех новых Admin Config endpoints;
- unit-тесты API, DB, миграции и policy/extension loading.

Текущая реализация безопасна для интеграционной оценки Horizon, потому что
`apply` не пишет конфигурационные файлы, не меняет runtime config, не пишет в
etcd, не перезапускает сервисы и не вызывает внешний deployment backend.

## Область реализации

Новый API extension:

```text
admin-config
```

Новые top-level collections:

```text
/v1/admin-config/...
/v1/admin-config-drafts/...
/v1/admin-config-apply-jobs/...
```

В зависимости от стандартного Masakari route setup эти ресурсы также могут быть
доступны в project-scoped форме:

```text
/v1/{project_id}/admin-config/...
/v1/{project_id}/admin-config-drafts/...
/v1/{project_id}/admin-config-apply-jobs/...
```

На текущем этапе API поддерживает только группу настроек:

```text
[staged_recovery]
```

Другие группы `masakari.conf`, `masakari-custom-recovery-methods.conf`,
`coordination`, `taskflow_driver_recovery_flows`, `host_failure` и т.п. пока не
включены в typed schema и будут отклоняться validation-логикой как
`unknown_group`.

## Модель данных

### Config drafts

Config drafts хранятся в SQL DB в таблице `admin_config_drafts`.

Основные поля:

```text
uuid        - публичный идентификатор draft;
name        - необязательное имя;
status      - draft / valid / invalid / applied;
values      - JSON с requested changes;
comment     - необязательный комментарий оператора;
validation  - JSON с последним результатом validation;
plan        - JSON с последним apply plan.
```

Draft является staging-объектом. Создание или изменение draft само по себе
ничего не меняет в live Masakari configuration.

### Apply jobs

Apply jobs хранятся в SQL DB в таблице `admin_config_apply_jobs`.

Основные поля:

```text
uuid        - публичный идентификатор apply job;
draft_uuid  - ссылка на config draft;
status      - queued / succeeded / failed, сейчас фактически queued -> succeeded;
strategy    - строка из apply request, по умолчанию noop;
canary      - boolean-флаг из apply request;
comment     - комментарий apply request;
plan        - JSON apply plan, сохраненный на момент apply;
result      - JSON результат выполнения;
errors      - JSON массив ошибок.
```

В no-op backend apply job создается, затем в рамках того же HTTP request
переводится в `succeeded`.

## Policy rules

Все endpoints закрыты admin policy rules:

```text
os_masakari_api:admin-config:schema
os_masakari_api:admin-config:effective

os_masakari_api:admin-config-drafts:index
os_masakari_api:admin-config-drafts:detail
os_masakari_api:admin-config-drafts:create
os_masakari_api:admin-config-drafts:update
os_masakari_api:admin-config-drafts:delete
os_masakari_api:admin-config-drafts:validate
os_masakari_api:admin-config-drafts:diff
os_masakari_api:admin-config-drafts:plan
os_masakari_api:admin-config-drafts:apply

os_masakari_api:admin-config-apply-jobs:index
os_masakari_api:admin-config-apply-jobs:detail
os_masakari_api:admin-config-apply-jobs:rollback
```

Сейчас все они используют `rule:admin_api`. Разделение на `ha_viewer`,
`ha_operator`, `ha_admin` пока не реализовано.

## API

### GET `/v1/admin-config/schema`

Возвращает schema metadata для UI-формы Horizon.

Policy:

```text
os_masakari_api:admin-config:schema
```

Response:

```json
{
  "schema": {
    "groups": [
      {
        "name": "staged_recovery",
        "options": [
          {
            "name": "max_parallel_starts_per_host",
            "type": "int",
            "default": 3,
            "mutable": true,
            "deploy_stage": "runtime",
            "secret": false,
            "help": "..."
          },
          {
            "name": "batch_delay",
            "type": "int",
            "default": 0,
            "mutable": false,
            "deploy_stage": "reconfigure",
            "secret": false,
            "help": "..."
          }
        ]
      }
    ]
  }
}
```

Правила формирования:

- `type` берется из oslo.config option class: `bool`, `int`, `string`;
- `default` берется из определения config option;
- `mutable=true` сейчас только для
  `staged_recovery.max_parallel_starts_per_host`;
- `deploy_stage=runtime` означает, что параметр потенциально может
  применяться через runtime path;
- `deploy_stage=reconfigure` означает, что нужен deployment/reconfigure
  backend;
- `secret=true` выставляется по имени option, если имя содержит
  `password`, `secret`, `token` или `key`.

### GET `/v1/admin-config/effective`

Возвращает текущие значения из `CONF.staged_recovery`.

Policy:

```text
os_masakari_api:admin-config:effective
```

Query parameters:

```text
group=staged_recovery
```

Если `group` не задан, возвращается `staged_recovery`. Если указан другой
group, API возвращает `404 Not Found`.

Response:

```json
{
  "config": {
    "staged_recovery": {
      "enabled": false,
      "max_parallel_starts_per_host": 3,
      "etcd_password": {
        "masked": true,
        "configured": true
      }
    }
  }
}
```

Secret-like значения не возвращаются в открытом виде.

### POST `/v1/admin-config-drafts`

Создает draft.

Policy:

```text
os_masakari_api:admin-config-drafts:create
```

Request:

```json
{
  "draft": {
    "name": "Enable staged recovery",
    "comment": "Change start limiter before enabling workflow in deployment",
    "changes": {
      "staged_recovery": {
        "max_parallel_starts_per_host": 4,
        "batch_delay": 5
      }
    }
  }
}
```

Альтернативный формат `changes`:

```json
{
  "draft": {
    "changes": [
      {
        "group": "staged_recovery",
        "option": "max_parallel_starts_per_host",
        "value": 4
      }
    ]
  }
}
```

Response: `201 Created`

```json
{
  "draft": {
    "uuid": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
    "name": "Enable staged recovery",
    "status": "draft",
    "changes": {
      "staged_recovery": {
        "max_parallel_starts_per_host": 4,
        "batch_delay": 5
      }
    },
    "comment": "Change start limiter before enabling workflow in deployment"
  }
}
```

### GET `/v1/admin-config-drafts`

Возвращает список drafts, отсортированный по `id desc`.

Policy:

```text
os_masakari_api:admin-config-drafts:index
```

Response:

```json
{
  "drafts": [
    {
      "uuid": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
      "name": "Enable staged recovery",
      "status": "draft",
      "changes": {
        "staged_recovery": {
          "max_parallel_starts_per_host": 4
        }
      },
      "comment": null
    }
  ]
}
```

Pagination и фильтры для drafts пока не реализованы в API layer.

### GET `/v1/admin-config-drafts/{draft_id}`

Возвращает draft по UUID.

Policy:

```text
os_masakari_api:admin-config-drafts:detail
```

Response включает `validation` и `plan`, если они уже были рассчитаны.

### PATCH `/v1/admin-config-drafts/{draft_id}`

Обновляет draft.

Policy:

```text
os_masakari_api:admin-config-drafts:update
```

Request:

```json
{
  "draft": {
    "name": "Updated staged recovery tuning",
    "comment": "Operator changed limit after review",
    "changes": {
      "staged_recovery": {
        "max_parallel_starts_per_host": 2
      }
    }
  }
}
```

Если меняются `changes` или `values`, API сбрасывает `validation` и `plan`, а
`status` возвращает в `draft`.

### DELETE `/v1/admin-config-drafts/{draft_id}`

Удаляет draft.

Policy:

```text
os_masakari_api:admin-config-drafts:delete
```

Response: `204 No Content`

### POST `/v1/admin-config-drafts/{draft_id}/validate`

Валидирует draft against текущей typed schema.

Policy:

```text
os_masakari_api:admin-config-drafts:validate
```

Response:

```json
{
  "draft": {
    "uuid": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
    "status": "valid",
    "changes": {
      "staged_recovery": {
        "max_parallel_starts_per_host": 4,
        "batch_delay": 5
      }
    },
    "comment": null,
    "validation": {
      "status": "valid",
      "errors": [],
      "warnings": [
        {
          "code": "reconfigure_required",
          "group": "staged_recovery",
          "option": "batch_delay",
          "message": "Changing this option requires deploy backend reconfiguration."
        }
      ]
    }
  },
  "validation": {
    "status": "valid",
    "errors": [],
    "warnings": []
  }
}
```

Возможные ошибки validation:

```text
invalid_changes       - changes не object и не list;
invalid_group_value   - значение группы не object;
invalid_change        - элемент list не object;
unknown_group         - unsupported config group;
unknown_option        - unsupported option внутри supported group;
invalid_type          - тип значения не совпадает с oslo.config option type;
below_minimum         - int ниже min value option;
invalid_choice        - значение не входит в choices.
```

Если есть errors, draft получает `status=invalid`. Если errors нет,
`status=valid`.

### GET `/v1/admin-config-drafts/{draft_id}/diff`

Возвращает diff между current effective config и draft changes.

Policy:

```text
os_masakari_api:admin-config-drafts:diff
```

Response:

```json
{
  "diff": {
    "changes": [
      {
        "group": "staged_recovery",
        "option": "max_parallel_starts_per_host",
        "current": 3,
        "proposed": 4,
        "changed": true,
        "valid": true
      }
    ]
  }
}
```

Для secret-like option `current` и `proposed` маскируются.

### POST `/v1/admin-config-drafts/{draft_id}/plan`

Строит apply plan и сохраняет его в draft.

Policy:

```text
os_masakari_api:admin-config-drafts:plan
```

Response:

```json
{
  "plan": {
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
    "apply_supported": false
  }
}
```

Текущее значение `apply_supported=false` означает, что production apply backend
еще не реализован. При этом no-op apply endpoint доступен, чтобы Horizon мог
отработать UX apply-job lifecycle без изменения реальной конфигурации.

### POST `/v1/admin-config-drafts/{draft_id}/apply`

Создает no-op apply job.

Policy:

```text
os_masakari_api:admin-config-drafts:apply
```

Request:

```json
{
  "apply": {
    "strategy": "noop",
    "canary": false,
    "comment": "Horizon dry-run apply"
  }
}
```

Поля:

```text
strategy - string, optional, default noop; сейчас только сохраняется в job;
canary   - boolean, optional, default false; сейчас только сохраняется в job;
comment  - string, optional.
```

Response: `202 Accepted`

```json
{
  "apply_job": {
    "id": "1281c9c3-b6cc-4397-b04d-6b31a4a6d487",
    "draft_id": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
    "status": "succeeded",
    "strategy": "noop",
    "canary": false,
    "comment": "Horizon dry-run apply",
    "created_at": "2026-06-10T13:40:00Z",
    "updated_at": "2026-06-10T13:40:00Z",
    "errors": [],
    "plan": {
      "status": "planned",
      "steps": [
        {
          "group": "staged_recovery",
          "option": "max_parallel_starts_per_host",
          "action": "runtime_update"
        }
      ],
      "apply_supported": false
    },
    "result": {
      "status": "succeeded",
      "backend": "noop",
      "changed": false,
      "message": "No-op apply backend recorded the plan without changing configuration."
    }
  }
}
```

Логика:

1. API проверяет policy.
2. API загружает draft.
3. Если draft уже `applying` или `applied`, возвращается `409 Conflict`.
4. API повторно валидирует changes.
5. Если validation содержит errors, draft обновляется до `invalid`, а request
   завершается `400 Bad Request`.
6. API строит plan.
7. API создает apply job со статусом `queued`.
8. No-op backend сразу переводит job в `succeeded`.
9. Draft переводится в `applied`.
10. Response возвращает `apply_job`.

Важно: статус draft `applied` в текущем MVP означает, что apply workflow был
успешно записан и завершен no-op backend-ом. Это не означает, что live
configuration была изменена.

### GET `/v1/admin-config-apply-jobs`

Возвращает список apply jobs.

Policy:

```text
os_masakari_api:admin-config-apply-jobs:index
```

Query parameters:

```text
status=succeeded
draft_id=6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de
```

Response:

```json
{
  "apply_jobs": [
    {
      "id": "1281c9c3-b6cc-4397-b04d-6b31a4a6d487",
      "draft_id": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
      "status": "succeeded",
      "strategy": "noop",
      "canary": false,
      "comment": "Horizon dry-run apply",
      "created_at": "2026-06-10T13:40:00Z",
      "updated_at": "2026-06-10T13:40:00Z",
      "errors": [],
      "plan": {
        "status": "planned",
        "steps": []
      },
      "result": {
        "status": "succeeded",
        "backend": "noop",
        "changed": false
      }
    }
  ]
}
```

Сортировка сейчас `id desc`. Pagination для apply jobs в controller пока не
выведена в HTTP query contract, хотя DB API уже имеет параметры `limit` и
`marker`.

### GET `/v1/admin-config-apply-jobs/{job_id}`

Возвращает apply job по UUID.

Policy:

```text
os_masakari_api:admin-config-apply-jobs:detail
```

Response:

```json
{
  "apply_job": {
    "id": "1281c9c3-b6cc-4397-b04d-6b31a4a6d487",
    "draft_id": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
    "status": "succeeded",
    "strategy": "noop",
    "canary": false,
    "comment": "Horizon dry-run apply",
    "created_at": "2026-06-10T13:40:00Z",
    "updated_at": "2026-06-10T13:40:00Z",
    "errors": [],
    "plan": {
      "status": "planned",
      "steps": []
    },
    "result": {
      "status": "succeeded",
      "backend": "noop",
      "changed": false
    }
  }
}
```

### POST `/v1/admin-config-apply-jobs/{job_id}/rollback`

Rollback endpoint зарегистрирован, защищен policy и проверяет существование
apply job, но production rollback еще не реализован.

Policy:

```text
os_masakari_api:admin-config-apply-jobs:rollback
```

Response:

```text
409 Conflict
Rollback is not supported by the no-op apply backend.
```

Это сделано намеренно: Horizon может уже показать action и корректно обработать
unsupported state, но backend не будет создавать ложное ощущение настоящего
rollback.

## Логика работы end-to-end

### 1. Horizon строит форму

Horizon вызывает:

```text
GET /v1/admin-config/schema
GET /v1/admin-config/effective?group=staged_recovery
```

На основе `schema.groups[].options[]` UI строит typed controls:

- checkbox для `bool`;
- numeric input для `int`;
- text/select для `string`;
- disabled/readonly presentation для unsupported или secret fields;
- badge `runtime` или `reconfigure` по `deploy_stage`;
- предупреждения для `secret=true`.

### 2. Horizon создает draft

После изменения формы Horizon вызывает:

```text
POST /v1/admin-config-drafts
```

Backend сохраняет changes в SQL DB, но ничего не применяет.

### 3. Horizon валидирует и показывает diff

Horizon вызывает:

```text
POST /v1/admin-config-drafts/{draft_id}/validate
GET  /v1/admin-config-drafts/{draft_id}/diff
POST /v1/admin-config-drafts/{draft_id}/plan
```

Backend:

- проверяет supported group/option;
- проверяет типы значений;
- проверяет minimum/choices;
- отмечает параметры, требующие reconfigure;
- строит diff без раскрытия secret-like values;
- строит plan с actions `runtime_update` или `reconfigure_required`.

### 4. Horizon запускает no-op apply

Horizon вызывает:

```text
POST /v1/admin-config-drafts/{draft_id}/apply
```

Backend создает apply job и сразу завершает его как:

```json
{
  "status": "succeeded",
  "backend": "noop",
  "changed": false
}
```

Это подтверждает, что API contract и Horizon workflow работают, но live config
не меняется.

### 5. Horizon отслеживает job

Horizon может вызвать:

```text
GET /v1/admin-config-apply-jobs
GET /v1/admin-config-apply-jobs/{job_id}
```

Для no-op backend polling обычно не нужен, потому что job завершается в том же
request. Для будущего external/ansible/kolla backend этот contract уже подходит
под polling.

## Что уже можно интегрировать в Horizon plugin

Можно реализовать:

- страницу Admin Config для `[staged_recovery]`;
- schema-driven форму;
- masked effective values;
- create/update/delete draft;
- validate action;
- diff view;
- plan view;
- apply action как dry-run/no-op apply;
- список apply jobs;
- детальную страницу apply job;
- disabled или error-handled rollback action.

Horizon plugin не должен интерпретировать no-op apply как реальное применение
конфигурации. В UI стоит явно показывать backend/result:

```text
backend: noop
changed: false
```

## Что еще осталось доделать

### 1. Production apply backend

Нужно реализовать реальное применение конфигурации через backend abstraction:

```text
noop
external webhook
ansible
kolla/kolla-ansible
helm
local_file только для dev/test
```

Минимальный production-путь для Horizon:

```text
Masakari API -> external deployment controller
```

Backend должен:

- принимать apply plan;
- выполнять deployment/reconfigure вне HTTP request;
- возвращать progress/status/errors;
- уметь различать runtime-only changes и reconfigure-required changes;
- не раскрывать secrets;
- писать audit events.

### 2. Настоящая асинхронность apply jobs

Сейчас no-op apply завершается синхронно в HTTP request. Для production нужно:

- создать job `queued`;
- передать выполнение worker-у или внешнему backend-у;
- вернуть `202 Accepted` сразу;
- обновлять job statuses: `queued`, `running`, `succeeded`, `failed`,
  `cancelled`;
- добавить polling-friendly timestamps и progress;
- добавить защиту от параллельных apply jobs.

### 3. Реальный runtime apply для supported mutable options

Сейчас `max_parallel_starts_per_host` помечен как `runtime`, но apply не пишет
runtime override в etcd. Нужно связать Admin Config API с уже реализованным
runtime override staged recovery limit:

```text
staged_recovery.max_parallel_starts_per_host
        -> etcd runtime config key
        -> EtcdStartLimiter effective limit
```

После этого plan для runtime-only draft сможет реально менять поведение без
перезапуска сервисов.

### 4. Reconfigure apply для non-runtime options

Для `batch_delay`, `start_timeout`, `slot_lease_ttl`, `etcd_*`,
`nova_evacuate_microversion` и других non-runtime options нужен deployment
backend, который обновляет config files или values в системе управления
деплоем, а затем безопасно перезапускает/перечитывает нужные сервисы.

### 5. Поддержка остальных config groups

Нужно расширить schema/draft/validation/diff/plan на:

- `coordination`;
- `taskflow_driver_recovery_flows`;
- `host_failure`;
- `notification`;
- параметры Masakari engine/API/monitors, которые нужны Horizon;
- `masakari-custom-recovery-methods.conf`.

Для workflow tasks нужен allowlist, чтобы Horizon не мог отправить произвольное
имя Python task.

### 6. Rollback lifecycle

Сейчас rollback возвращает `409 Conflict`. Нужно реализовать:

- snapshot applied configuration before apply;
- rollback job model или тип apply job;
- `POST /admin-config-apply-jobs/{job_id}/rollback -> 202 Accepted`;
- rollback plan;
- rollback execution через тот же deployment backend;
- отображение rollback status/errors;
- audit events.

### 7. Audit API и события

Требование из основного ТЗ пока не закрыто. Нужно добавить:

- audit event model/repository;
- `GET /v1/admin-audit/events`;
- audit для create/update/delete draft;
- audit для validate/plan/apply/rollback;
- запись changed fields без plaintext secrets;
- связь audit events с user/project/request id.

### 8. Idempotency-Key

Сейчас write endpoints не поддерживают `Idempotency-Key`.

Нужно добавить idempotency для:

- draft create;
- validate;
- apply;
- rollback;
- diagnostics jobs;
- future emergency operations.

Это особенно важно для Horizon, потому что пользователь может повторить request
после timeout или browser retry.

### 9. Более строгая state machine draft/apply

Сейчас состояния минимальные. Нужно формализовать transitions:

```text
draft -> valid -> planned -> applying -> applied
draft -> invalid
applying -> failed
applied -> rollback_requested -> rollback_running -> rolled_back
```

Также нужно запретить удаление/изменение draft, если по нему уже есть running
apply job.

### 10. Pagination, filters и marker support

DB API уже частично поддерживает sort/limit/marker для apply jobs, но HTTP
controller пока не предоставляет полноценный contract.

Нужно добавить:

- `limit`;
- `marker`;
- сортировку;
- фильтры по `status`, `draft_id`, `created_at`;
- одинаковый pagination style для drafts и jobs.

### 11. Service layer extraction

Сейчас логика MVP находится в API controller. Для production лучше вынести
бизнес-логику в service layer, например:

```text
masakari/ha/admin_config_api.py
masakari/config_admin/schema.py
masakari/config_admin/drafts.py
masakari/config_admin/apply.py
```

API controller должен остаться тонким: policy, request parsing, response view.

### 12. Horizon plugin

Backend contract уже достаточен для первого UI prototype, но сам Horizon plugin
еще нужно реализовать:

- read-only overview/admin config panels;
- forms для staged recovery options;
- draft table/detail;
- diff/plan views;
- apply job table/detail;
- policy-aware actions;
- compatibility check, если endpoint недоступен;
- понятное отображение no-op backend.

Перед UI-разработкой нужно сделать read-only checkout/fork
`openstack/masakari-dashboard` и реализовывать изменения в plugin, а не в Horizon
core, если core changes не потребуются.

### 13. Diagnostics API

Из основного ТЗ еще остается diagnostics:

- Masakari API/engine/monitor health;
- Nova availability и max microversion `2.95+`;
- etcd read/write check;
- coordination check;
- staged tasks registration check;
- workflow config check;
- stale notification/recovery checks.

### 14. Тесты, которые стоит добавить следующими

Текущие unit-тесты закрывают MVP. Для следующих фаз нужны:

- concurrency tests для одного активного apply job;
- idempotency tests;
- rollback tests;
- audit tests;
- service-layer tests после extraction;
- Horizon API client tests;
- negative API tests для invalid apply body, pagination и filters;
- production backend driver tests.

## Практический следующий шаг

Самый полезный следующий backend этап:

```text
Реализовать runtime apply для staged_recovery.max_parallel_starts_per_host:
draft -> validate -> plan -> apply -> запись runtime override в etcd -> apply job succeeded.
```

Почему именно он:

- это маленький production-полезный срез;
- он уже соответствует текущему `runtime_update` plan action;
- он не требует перезапуска сервисов;
- он напрямую нужен staged recovery limiter-у;
- Horizon сможет показать не только no-op workflow, но и реально работающее
  изменение безопасного параметра.

После этого можно переходить к external deployment backend для
`reconfigure_required` параметров и к Horizon UI.
