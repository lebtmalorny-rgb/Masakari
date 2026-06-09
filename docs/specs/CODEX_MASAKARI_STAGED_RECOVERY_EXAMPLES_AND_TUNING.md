# Masakari staged recovery: примеры эвакуации и правила настройки

Документ описывает, как работает staged recovery при отказе одной или
нескольких compute-нод, где задается лимит одновременного старта ВМ и как
безопасно менять этот лимит в работающем кластере.

## Где задается лимит одновременного старта

Лимит задается в `masakari.conf`:

```ini
[staged_recovery]
enabled = true
max_parallel_starts_per_host = 2
```

Параметр `max_parallel_starts_per_host` означает:

```text
Максимальное число одновременно стартующих ВМ на один destination compute host.
```

Это не лимит на notification и не лимит на весь кластер. Это лимит именно на
каждый destination hypervisor.

В коде значение читается при создании `EtcdStartLimiter` внутри
`BatchedStartInstancesTask`:

```text
BatchedStartInstancesTask
  -> EtcdStartLimiter(...)
     -> EtcdStagedRecoveryStore.get_max_parallel_starts_per_host(...)
        -> runtime etcd override или CONF.staged_recovery.max_parallel_starts_per_host
```

При каждой попытке старта limiter:

1. Берет короткий distributed lock через Tooz:
   `staged-start-lock-<dest_host>`.
2. Считает живые etcd lease keys для этого destination host.
3. Если живых lease keys меньше лимита, создает новый TTL lease key.
4. Отпускает Tooz lock.
5. Удерживает etcd lease до `ACTIVE`, `ERROR` или timeout.

## Можно ли поменять лимит на работающем кластере

Да. Реализация поддерживает runtime override в etcd. Это позволяет изменить
лимит для новых start-slot allocation без перезапуска `masakari-engine`.

Runtime override хранится в etcd:

```text
<etcd_prefix>/config/max_parallel_starts_per_host
```

При каждой попытке взять start slot `EtcdStartLimiter` читает effective limit:

```text
runtime etcd override, если он задан
иначе значение из masakari.conf
```

### Команды управления runtime limit

В этом репозитории добавлена server-side admin-команда через `masakari-manage`:

```bash
masakari-manage staged_recovery get_start_limit
masakari-manage staged_recovery set_start_limit 3
masakari-manage staged_recovery clear_start_limit
```

Команды используют тот же etcd backend, что и staged recovery state. Их нужно
запускать там, где доступен `masakari.conf` с `[staged_recovery]` и
`[coordination]`.

Пример для Kolla container:

```bash
docker exec -it masakari_engine masakari-manage staged_recovery get_start_limit
docker exec -it masakari_engine masakari-manage staged_recovery set_start_limit 3
docker exec -it masakari_engine masakari-manage staged_recovery clear_start_limit
```

`clear_start_limit` удаляет runtime override из etcd. После этого effective
limit снова берется из `masakari.conf`.

### Можно ли сделать это через `openstack` CLI

На стороне Masakari server теперь есть REST API, поэтому лимит можно менять
без доступа к контейнеру `masakari-engine`. Нативная команда вида
`openstack masakari staged recovery ...` все еще должна быть добавлена в
отдельном клиентском репозитории `python-masakariclient`, но у нее уже есть
серверный REST-контракт.

REST API:

```http
GET    /v1/{project_id}/staged-recovery/start-limit
PUT    /v1/{project_id}/staged-recovery/start-limit
DELETE /v1/{project_id}/staged-recovery/start-limit
GET    /v1/{project_id}/staged-recovery/instances
GET    /v1/{project_id}/staged-recovery/leases
```

В примерах ниже `MASAKARI_API` - это root URL Masakari API без `/v1`, например
`http://masakari-api.example.com:15868`.

Пример установки лимита через REST:

```bash
curl -s -X PUT \
  -H "X-Auth-Token: $OS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"start_limit": {"max_parallel_starts_per_host": 3}}' \
  "$MASAKARI_API/v1/$OS_PROJECT_ID/staged-recovery/start-limit"
```

Пример просмотра effective limit:

```bash
curl -s -H "X-Auth-Token: $OS_TOKEN" \
  "$MASAKARI_API/v1/$OS_PROJECT_ID/staged-recovery/start-limit"
```

Ответ:

```json
{
  "start_limit": {
    "max_parallel_starts_per_host": 3,
    "source": "runtime"
  }
}
```

Пример просмотра staged states по notification:

```bash
curl -s -H "X-Auth-Token: $OS_TOKEN" \
  "$MASAKARI_API/v1/$OS_PROJECT_ID/staged-recovery/instances?notification_uuid=$NOTIFICATION_UUID&limit=100"
```

Пример просмотра live leases по destination host:

```bash
curl -s -H "X-Auth-Token: $OS_TOKEN" \
  "$MASAKARI_API/v1/$OS_PROJECT_ID/staged-recovery/leases?dest_host=compute-3&limit=100"
```

Query filters для `instances`:

- `notification_uuid`;
- `instance_uuid`;
- `source_host`;
- `dest_host`;
- `step`;
- `limit`.

Query filters для `leases`:

- `notification_uuid`;
- `instance_uuid`;
- `dest_host`;
- `limit`.

`limit` по умолчанию равен `100`, максимальное разрешенное значение `1000`.
Для больших кластеров не стоит делать unfiltered diagnostic requests: endpoint
читает staged recovery etcd prefix, а затем применяет лимит к ответу. Фильтр
`dest_host` для leases оптимизирован и сужает etcd prefix scan.

Чтобы получить удобную нативную команду OpenStack CLI, следующий patch должен
быть в `python-masakariclient` и должен добавить команды примерно такого вида:

```bash
openstack masakari staged recovery start limit show
openstack masakari staged recovery start limit set 3
openstack masakari staged recovery start limit unset
openstack masakari staged recovery instance list --notification $NOTIFICATION_UUID
openstack masakari staged recovery lease list --dest-host compute-3
```

## Нужно ли перезапускать `masakari-engine`

Для runtime override через `masakari-manage` перезапуск не нужен.

Перезапуск нужен только если меняется static config в `masakari.conf`, например
если runtime override очищен, а нужно изменить fallback/default:

Безопасный порядок для Kolla/Kolla-Ansible:

1. Обновить Masakari config override, например:

   ```ini
   [staged_recovery]
   max_parallel_starts_per_host = 3
   ```

2. Применить конфигурацию Kolla-Ansible для Masakari.

3. Перезапустить все `masakari-engine` containers, чтобы все engine-процессы
   работали с одинаковым лимитом.

4. Проверить логи `masakari-engine` и etcd lease keys.

Для уменьшения лимита, например с `4` до `2`, важно понимать:

- уже стартующие ВМ не будут остановлены;
- существующие etcd lease keys останутся до release или TTL expiration;
- новые start slots не будут выдаваться на host, пока число live leases не
  станет меньше нового лимита;
- если лимит меняется через runtime override, все engine-процессы увидят новое
  значение при следующей allocation попытке.

Для увеличения лимита, например с `2` до `4`:

- существующие leases сохраняются;
- engine сможет выдавать больше новых slots после изменения runtime override;
- эффект появится для новых allocation попыток.

## Базовая настройка для примеров

Кластер:

```text
compute-1
compute-2
compute-3
compute-4
compute-5
```

Staged recovery config:

```ini
[staged_recovery]
enabled = true
state_backend = etcd
max_parallel_starts_per_host = 2
start_timeout = 900
batch_delay = 30
slot_lease_ttl = 990
slot_retry_interval = 5
nova_evacuate_microversion = 2.95
start_only_originally_active = true

[coordination]
backend_url = etcd3+http://etcd.example.internal:2379
```

Recovery flow:

```ini
[taskflow_driver_recovery_flows]
host_auto_failure_recovery_tasks = pre:['disable_compute_service_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']
host_rh_failure_recovery_tasks = pre:['disable_compute_service_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']
```

## Пример 1: падает одна нода со 100 ВМ

Исходное состояние:

```text
compute-1: 100 active ВМ
compute-2: alive
compute-3: alive
compute-4: alive
compute-5: alive
```

Падает `compute-1`.

Шаги recovery:

1. Masakari получает host failure notification для `compute-1`.
2. `disable_compute_service_task` отключает nova-compute service на
   `compute-1`.
3. `prepare_HA_enabled_instances_task` создает `VMove` records для 100 ВМ.
4. `reconcile_staged_recovery_task` создает etcd state для каждой ВМ:

   ```text
   DISCOVERED
   ```

5. `evacuate_to_stopped_task` вызывает Nova evacuate с microversion `2.95`.
6. Nova scheduler выбирает destination hosts среди живых нод.
7. После evacuation ВМ остаются stopped на destination host.
8. etcd state переходит в:

   ```text
   EVACUATED_STOPPED
   ```

Условное распределение Nova scheduler:

```text
compute-2: 25 ВМ
compute-3: 25 ВМ
compute-4: 25 ВМ
compute-5: 25 ВМ
```

При `max_parallel_starts_per_host = 2` одновременно стартуют максимум:

```text
compute-2: 2 ВМ
compute-3: 2 ВМ
compute-4: 2 ВМ
compute-5: 2 ВМ
```

Итого по кластеру:

```text
4 destination hosts * 2 slots = 8 concurrent starts
```

Когда одна ВМ на `compute-2` становится `ACTIVE`, Masakari освобождает lease и
может запустить следующую stopped ВМ на `compute-2`. То же самое независимо
происходит на `compute-3`, `compute-4` и `compute-5`.

## Пример 2: падают две ноды по 100 ВМ

Исходное состояние:

```text
compute-1: 100 active ВМ
compute-2: 100 active ВМ
compute-3: alive
compute-4: alive
compute-5: alive
```

Почти одновременно падают `compute-1` и `compute-2`.

Живые destination hosts:

```text
compute-3
compute-4
compute-5
```

Nova scheduler условно распределяет 200 эвакуированных ВМ так:

```text
compute-3: 67 ВМ
compute-4: 67 ВМ
compute-5: 66 ВМ
```

Даже если два `masakari-engine` одновременно обрабатывают два разных
notification, limiter смотрит в общий etcd namespace:

```text
/masakari/staged-recovery/v1/start-leases/compute-3/...
/masakari/staged-recovery/v1/start-leases/compute-4/...
/masakari/staged-recovery/v1/start-leases/compute-5/...
```

При `max_parallel_starts_per_host = 2` одновременно стартуют максимум:

```text
compute-3: 2 ВМ
compute-4: 2 ВМ
compute-5: 2 ВМ
```

Итого по кластеру:

```text
3 destination hosts * 2 slots = 6 concurrent starts
```

Лимит общий для всех recovery workflows. Если notification от падения
`compute-1` уже занял два слота на `compute-3`, notification от падения
`compute-2` не сможет запустить третью ВМ на `compute-3`, пока один из слотов
не освободится или не истечет по TTL.

## Формула для оценки concurrency

Для auto recovery:

```text
cluster_concurrent_starts <= live_destination_hosts * max_parallel_starts_per_host
```

Примеры:

```text
1 failed host в 5-node cluster:
live destination hosts = 4
limit = 2
max starts = 4 * 2 = 8

2 failed hosts в 5-node cluster:
live destination hosts = 3
limit = 2
max starts = 3 * 2 = 6
```

На практике значение может быть ниже, если Nova scheduler распределила ВМ
неравномерно или если на части destination hosts нет stopped candidates.

## Правила выбора лимита

Начинать лучше консервативно:

```ini
max_parallel_starts_per_host = 2
```

Увеличивать лимит стоит только после проверки:

- CPU steal/load на destination hosts;
- storage latency;
- network pressure;
- время загрузки типовой ВМ;
- поведение control plane во время массового recovery;
- доля ВМ с тяжелым boot I/O.

Практические ориентиры:

```text
1-2 slots per host: безопасный старт для тяжелых/неизвестных workloads.
3-5 slots per host: возможно для легких workloads и быстрых storage/network.
>5 slots per host: требует load testing, иначе легко получить boot storm.
```

## Связанные параметры

`start_timeout`
    Максимальное время ожидания перехода ВМ в `ACTIVE`.

`batch_delay`
    Дополнительная задержка после успешного старта перед release slot. Помогает
    сгладить CPU/I/O spikes сразу после boot.

`slot_lease_ttl`
    TTL etcd lease. Должен быть не меньше `start_timeout + batch_delay`.
    Рекомендуется добавлять запас.

Пример:

```ini
start_timeout = 900
batch_delay = 30
slot_lease_ttl = 990
```

Здесь запас:

```text
990 - 900 - 30 = 60 секунд
```

## Что смотреть во время эксплуатации

etcd lease keys:

```text
<etcd_prefix>/start-leases/<encoded_dest_host>/<instance_uuid>
```

staged instance state:

```text
<etcd_prefix>/notifications/<notification_uuid>/instances/<instance_uuid>
```

Ключевые state transitions:

```text
DISCOVERED
EVACUATING
EVACUATED_STOPPED
WAITING_START_SLOT
STARTING
ACTIVE | FAILED | IGNORED
```

Если `STARTING` зависает дольше `stale_recovery_timeout`, manager reconcile
переведет SQL notification из `RUNNING` в `ERROR`, после чего штатный механизм
unfinished notifications сможет повторить workflow с учетом etcd state.
