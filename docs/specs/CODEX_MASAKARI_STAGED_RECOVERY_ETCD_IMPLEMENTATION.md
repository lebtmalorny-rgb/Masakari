# Masakari staged recovery etcd: описание реализации

Этот документ описывает, что было реализовано поверх ТЗ
`CODEX_MASAKARI_STAGED_RECOVERY_ETCD.md`, и какие новые внутренние API
появились в fork-е Masakari.

## Что сделано

1. Добавлена конфигурационная группа `[staged_recovery]`:
   - включение staged mode;
   - выбор etcd state backend;
   - параметры подключения к etcd;
   - prefix для ключей;
   - лимит одновременных стартов на destination host;
   - таймаут старта, задержка между batch-ами и TTL lease;
   - Nova microversion для evacuate-to-stopped;
   - включение запуска только ВМ, которые были `active` до отказа;
   - periodic reconcile для stale recovery state.

2. Добавлена отдельная Nova-обертка для staged evacuation:
   - обычный Masakari Nova client продолжает использовать microversion `2.53`;
   - staged evacuation использует `[staged_recovery] nova_evacuate_microversion`,
     по умолчанию `2.95`;
   - для auto recovery destination host не передается, чтобы Nova scheduler сам
     выбрал target host;
   - для reserved-host recovery target host передается как `host`, без
     `force=True`.

3. Добавлен etcd state store:
   - состояние каждой ВМ хранится под
     `<etcd_prefix>/notifications/<notification_uuid>/instances/<instance_uuid>`;
   - запись создается idempotent-методом create-or-get;
   - обновления используют compare-and-replace;
   - поддержаны состояния `DISCOVERED`, `EVACUATING`, `EVACUATED_STOPPED`,
     `WAITING_START_SLOT`, `STARTING`, `ACTIVE`, `FAILED`, `IGNORED`;
   - original state ВМ сохраняется и не перезаписывается при retry/reconcile;
   - stale states выбираются по `updated_at`.

4. Добавлены etcd TTL start leases:
   - ключи создаются под
     `<etcd_prefix>/start-leases/<encoded_dest_host>/<instance_uuid>`;
   - lease удерживает слот до `ACTIVE`, `ERROR` или timeout;
   - если создание ключа не удалось из-за гонки, неиспользованный lease
     отзывается сразу;
   - release удаляет ключ и отзывает lease.

5. Добавлен distributed start limiter:
   - Tooz используется только как короткий mutex на allocation секцию;
   - long-running start slot представлен etcd TTL lease key;
   - если coordination недоступна, staged start падает закрыто;
   - `slot_lease_ttl` валидируется как минимум
     `start_timeout + batch_delay`.

6. Добавлен runtime override лимита старта:
   - ключ хранится в etcd под `<etcd_prefix>/config/max_parallel_starts_per_host`;
   - `EtcdStartLimiter` читает effective limit при каждой allocation попытке;
   - если runtime override отсутствует, используется значение из
     `masakari.conf`;
   - добавлены команды `masakari-manage staged_recovery get_start_limit`,
     `set_start_limit` и `clear_start_limit`.

7. Добавлен admin REST API для staged recovery:
   - `GET /v1/{project_id}/staged-recovery/start-limit`;
   - `PUT /v1/{project_id}/staged-recovery/start-limit`;
   - `DELETE /v1/{project_id}/staged-recovery/start-limit`;
   - `GET /v1/{project_id}/staged-recovery/instances`;
   - `GET /v1/{project_id}/staged-recovery/leases`;
   - endpoints защищены policy rules `os_masakari_api:staged-recovery:*`;
   - list endpoints поддерживают filters и `limit`.

8. Добавлены staged TaskFlow tasks:
   - `ReconcileStagedRecoveryTask`;
   - `EvacuateToStoppedTask`;
   - `BatchedStartInstancesTask`.

9. Добавлены entry points:
   - `reconcile_staged_recovery_task`;
   - `evacuate_to_stopped_task`;
   - `batched_start_instances_task`;
   - API extension `staged_recovery`.

10. Добавлен sample recovery config:
   - `etc/masakari/masakari-staged-recovery-methods.conf`;
   - default `masakari-custom-recovery-methods.conf` не изменен, чтобы staged
     workflow не включался неявно.

11. Добавлен manager reconcile:
   - periodic task ищет stale staged states в etcd;
   - для каждого notification берет distributed lock;
   - если SQL notification все еще `RUNNING`, переводит его в `ERROR`;
   - повторный запуск делает штатный unfinished notification processing.

12. Добавлен startup/stop coordination для RPC service:
    - если `[coordination] backend_url` задан, engine service запускает и
      останавливает глобальный coordinator;
    - это нужно для staged limiter и manager reconcile locks.

13. Добавлена документация, release note и unit-тесты:
    - Nova wrapper tests;
    - etcd state store tests;
    - start limiter tests;
    - staged host failure task tests;
    - TaskFlow fallback tests;
    - service coordination startup test;
    - manager stale reconcile test.

## Новые внутренние API

### `masakari.compute.nova`

#### `novaclient(context, timeout=None, api_version=NOVA_API_VERSION)`

Создает Nova client с выбранной microversion.

Совместимость:
- существующие вызовы без `api_version` остаются на `NOVA_API_VERSION = "2.53"`;
- staged recovery передает `api_version="2.95"` или значение из config.

#### `API.evacuate_instance_stopped(context, uuid, target=None)`

Выполняет Nova evacuate с microversion из
`CONF.staged_recovery.nova_evacuate_microversion`.

Поведение:
- `target is None`: вызывает `nova.servers.evacuate(uuid)`;
- `target is not None`: вызывает `nova.servers.evacuate(uuid, host=target)`;
- метод не включает `force=True`;
- ожидаемое Nova-поведение при microversion `2.95+`: эвакуированная ВМ остается
  stopped на destination host до явного start.

#### `API.get_server_with_microversion(context, uuid, api_version)`

Получает server через Nova client с явно заданной microversion.
Метод добавлен как вспомогательный API для staged/reconcile логики и будущих
проверок Nova state с нестандартной microversion.

### `masakari.engine.drivers.taskflow.staged_state_etcd`

#### `EtcdStagedRecoveryStore(conf, owner, client=None)`

Хранилище staged recovery state в etcd.

Основные методы instance state:
- `assert_available()`;
- `create_or_get_instance_state(notification_uuid, instance_uuid, values)`;
- `get_instance_state(notification_uuid, instance_uuid)`;
- `list_instance_states(notification_uuid)`;
- `list_stale_instance_states(older_than_seconds, steps, now=None)`;
- `update_instance_state(notification_uuid, instance_uuid, patch)`;
- `transition_instance_state(notification_uuid, instance_uuid, expected_steps, new_step, patch=None)`;
- `mark_failed(notification_uuid, instance_uuid, error)`.

Методы start leases:
- `list_start_leases(dest_host)`;
- `create_start_lease(notification_uuid, instance_uuid, dest_host, ttl)`;
- `refresh_start_lease(lease_record)`;
- `release_start_lease(dest_host, instance_uuid)`.

Методы runtime config:
- `get_runtime_config(name)`;
- `set_runtime_config(name, value)`;
- `clear_runtime_config(name)`;
- `get_max_parallel_starts_per_host(default)`;
- `get_max_parallel_starts_per_host_with_source(default)`;
- `set_max_parallel_starts_per_host(value)`;
- `clear_max_parallel_starts_per_host()`.

Diagnostic/list methods для REST API:
- `list_all_instance_states(filters=None, limit=None)`;
- `list_all_start_leases(filters=None, limit=None)`.

Key helpers:
- `instance_key(notification_uuid, instance_uuid)`;
- `notification_instances_prefix(notification_uuid)`;
- `start_lease_key(dest_host, instance_uuid)`;
- `start_lease_prefix(dest_host)`;
- `runtime_config_key(name)`;
- `encode_key_part(value)`;
- `decode_key_part(value)`.

Исключения:
- `ConcurrentStateUpdate`;
- `StateNotFound`.

### `masakari.engine.drivers.taskflow.staged_limiter`

#### `EtcdStartLimiter(context, state_store, owner, coordinator=None)`

Distributed limiter для start операций.

Методы:
- `acquire(notification_uuid, instance_uuid, dest_host)`;
- `release(slot)`.

`acquire()` возвращает `StartSlot`. Если coordination lock недоступен, метод
падает закрыто через `MasakariException`. Перед проверкой capacity метод читает
runtime override `max_parallel_starts_per_host` из etcd; если override не задан,
использует значение из `masakari.conf`.

#### `StartSlot`

Объект результата allocation:
- `notification_uuid`;
- `instance_uuid`;
- `dest_host`;
- `lease_id`.

### `masakari.engine.drivers.taskflow.staged_host_failure`

#### `ReconcileStagedRecoveryTask`

TaskFlow task. Требует из flow store:
- `host_name`;
- `notification_uuid`.

Делает create-or-get etcd state для `VMove` records и синхронизирует state с
текущим Nova server state.

#### `EvacuateToStoppedTask`

TaskFlow task. Требует из flow store:
- `host_name`;
- `notification_uuid`;
- optional `reserved_host` в reserved-host retry flow.

Выполняет staged evacuation, обновляет `VMove`, пишет destination host и
финальный evacuation state в etcd.

#### `BatchedStartInstancesTask`

TaskFlow task. Требует из flow store:
- `notification_uuid`.

Берет candidates из etcd, получает start slot через `EtcdStartLimiter`,
запускает Nova server и удерживает lease до результата. Если хотя бы одна
originally active ВМ закончила `FAILED`, выбрасывает
`StagedStartFailureException`.

### `masakari.engine.manager`

#### `_process_stale_staged_recoveries(context)`

Periodic task, активный только при `CONF.staged_recovery.enabled`.

Алгоритм:
1. Сканирует etcd state по шагам `EVACUATING`, `WAITING_START_SLOT`,
   `STARTING`.
2. Фильтрует записи старше `stale_recovery_timeout`.
3. Группирует по `notification_uuid`.
4. Берет lock `staged-recovery-notification-<uuid>`.
5. Если SQL notification все еще `RUNNING`, переводит его в `ERROR`.

Метод специально не запускает workflow напрямую, чтобы не получить два
одновременных recovery flow для одного notification.

### `masakari.exception`

#### `StagedStartFailureException`

Наследник `HostRecoveryFailureException`.

Используется для batched start failures. Для `auto_priority` и `rh_priority`
driver не делает fallback на другой host recovery method после этой ошибки,
потому что evacuation уже могла частично завершиться, а повторный fallback
может привести к небезопасному поведению.

### `masakari.cmd.manage`

#### `masakari-manage staged_recovery get_start_limit`

Печатает effective staged start limit. Если runtime override в etcd отсутствует,
печатает значение из `masakari.conf`.

#### `masakari-manage staged_recovery set_start_limit <value>`

Пишет runtime override в etcd. Значение должно быть целым числом `>= 1`.
Новый лимит применяется для новых start-slot allocation попыток без
перезапуска `masakari-engine`.

#### `masakari-manage staged_recovery clear_start_limit`

Удаляет runtime override из etcd. После этого лимит снова берется из
`masakari.conf`.

### `masakari.api.openstack.ha.staged_recovery`

#### `StagedRecoveryController`

Admin REST controller для runtime staged recovery state.

Методы:
- `show(req, id)`;
- `update(req, id, body)`;
- `delete(req, id)`.

Поддерживаемые `id`:
- `start-limit`;
- `instances`;
- `leases`.

`GET start-limit` возвращает:

```json
{
  "start_limit": {
    "max_parallel_starts_per_host": 2,
    "source": "config"
  }
}
```

`PUT start-limit` принимает:

```json
{
  "start_limit": {
    "max_parallel_starts_per_host": 3
  }
}
```

`GET instances` поддерживает exact-match filters:
- `notification_uuid`;
- `instance_uuid`;
- `source_host`;
- `dest_host`;
- `step`;
- `limit`.

`GET leases` поддерживает exact-match filters:
- `notification_uuid`;
- `instance_uuid`;
- `dest_host`;
- `limit`.

Для list endpoints `limit` по умолчанию равен `100`, максимум `1000`.

#### `StagedRecovery`

API extension class. Регистрирует resource `staged-recovery` через
`masakari.api.v1.extensions`.

### `masakari.api.openstack.ha.schemas.staged_recovery`

#### `update_start_limit`

JSON schema для `PUT /staged-recovery/start-limit`. Проверяет наличие объекта
`start_limit` и integer поля `max_parallel_starts_per_host`. Проверка `>= 1`
остается в `EtcdStagedRecoveryStore`, чтобы один и тот же validation path
использовался REST API и `masakari-manage`.

### `masakari.policies.staged_recovery`

Новые policy rules:
- `os_masakari_api:staged-recovery:start_limit:show`;
- `os_masakari_api:staged-recovery:start_limit:update`;
- `os_masakari_api:staged-recovery:start_limit:delete`;
- `os_masakari_api:staged-recovery:instances:index`;
- `os_masakari_api:staged-recovery:leases:index`;
- `os_masakari_api:staged-recovery:discoverable`.

## Новые entry points

В `setup.cfg` добавлены:

```ini
masakari.task_flow.tasks =
    reconcile_staged_recovery_task = masakari.engine.drivers.taskflow.staged_host_failure:ReconcileStagedRecoveryTask
    evacuate_to_stopped_task = masakari.engine.drivers.taskflow.staged_host_failure:EvacuateToStoppedTask
    batched_start_instances_task = masakari.engine.drivers.taskflow.staged_host_failure:BatchedStartInstancesTask

masakari.api.v1.extensions =
    staged_recovery = masakari.api.openstack.ha.staged_recovery:StagedRecovery
```

## Новые файлы

- `masakari/conf/staged_recovery.py`
- `masakari/api/openstack/ha/staged_recovery.py`
- `masakari/api/openstack/ha/schemas/staged_recovery.py`
- `masakari/engine/drivers/taskflow/staged_state_etcd.py`
- `masakari/engine/drivers/taskflow/staged_limiter.py`
- `masakari/engine/drivers/taskflow/staged_host_failure.py`
- `masakari/policies/staged_recovery.py`
- `etc/masakari/masakari-staged-recovery-methods.conf`
- `doc/source/configuration/staged_recovery.rst`
- `releasenotes/notes/staged-recovery-etcd-3f2bb8e7a6d2c955.yaml`
- `releasenotes/notes/staged-recovery-rest-api-8ce2a61dc10f3d91.yaml`
- unit-тесты `test_nova_staged.py`, `test_staged_state_etcd.py`,
  `test_staged_limiter.py`, `test_staged_host_failure.py`,
  `test_staged_recovery.py`

## Проверки, выполненные перед первым commit

- targeted unit suite по затронутым зонам: `95 tests OK`;
- `py_compile` измененных Python modules;
- `git diff --check`;
- parse sample config через `oslo.config`.

Live integration с реальным Nova + etcd в рамках этого patch set не
выполнялась.
