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
- runtime apply для `staged_recovery.max_parallel_starts_per_host` через etcd
  override;
- immutable `masakari.conf` contract: draft-ы с `reconfigure_required`
  параметрами можно валидировать, смотреть diff/plan, но нельзя применить через
  Masakari API;
- просмотр apply jobs;
- policy rules для всех новых Admin Config endpoints;
- unit-тесты API, DB, миграции и policy/extension loading.

Текущая реализация безопасна для интеграционной оценки Horizon: `apply` не
пишет конфигурационные файлы, не перезапускает сервисы и не имеет deployment
backend в публичном контракте. Единственное реальное изменение runtime state -
запись `staged_recovery.max_parallel_starts_per_host` в существующий etcd
runtime override, который уже читает staged recovery limiter.

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
status      - queued / succeeded / failed, сейчас фактически queued -> succeeded/failed;
strategy    - server-controlled значение runtime, оставлено для совместимости схемы;
canary      - server-controlled значение false, оставлено для совместимости схемы;
comment     - комментарий apply request;
plan        - JSON apply plan, сохраненный на момент apply;
result      - JSON результат выполнения;
errors      - JSON массив ошибок.
```

Для runtime-only draft-а apply job создается и в рамках того же HTTP request
завершается через backend `runtime_etcd`. Для draft-а с reconfigure-required
изменениями apply job не создается: API возвращает `409 Conflict`.

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
- `deploy_stage=reconfigure` означает immutable `masakari.conf` параметр:
  его можно показывать, валидировать и планировать, но нельзя применять через
  Masakari API;
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
      "max_parallel_starts_per_host": {
        "value": 3,
        "source": "config"
      },
      "etcd_password": {
        "masked": true,
        "configured": true
      }
    }
  }
}
```

Secret-like значения не возвращаются в открытом виде.
Runtime-mutable `max_parallel_starts_per_host` возвращается как объект с
`value` и `source`, где `source=config` означает значение из `masakari.conf`, а
`source=runtime` означает etcd runtime override.

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

`apply_supported=true` выставляется для runtime-only plan, где все steps имеют
`action=runtime_update`. Сейчас это только
`staged_recovery.max_parallel_starts_per_host`.

`apply_supported=false` для plan с `reconfigure_required` означает, что
соответствующие `masakari.conf` параметры immutable для Masakari API. Такие
draft-ы нельзя применить через этот endpoint; Horizon должен оставить apply
action disabled или показать понятную ошибку.

### POST `/v1/admin-config-drafts/{draft_id}/apply`

Создает apply job только для runtime-only draft-а. Runtime-only draft
применяется через etcd runtime override; draft с `reconfigure_required`
изменениями отклоняется как immutable configuration.

Policy:

```text
os_masakari_api:admin-config-drafts:apply
```

Request:

```json
{
  "apply": {
    "comment": "Horizon runtime apply"
  }
}
```

Поля:

```text
comment - string, optional.
```

`strategy` и `canary` больше не являются управляющими параметрами apply API.
Новые jobs всегда создаются как `strategy=runtime`, `canary=false`. Для мягкой
совместимости API допускает `strategy=runtime` и `canary=false`, но отклоняет
`strategy` с любым другим значением и `canary=true` как `400 Bad Request`.

Response: `202 Accepted`

```json
{
  "apply_job": {
    "id": "1281c9c3-b6cc-4397-b04d-6b31a4a6d487",
    "draft_id": "6ac7b8d6-6f0e-46b9-a785-42d6b8d3c7de",
    "status": "succeeded",
    "strategy": "runtime",
    "canary": false,
    "comment": "Horizon runtime apply",
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
      "apply_supported": true
    },
    "result": {
      "status": "succeeded",
      "backend": "runtime_etcd",
      "changed": true,
      "runtime_updates": [
        {
          "group": "staged_recovery",
          "option": "max_parallel_starts_per_host",
          "value": 4,
          "source": "runtime"
        }
      ],
      "message": "Runtime configuration was applied to etcd."
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
6. API проверяет apply request: поддерживается только runtime strategy без
   canary.
7. API строит plan.
8. Если plan содержит `reconfigure_required`, API сохраняет validation/plan в
   draft и возвращает `409 Conflict` без создания apply job.
9. API создает apply job со статусом `queued`, `strategy=runtime` и
   `canary=false`.
10. Если plan runtime-only, backend `runtime_etcd` пишет supported values в
   etcd runtime override и переводит job в `succeeded`.
11. Если runtime backend возвращает `InvalidInput`, job переводится в `failed`,
    request завершается `400 Bad Request`, draft остается не примененным.
12. Если runtime backend падает неожиданно, job переводится в `failed`, а
    request идет через стандартный `500 Internal Server Error` путь.
13. Draft переводится в `applied` только после успешного apply.
14. Response возвращает `apply_job`.

Важно: статус draft `applied` в текущем MVP означает, что apply workflow был
успешно записан и завершен через `backend=runtime_etcd`; live runtime override
действительно изменен. Draft-ы с immutable `masakari.conf` изменениями не
переходят в `applied`.

Если draft содержит non-runtime изменения, ответ:

```text
409 Conflict
Draft contains immutable Masakari configuration changes. Only runtime configuration changes are supported by Masakari API apply.
```

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
      "strategy": "runtime",
      "canary": false,
      "comment": "Horizon runtime apply",
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
        "apply_supported": true
      },
      "result": {
        "status": "succeeded",
        "backend": "runtime_etcd",
        "changed": true
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
    "strategy": "runtime",
    "canary": false,
    "comment": "Horizon runtime apply",
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
      "apply_supported": true
    },
    "result": {
      "status": "succeeded",
      "backend": "runtime_etcd",
      "changed": true
    }
  }
}
```

Rollback endpoint не входит в текущий backend MVP. Для runtime-only параметра
откат выполняется обычным новым draft/apply со старым значением
`staged_recovery.max_parallel_starts_per_host`; для immutable `masakari.conf`
параметров изменение и откат остаются задачей deployment-процесса вне Masakari
API.

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

### 4. Horizon запускает apply

Horizon вызывает:

```text
POST /v1/admin-config-drafts/{draft_id}/apply
```

Для runtime-only draft-а backend создает apply job, записывает etcd runtime
override и сразу завершает его как:

```json
{
  "status": "succeeded",
  "backend": "runtime_etcd",
  "changed": true
}
```

Для draft-а с `reconfigure_required` изменениями backend возвращает
`409 Conflict`, потому что `masakari.conf` параметры immutable для Masakari API.
Apply job в этом случае не создается, и partial runtime update не выполняется.

### 5. Horizon отслеживает job

Horizon может вызвать:

```text
GET /v1/admin-config-apply-jobs
GET /v1/admin-config-apply-jobs/{job_id}
```

Для `runtime_etcd` polling обычно не нужен, потому что job завершается в том же
request. Список и detail нужны как audit-friendly история runtime apply и для
отображения результата/ошибок в Horizon.

## Что уже можно интегрировать в Horizon plugin

Можно реализовать:

- страницу Admin Config для `[staged_recovery]`;
- schema-driven форму;
- masked effective values;
- create/update/delete draft;
- validate action;
- diff view;
- plan view;
- apply action для runtime-only `max_parallel_starts_per_host`;
- disabled apply action для reconfigure-required drafts;
- список apply jobs;
- детальную страницу apply job.

Horizon plugin должен явно показывать backend/result:

```text
backend: runtime_etcd
changed: true
```

## Что еще осталось доделать

### 1. Immutable non-runtime config remains out of Masakari API apply

В текущем контракте `masakari.conf` параметры immutable для Masakari API, поэтому
backend не должен реализовывать `rolling`, `canary`, rollback или внешний
deployment/reconfigure path. Horizon может показывать такие параметры как
read-only/staged и объяснять оператору, что изменение выполняется через
deployment pipeline вне Masakari API.

### 2. Настоящая асинхронность apply jobs

Сейчас `runtime_etcd` apply завершается синхронно в HTTP request. Если появятся
другие runtime-safe операции, которые могут выполняться долго, нужно:

- создать job `queued`;
- передать выполнение worker-у;
- вернуть `202 Accepted` сразу;
- обновлять job statuses: `queued`, `running`, `succeeded`, `failed`,
  `cancelled`;
- добавить polling-friendly timestamps и progress;
- добавить защиту от параллельных apply jobs.

### 3. Расширение runtime apply

Сейчас runtime apply реализован только для
`staged_recovery.max_parallel_starts_per_host`. Если появятся другие runtime-safe
options, нужно расширить allowlist, validation, plan и runtime backend.

### 4. Явное UI-поведение для non-runtime options

Для `batch_delay`, `start_timeout`, `slot_lease_ttl`, `etcd_*`,
`nova_evacuate_microversion` и других non-runtime options Horizon должен
показывать `deploy_stage=reconfigure` и `apply_supported=false`. Backend уже
возвращает `409 Conflict`, если такой draft отправлен на apply; UI должен
предотвращать этот сценарий на стороне оператора.

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

### 6. Audit API и события

Требование из основного ТЗ пока не закрыто. Нужно добавить:

- audit event model/repository;
- `GET /v1/admin-audit/events`;
- audit для create/update/delete draft;
- audit для validate/plan/apply;
- запись changed fields без plaintext secrets;
- связь audit events с user/project/request id.

### 7. Idempotency-Key

Сейчас write endpoints не поддерживают `Idempotency-Key`.

Нужно добавить idempotency для:

- draft create;
- validate;
- apply;
- diagnostics jobs;
- future emergency operations.

Это особенно важно для Horizon, потому что пользователь может повторить request
после timeout или browser retry.

### 8. Более строгая state machine draft/apply

Сейчас состояния минимальные. Нужно формализовать transitions:

```text
draft -> valid -> planned -> applying -> applied
draft -> invalid
applying -> failed
```

Также нужно запретить удаление/изменение draft, если по нему уже есть running
apply job.

### 9. Pagination, filters и marker support

DB API уже частично поддерживает sort/limit/marker для apply jobs, но HTTP
controller пока не предоставляет полноценный contract.

Нужно добавить:

- `limit`;
- `marker`;
- сортировку;
- фильтры по `status`, `draft_id`, `created_at`;
- одинаковый pagination style для drafts и jobs.

### 10. Service layer extraction

Сейчас логика MVP находится в API controller. Для production лучше вынести
бизнес-логику в service layer, например:

```text
masakari/ha/admin_config_api.py
masakari/config_admin/schema.py
masakari/config_admin/drafts.py
masakari/config_admin/apply.py
```

API controller должен остаться тонким: policy, request parsing, response view.

### 11. Horizon plugin

Backend contract уже достаточен для первого UI prototype, но сам Horizon plugin
еще нужно реализовать:

- read-only overview/admin config panels;
- forms для staged recovery options;
- draft table/detail;
- diff/plan views;
- apply job table/detail;
- policy-aware actions;
- compatibility check, если endpoint недоступен;
- понятное отображение immutable `apply_supported=false` для non-runtime
  параметров.

Перед UI-разработкой нужно сделать read-only checkout/fork
`openstack/masakari-dashboard` и реализовывать изменения в plugin, а не в Horizon
core, если core changes не потребуются.

### 12. Diagnostics API

Из основного ТЗ еще остается diagnostics:

- Masakari API/engine/monitor health;
- Nova availability и max microversion `2.95+`;
- etcd read/write check;
- coordination check;
- staged tasks registration check;
- workflow config check;
- stale notification/recovery checks.

### 13. Тесты, которые стоит добавить следующими

Текущие unit-тесты закрывают MVP. Для следующих фаз нужны:

- concurrency tests для одного активного apply job;
- idempotency tests;
- audit tests;
- service-layer tests после extraction;
- Horizon API client tests;
- negative API tests для invalid apply body, pagination и filters;
- runtime backend extension tests, если появятся новые runtime-safe options.

## Практический следующий шаг

Самый полезный следующий backend этап при сохранении immutable `masakari.conf`:

```text
Вынести Admin Config бизнес-логику из API controller в service layer:
controller -> service -> runtime backend / immutable apply guard.
```

Почему именно он:

- runtime-safe срез уже закрыт через `runtime_etcd`;
- `masakari.conf` параметры остаются read-only/immutable через API;
- service layer упростит будущие audit/idempotency/state-machine проверки;
- Horizon plugin сможет отдельно использовать текущий контракт без ожидания
  дополнительных backend-фичей.

Параллельно можно начинать Horizon UI/plugin клиент для текущего API contract.
