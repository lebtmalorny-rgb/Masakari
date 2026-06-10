# Masakari Redfish fencing: алгоритм работы и порядок настройки

Документ описывает эксплуатационную модель fencing для host failure recovery в
этом fork-е Masakari. Он дополняет техническое задание
`CODEX_MASAKARI_REDFISH_FENCING_ETCD.md` и фиксирует практический порядок:

- как fencing встраивается в recovery workflow;
- какой алгоритм выполняется до Nova evacuation;
- какие параметры задаются до деплоя;
- какие параметры можно менять после деплоя и когда нужен рестарт;
- какие состояния и ключи смотреть при диагностике.

Ключевое правило безопасности:

```text
Нет verified Redfish fencing proof с PowerState=Off -> нет Nova evacuation.
```

Fencing нужен для защиты от split-brain. Если failed compute host на самом деле
продолжает выполнять ВМ, а Masakari уже эвакуирует эти же ВМ на другие hosts,
можно получить одновременный запуск одной нагрузки в двух местах. Поэтому
fencing стоит перед evacuation, а staged recovery стоит после evacuation.

## Где fencing находится в recovery flow

Целевой host failure workflow:

```ini
[taskflow_driver_recovery_flows]
host_auto_failure_recovery_tasks = pre:['collect_fencing_failure_set_task', 'disable_failure_set_task', 'redfish_fence_failure_set_task', 'assert_failure_set_fenced_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']
host_rh_failure_recovery_tasks = pre:['collect_fencing_failure_set_task', 'disable_failure_set_task', 'redfish_fence_failure_set_task', 'assert_failure_set_fenced_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']
```

Логический порядок:

```text
Masakari host failure notification
  -> collect_fencing_failure_set_task
  -> disable_failure_set_task
  -> redfish_fence_failure_set_task
  -> assert_failure_set_fenced_task
  -> prepare_HA_enabled_instances_task
  -> reconcile_staged_recovery_task
  -> evacuate_to_stopped_task
  -> batched_start_instances_task
```

Важная граница ответственности:

- fencing выключает или подтверждает выключение failed compute hosts;
- Nova evacuation переносит ВМ после подтвержденного fencing;
- staged recovery запускает эвакуированные ВМ по лимитам, чтобы не устроить boot
  storm.

## Общий алгоритм recovery

### 1. Masakari получает host failure notification

Host monitor создает notification для failed compute host. TaskFlow получает:

- `host_name`;
- `segment_uuid`;
- `event_id`;
- `notification_uuid`.

Эти значения используются как ключи для etcd state и audit trail.

### 2. `collect_fencing_failure_set_task`

Task делает fail-fast проверки:

1. Проверяет, что `[redfish_fencing] enabled = true`.
2. Проверяет доступность etcd backend.
3. Регистрирует факт отказа host в etcd.
4. Читает недавние host failure events в том же segment.
5. Формирует `failure_set`.
6. Проверяет, что размер `failure_set` не больше
   `max_auto_fence_hosts_per_segment`.
7. Проверяет, что после исключения failed hosts остается не меньше
   `min_surviving_compute_hosts`.
8. Проверяет, что для каждого host в `failure_set` есть Redfish mapping.

Если любая проверка не проходит, workflow завершается ошибкой до evacuation.

Назначение `failure_set`: если в одном segment почти одновременно упали
несколько compute hosts, один notification не должен эвакуировать ВМ так, будто
остальные failed hosts живы и пригодны как destination.

### 3. `disable_failure_set_task`

Task отключает nova-compute service для каждого host из `failure_set`:

```text
nova service-disable <hostname> nova-compute
```

В коде это выполняется через Nova client
`enable_disable_service(..., enable=False, reason=...)`.

Цель:

- убрать failed hosts из планирования;
- не дать Nova scheduler выбрать host из того же failure set как destination;
- сделать состояние control plane согласованным до fencing.

### 4. `redfish_fence_failure_set_task`

Task берет segment lock в etcd, затем обрабатывает каждый host из
`failure_set`. Для каждого host:

1. Берется per-host fencing lock в etcd.
2. Текущее состояние host переводится в `FENCING`.
3. Загружается mapping из `hosts_config_path`.
4. Создается Redfish client.
5. Выполняется Redfish fencing.
6. Успешный proof сохраняется в etcd как `FENCED`.
7. При ошибке состояние сохраняется как `FENCE_FAILED`.
8. Redfish session закрывается.
9. Per-host lock освобождается.

Segment lock нужен, чтобы два `masakari-engine` не выполняли конкурирующий
recovery для одного segment. Per-host lock нужен, чтобы два workflow не
выключали один и тот же BMC одновременно и не перетирали proof.

### 5. Redfish client

Для каждого host Redfish client выполняет такой алгоритм:

1. `GET <base_uri>` для проверки доступности Redfish service.
2. Если `auth_type = session`, создает Redfish session через
   `POST <base_uri>/SessionService/Sessions`.
3. Если `auth_type = basic`, использует HTTP Basic auth.
4. `GET <systems_uri>` и читает Redfish ComputerSystem.
5. Проверяет identity guards:
   - `expected_system_uuid`;
   - `expected_serial_number`;
   - `expected_asset_tag`;
   - `expected_manufacturer`;
   - `expected_model`.
6. Проверяет Redfish reset action и допустимость `ForceOff`.
7. Если `PowerState` уже равен `Off`, переходит к стабильной проверке `Off`.
8. Если `PowerState` не `Off`, выполняет:

   ```json
   {"ResetType": "ForceOff"}
   ```

9. Polling-ом читает `systems_uri`, пока не получит
   `stable_power_state_reads` последовательных ответов `PowerState=Off`.
10. Возвращает proof.
11. В `finally` удаляет Redfish session, если использовалась session auth.

`ForceOff` используется намеренно. Graceful shutdown, reboot, restart и
power-cycle не являются достаточным split-brain fencing proof.

### 6. `assert_failure_set_fenced_task`

Task повторно проверяет etcd state для всех hosts из `failure_set`.

Evacuation разрешается только если для каждого host:

```text
state == FENCED
verified_power_state == Off
```

Если state отсутствует, равен `FENCE_FAILED` или содержит другой
`verified_power_state`, workflow останавливается.

### 7. Evacuation и staged start

После successful fencing workflow переходит к staged recovery:

1. `prepare_HA_enabled_instances_task` создает `VMove` records.
2. `reconcile_staged_recovery_task` создает/обновляет per-VM state в etcd.
3. `evacuate_to_stopped_task` вызывает Nova evacuate с microversion `2.95`.
4. Эвакуированные ВМ остаются stopped на destination host.
5. `batched_start_instances_task` запускает только ВМ, которые до сбоя были
   `ACTIVE`, и ограничивает concurrency через etcd leases.

## Redfish fencing state machine

Состояния host fencing:

| State | Значение |
|---|---|
| `FENCE_REQUIRED` | Host failure зарегистрирован, fencing еще не начат. |
| `FENCING` | Task владеет lock и выполняет Redfish fencing. |
| `FENCED` | Host подтвержден как выключенный, proof сохранен в etcd. |
| `FENCE_FAILED` | Fencing не был подтвержден; evacuation запрещен. |

Переходы:

| Текущее состояние | Условие | Следующее состояние |
|---|---|---|
| missing | Получен host failure event | `FENCE_REQUIRED` |
| `FENCE_REQUIRED` | Per-host lock взят | `FENCING` |
| `FENCING` | Redfish вернул стабильный `PowerState=Off` | `FENCED` |
| `FENCING` | BMC недоступен, TLS ошибка, timeout, identity mismatch | `FENCE_FAILED` |
| `FENCED` | Повторный workflow видит валидный proof | `FENCED` |

Повторный workflow должен быть idempotent: если host уже `FENCED` и proof
валиден, task не должна повторно выключать BMC без необходимости.

## etcd key model

Fencing state хранится отдельно от staged recovery state.

Default prefix:

```text
/masakari/redfish-fencing/v1
```

Failure events:

```text
<etcd_prefix>/failures/<segment_uuid>/<event_id>/<hostname>
```

Host state:

```text
<etcd_prefix>/hosts/<hostname>
```

Locks:

```text
<etcd_prefix>/locks/segments/<segment_uuid>
<etcd_prefix>/locks/hosts/<hostname>
```

Пример successful host state:

```json
{
  "schema_version": 1,
  "segment_uuid": "segment-a",
  "event_id": "event-1",
  "hostname": "compute-01",
  "notification_uuid": "7ce6d9a3-1111-2222-3333-abcdefabcdef",
  "owner": "masakari-engine-1-12345",
  "state": "FENCED",
  "method": "redfish",
  "systems_uri": "/redfish/v1/Systems/1",
  "reset_target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
  "requested_reset_type": "ForceOff",
  "verified_power_state": "Off",
  "power_state_reads": 2,
  "last_error": null,
  "created_at": "2026-06-10T10:00:00Z",
  "updated_at": "2026-06-10T10:00:42Z"
}
```

В etcd нельзя записывать:

- BMC password;
- Redfish session token;
- `X-Auth-Token`;
- HTTP Authorization header;
- полный secret reference, если он раскрывает секрет.

## Настройки до деплоя

Эти настройки должны быть определены до включения fencing workflow в production.
Их изменение обычно требует config deployment и рестарта `masakari-engine`.

### 1. etcd backend

Fencing требует доступный etcd backend. URL берется в таком порядке:

1. `[redfish_fencing] etcd_backend_url`;
2. `[staged_recovery] etcd_backend_url`;
3. `[coordination] backend_url`.

Минимальный вариант:

```ini
[coordination]
backend_url = etcd3+http://etcd.example.internal:2379
```

TLS-вариант:

```ini
[coordination]
backend_url = etcd3+https://etcd.example.internal:2379

[staged_recovery]
etcd_ca_cert = /etc/ssl/certs/etcd-ca.pem
etcd_cert_file = /etc/masakari/etcd-client.pem
etcd_key_file = /etc/masakari/etcd-client-key.pem
```

Если для fencing нужен отдельный etcd endpoint:

```ini
[redfish_fencing]
etcd_backend_url = etcd3+https://etcd-fencing.example.internal:2379
```

До деплоя нужно проверить:

- все `masakari-engine` видят один и тот же etcd cluster;
- etcd доступен из контейнера или host namespace, где работает engine;
- TLS certificates разложены на все engine nodes;
- prefix не конфликтует с другим окружением.

Не меняйте `etcd_prefix` после начала эксплуатации без миграции или осознанной
очистки state: старые proofs и locks останутся под старым prefix.

### 2. `[redfish_fencing]`

Базовая production-настройка:

```ini
[redfish_fencing]
enabled = true
hosts_config_path = /etc/masakari/redfish-fencing-hosts.yaml
allow_insecure_inline_password = false
etcd_prefix = /masakari/redfish-fencing/v1
etcd_timeout = 5

reset_type = ForceOff
expected_power_state = Off
power_off_timeout = 180
poll_interval = 5
stable_power_state_reads = 2
connect_timeout = 5
read_timeout = 30
max_attempts = 3
retry_interval = 5
lock_ttl = 300

multi_host_batch_window = 30
max_auto_fence_hosts_per_segment = 1
min_surviving_compute_hosts = 1
```

Практические правила:

- `enabled` включать только после готовности BMC mapping и etcd.
- `reset_type` должен оставаться `ForceOff`.
- `expected_power_state` должен оставаться `Off`.
- `stable_power_state_reads = 2` лучше не уменьшать без причины: это защита от
  устаревших или нестабильных ответов BMC.
- `power_off_timeout` должен покрывать самый медленный BMC в окружении.
- `lock_ttl` должен быть больше максимального ожидаемого времени fencing одного
  segment.
- `max_auto_fence_hosts_per_segment` начинать с `1`; увеличивать только после
  тестов multi-host failure.
- `min_surviving_compute_hosts` должен отражать минимальное число hosts, на
  которое реально можно эвакуировать workload.

### 3. Redfish host mapping

Файл задается параметром:

```ini
[redfish_fencing]
hosts_config_path = /etc/masakari/redfish-fencing-hosts.yaml
```

Пример:

```yaml
schema_version: 1

defaults:
  scheme: https
  port: 443
  base_uri: /redfish/v1
  auth_type: session
  tls_verify: true
  ca_cert: /etc/masakari/certs/bmc-ca.pem

hosts:
  compute-01:
    address: 10.10.20.101
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password_file: /etc/masakari/secrets/compute-01-bmc.password
    expected_serial_number: ABC12345

  compute-02:
    address: 10.10.20.102
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password_file: /etc/masakari/secrets/compute-02-bmc.password
    expected_serial_number: DEF67890
```

Обязательные поля на каждый host:

| Поле | Назначение |
|---|---|
| `address` | IP/FQDN BMC endpoint. |
| `systems_uri` | Redfish ComputerSystem URI конкретного сервера. |
| `username` | BMC account для Masakari fencing. |
| `password_file` или `password` | Источник пароля. В production использовать `password_file`. |

Рекомендуемые identity guards:

| Поле | Почему важно |
|---|---|
| `expected_system_uuid` | Защита от неверного BMC mapping. |
| `expected_serial_number` | Практичный guard для iDRAC/iLO/XCC inventory. |
| `expected_asset_tag` | Дополнительная проверка, если asset tags ведутся аккуратно. |
| `expected_manufacturer` | Sanity check против неверного endpoint. |
| `expected_model` | Sanity check против неверного endpoint. |

До деплоя нужно проверить:

- Nova/Masakari host name точно совпадает с ключом в `hosts`;
- `systems_uri` указывает на правильный physical server;
- BMC account имеет права читать ComputerSystem и выполнять Reset;
- BMC password files имеют права `0400` или эквивалентные;
- inline `password` не используется в production;
- TLS включен, CA доступен на всех `masakari-engine`;
- файл mapping одинаковый на всех engine nodes.

### 4. `[staged_recovery]`

Fencing сам не запускает ВМ после evacuation. Для безопасного recovery должен
быть включен staged recovery:

```ini
[staged_recovery]
enabled = true
state_backend = etcd
etcd_prefix = /masakari/staged-recovery/v1
max_parallel_starts_per_host = 2
start_timeout = 900
batch_delay = 30
slot_lease_ttl = 990
slot_retry_interval = 5
nova_evacuate_microversion = 2.95
start_only_originally_active = true
reconcile_interval = 60
stale_recovery_timeout = 300
```

Правила:

- `nova_evacuate_microversion` должен быть `2.95` или выше, чтобы evacuation
  оставлял ВМ stopped.
- `slot_lease_ttl` должен быть не меньше
  `start_timeout + batch_delay + safety margin`.
- `start_only_originally_active = true` нужен, чтобы ВМ, которые до отказа были
  `SHUTOFF`, не стартовали автоматически.

### 5. Recovery flow config

До деплоя нужно заменить штатный host failure workflow на fencing + staged
workflow. Пример находится в:

```text
etc/masakari/masakari-redfish-fencing-methods.conf
```

В deployment-системе этот config должен попасть туда, откуда Masakari читает
`[taskflow_driver_recovery_flows]`.

После изменения workflow нужно перезапустить `masakari-engine`, потому что
engine должен перечитать task list и oslo config.

### 6. BMC и сеть управления

До включения автоматического fencing нужно подготовить инфраструктуру:

- management network от `masakari-engine` до всех BMC;
- firewall rules к TCP/443 или другому Redfish port;
- отдельный BMC account `masakari-fencer` или аналог;
- минимальные права account: read ComputerSystem и power reset;
- TLS certificates BMC или internal CA;
- процедура ручного возврата host после fencing.

Не включайте automatic fencing, пока нет проверенной процедуры для случая,
когда BMC выключил host, а оператору нужно вернуть его в эксплуатацию.

## Настройки после деплоя

После деплоя параметры делятся на три группы:

1. runtime-настройки, которые можно менять без рестарта;
2. file-based данные, которые новые tasks читают при следующем запуске;
3. static oslo config, который требует reconfigure/restart.

### Можно менять без рестарта

#### Staged recovery start limit

Лимит одновременных стартов на destination host можно менять через runtime
override в etcd:

```bash
masakari-manage staged_recovery get_start_limit
masakari-manage staged_recovery set_start_limit 3
masakari-manage staged_recovery clear_start_limit
```

Через REST API:

```http
GET    /v1/{project_id}/staged-recovery/start-limit
PUT    /v1/{project_id}/staged-recovery/start-limit
DELETE /v1/{project_id}/staged-recovery/start-limit
```

Эффект:

- новые start-slot allocations видят новый лимит;
- уже стартующие ВМ не останавливаются;
- существующие leases живут до release или TTL expiration;
- уменьшение лимита начинает ограничивать только новые slots.

#### BMC password rotation

Если mapping использует `password_file`, пароль можно заменить в файле секрета:

```text
/etc/masakari/secrets/compute-01-bmc.password
```

Новые fencing tasks прочитают файл при следующем запуске. Практически важно
разложить обновленный secret на все `masakari-engine` nodes до следующего
recovery.

Не меняйте пароль во время активного fencing для этого же host: task могла уже
создать session или прочитать старое значение.

### Можно менять как file-based данные

#### Redfish host mapping

`hosts_config_path` читается при загрузке host config внутри fencing task.
Поэтому изменение содержимого YAML-файла применяется к новым fencing tasks без
изменения process config.

Это удобно для:

- добавления нового compute host;
- изменения BMC address после замены hardware;
- уточнения `expected_serial_number`;
- переключения `systems_uri`;
- добавления `ca_cert` для конкретного BMC.

Практический порядок:

1. Обновить YAML в deployment inventory.
2. Разложить файл на все `masakari-engine` nodes.
3. Проверить permissions и владельца.
4. Проверить Redfish доступность вручную.
5. Не запускать host recovery, пока файл не синхронизирован на всех engines.

Если меняется сам путь `hosts_config_path`, это уже static oslo config и нужен
restart.

### Требует reconfigure/restart

Эти параметры читаются из oslo config и должны меняться через обычный deployment
цикл с рестартом `masakari-engine`:

| Параметр | Почему нужен рестарт |
|---|---|
| `[redfish_fencing] enabled` | Engine должен перечитать, включены ли fencing tasks. |
| `hosts_config_path` | Путь к YAML является process config. |
| `allow_insecure_inline_password` | Политика безопасности должна быть одинаковой на всех engines. |
| `etcd_backend_url` | Меняет backend state/locks. |
| `etcd_prefix` | Меняет namespace proof и locks. |
| `etcd_timeout` | Timeout клиента создается из config. |
| `reset_type` | Safety-critical параметр; должен быть одинаковым в кластере. |
| `expected_power_state` | Safety-critical параметр; должен быть одинаковым в кластере. |
| `power_off_timeout` | Меняет поведение polling. |
| `poll_interval` | Меняет частоту polling BMC. |
| `stable_power_state_reads` | Меняет строгость proof. |
| `connect_timeout` | Меняет HTTP client behavior. |
| `read_timeout` | Меняет HTTP client behavior. |
| `max_attempts` | Меняет retry behavior. |
| `retry_interval` | Меняет retry behavior. |
| `lock_ttl` | Меняет TTL для segment/host locks. |
| `multi_host_batch_window` | Меняет grouping host failures. |
| `max_auto_fence_hosts_per_segment` | Safety threshold. |
| `min_surviving_compute_hosts` | Safety threshold. |
| `[taskflow_driver_recovery_flows] ...` | Меняет состав TaskFlow tasks. |
| `[staged_recovery] nova_evacuate_microversion` | Меняет Nova API behavior. |
| `[staged_recovery] start_timeout` | Меняет ожидание ACTIVE. |
| `[staged_recovery] batch_delay` | Меняет release start slot. |
| `[staged_recovery] slot_lease_ttl` | Меняет TTL leases. |
| `[coordination] backend_url` | Меняет distributed coordination backend. |

Порядок изменения static config:

1. Убедиться, что нет active host recovery.
2. Обновить deployment config.
3. Разложить config на все nodes.
4. Перезапустить все `masakari-engine`.
5. Проверить, что все engines стартовали с одинаковыми значениями.
6. Проверить доступность etcd и BMC.
7. Провести controlled recovery test или dry operational check.

## Когда менять конкретные настройки

### До первого production-деплоя

Обязательно настроить:

- `[coordination] backend_url` или `[redfish_fencing] etcd_backend_url`;
- `[redfish_fencing] enabled`;
- `[redfish_fencing] hosts_config_path`;
- Redfish host mapping для всех compute hosts;
- BMC credentials и password files;
- TLS CA для BMC;
- `max_auto_fence_hosts_per_segment`;
- `min_surviving_compute_hosts`;
- recovery flow с fencing tasks;
- `[staged_recovery] enabled`;
- `nova_evacuate_microversion = 2.95`;
- `max_parallel_starts_per_host`.

До первого production-деплоя также нужно проверить один host в maintenance:

1. Redfish `GET /redfish/v1` доступен.
2. Redfish `GET systems_uri` возвращает правильный serial/UUID.
3. BMC account может выполнить `ForceOff`.
4. Masakari сохраняет `FENCED` proof в etcd.
5. Evacuation не начинается без valid proof.
6. Staged start не превышает лимит на destination host.

### После добавления нового compute host

Настроить:

- host aggregate / failover segment в Masakari;
- Nova compute service;
- BMC account;
- secret file;
- запись в `redfish-fencing-hosts.yaml`;
- identity guards;
- доступность BMC из `masakari-engine`.

Restart не нужен, если путь `hosts_config_path` не менялся, но файл должен быть
разложен на все engine nodes.

### После замены hardware или BMC

Настроить:

- `address`, если изменился BMC IP/FQDN;
- `systems_uri`, если изменился Redfish ComputerSystem path;
- `expected_system_uuid`;
- `expected_serial_number`;
- `expected_asset_tag`;
- BMC TLS CA, если изменился certificate chain;
- password file, если создан новый account.

Не оставляйте старые identity guards: они должны блокировать неверный mapping.
Если после замены hardware guard mismatch не происходит, значит guard выбран
слишком слабый или не настроен.

### После изменения размера кластера

Пересмотреть:

- `max_auto_fence_hosts_per_segment`;
- `min_surviving_compute_hosts`;
- `max_parallel_starts_per_host`;
- `slot_lease_ttl`, если изменилась скорость старта workload;
- `multi_host_batch_window`, если failure detector часто присылает связанные
  events с задержкой.

Thresholds fencing требуют restart. Start limit можно менять runtime override.

### После проблем с BMC latency

Если BMC медленно отвечает, но надежно выключает host:

- увеличить `read_timeout`;
- увеличить `power_off_timeout`;
- возможно увеличить `retry_interval`;
- не уменьшать `stable_power_state_reads` первым действием.

Если BMC периодически возвращает stale `PowerState`, лучше увеличить
`stable_power_state_reads`, а не снижать строгость fencing.

### После boot storm или высокой нагрузки при recovery

Fencing параметры обычно не меняются. Меняется staged recovery:

```bash
masakari-manage staged_recovery set_start_limit 1
```

Если старт стал слишком медленным и инфраструктура выдерживает нагрузку:

```bash
masakari-manage staged_recovery set_start_limit 3
```

После стабилизации можно перенести новое значение в static config
`max_parallel_starts_per_host` и очистить runtime override.

## Диагностика

### Что смотреть в etcd

Fencing host state:

```text
/masakari/redfish-fencing/v1/hosts/<hostname>
```

Failure events:

```text
/masakari/redfish-fencing/v1/failures/<segment_uuid>/<event_id>/<hostname>
```

Locks:

```text
/masakari/redfish-fencing/v1/locks/segments/<segment_uuid>
/masakari/redfish-fencing/v1/locks/hosts/<hostname>
```

Staged recovery state:

```text
/masakari/staged-recovery/v1/notifications/<notification_uuid>/instances/<instance_uuid>
/masakari/staged-recovery/v1/start-leases/<encoded_dest_host>/<instance_uuid>
```

### Типовые причины `FENCE_FAILED`

| Симптом | Вероятная причина | Что проверить |
|---|---|---|
| `No Redfish host mapping found` | Host отсутствует в YAML. | Имя host в Nova и ключ в `hosts`. |
| Identity mismatch | Mapping указывает на другой physical server. | Serial/UUID/AssetTag и BMC address. |
| TLS verify error | Нет CA или неверный certificate chain. | `ca_cert`, системные CA, BMC cert. |
| HTTP 401/403 | Неверные credentials или мало прав. | BMC account и роль. |
| HTTP 404 на `systems_uri` | Неверный Redfish path. | `GET /redfish/v1/Systems`. |
| `ForceOff` not allowed | BMC не объявляет нужный ResetType. | Firmware, privileges, Redfish action. |
| Timeout waiting for `PowerState=Off` | Host не выключился или BMC медленный. | Power state в BMC UI, `power_off_timeout`. |
| Segment lock already held | Другой engine обрабатывает segment. | Lock key TTL и active engine logs. |
| Too many hosts in failure set | Сработал safety threshold. | `max_auto_fence_hosts_per_segment`. |

### Проверки после деплоя

Минимальный operational check:

1. На каждом engine node существует `redfish-fencing-hosts.yaml`.
2. Secret files читаются пользователем процесса Masakari.
3. `masakari-engine` стартует без oslo config ошибок.
4. etcd status доступен из engine.
5. Для тестового host Redfish service root доступен.
6. `GET systems_uri` возвращает ожидаемый serial/UUID.
7. В controlled failure `FENCED` появляется до `EVACUATING`.
8. Без `FENCED` proof evacuation не начинается.

## Fail-closed правила

Workflow должен остановиться до evacuation, если:

- etcd недоступен;
- mapping для host отсутствует;
- YAML invalid;
- BMC недоступен;
- TLS verification не проходит;
- credentials не работают;
- identity guard не совпал;
- Redfish не поддерживает `ForceOff`;
- `PowerState=Off` не подтвержден за timeout;
- exceeded `max_auto_fence_hosts_per_segment`;
- surviving hosts меньше `min_surviving_compute_hosts`;
- fencing proof отсутствует или невалиден.

Это ожидаемое поведение. Для fencing лучше остановить recovery и потребовать
оператора, чем эвакуировать ВМ без доказанного отключения failed host.

## Короткая матрица настроек

| Когда | Что настраивается | Где | Рестарт |
|---|---|---|---|
| До деплоя | etcd endpoint | `[coordination]` / `[redfish_fencing]` | Да |
| До деплоя | fencing enablement | `[redfish_fencing] enabled` | Да |
| До деплоя | recovery flow | `[taskflow_driver_recovery_flows]` | Да |
| До деплоя | Redfish timeouts/retries | `[redfish_fencing]` | Да |
| До деплоя | safety thresholds | `[redfish_fencing]` | Да |
| До деплоя | BMC mapping path | `hosts_config_path` | Да |
| До деплоя и при росте кластера | BMC mapping content | YAML file | Нет для новых tasks |
| До деплоя и при ротации | BMC password | `password_file` | Нет для новых tasks |
| До деплоя | Nova evacuate microversion | `[staged_recovery]` | Да |
| После деплоя | start concurrency runtime limit | `masakari-manage` / REST | Нет |
| После деплоя | static default start limit | `[staged_recovery]` | Да |
| После деплоя | etcd prefix | `[redfish_fencing]` / `[staged_recovery]` | Да, плюс migration/cleanup |
| После деплоя | TLS CA path in mapping | YAML file | Нет для новых tasks |
| После деплоя | TLS cert files for etcd | `[staged_recovery]` paths | Да |

## Безопасный порядок включения в production

1. Подготовить etcd и coordination backend.
2. Создать BMC account на всех compute hosts.
3. Разложить BMC password files и CA certificates.
4. Создать `redfish-fencing-hosts.yaml` со строгими identity guards.
5. Включить `[redfish_fencing]`, но сначала протестировать в staging или на
   maintenance host.
6. Включить `[staged_recovery]` с консервативным
   `max_parallel_starts_per_host = 1` или `2`.
7. Настроить fencing + staged recovery flow.
8. Перезапустить все `masakari-engine`.
9. Проверить config, logs, etcd и Redfish доступность.
10. Провести controlled host failure test.
11. После успешного теста увеличить start limit только если infrastructure
    выдерживает нагрузку.

## Операторская процедура при failed fencing

Если host recovery остановился на `FENCE_FAILED`:

1. Не запускать evacuation вручную, пока не понятно состояние failed host.
2. Проверить Masakari logs и host state в etcd.
3. Проверить BMC доступность и фактический power state.
4. Если host включен, выключить его через out-of-band BMC или физически.
5. Убедиться, что ВМ на failed host не выполняются.
6. Исправить mapping/credentials/TLS/timeout, если причина конфигурационная.
7. Повторить recovery штатным механизмом Masakari.

Ручной override fencing proof должен быть отдельной audited процедурой. Без
такой процедуры автоматический workflow должен принимать только Redfish proof
`FENCED` + `verified_power_state=Off`.
