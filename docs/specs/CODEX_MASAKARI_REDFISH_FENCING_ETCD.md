# CODEX TASK: Redfish fencing gate for Masakari host recovery with etcd state

## Назначение

Этот Markdown — техническое задание для Codex/AI coding agent, работающего в fork-е `openstack/masakari` ветки `stable/2025.1` / `2025.1`.

Нужно добавить **Redfish fencing gate** в host failure recovery workflow Masakari, который уже использует staged recovery и etcd state store.

Цель:

```text
Перед Nova evacuate гарантированно выключить или изолировать failed compute host через Redfish,
записать verified fencing proof в etcd и не допускать evacuation без подтвержденного PowerState=Off.
```

Ключевое требование по split-brain:

```text
No verified Redfish fencing proof -> no Nova evacuate.
```

Существующий staged recovery решает boot storm после evacuation. Эта задача решает более раннюю фазу: **fence-before-evacuate**.

---

## Что уже есть в fork-е

В fork-е уже реализован staged recovery на etcd:

```text
Masakari TaskFlow tasks
  -> Nova evacuate microversion >= 2.95
  -> etcd persistent per-VM state
  -> etcd TTL start leases
  -> batched Nova server start
```

Уже есть или ожидаются файлы:

```text
masakari/engine/drivers/taskflow/staged_host_failure.py
masakari/engine/drivers/taskflow/staged_state_etcd.py
masakari/engine/drivers/taskflow/staged_limiter.py
masakari/conf/staged_recovery.py
```

Эта задача добавляет отдельный слой fencing:

```text
Masakari host failure notification
  -> disable failed compute service
  -> register failure event in etcd
  -> acquire segment recovery lock
  -> collect multi-host failure set
  -> disable all hosts in failure set
  -> Redfish ForceOff all hosts in failure set
  -> verify PowerState=Off for all hosts in failure set
  -> prepare VMove records
  -> evacuate-to-stopped
  -> release segment recovery lock
  -> batched start with existing staged limiter
```

---

## Redfish-параметры: что обязательно передать

### Минимальный набор на каждый compute host

Для каждого compute-гипервизора нужен mapping `nova compute hostname -> Redfish BMC endpoint`.

Обязательные параметры:

| Параметр | Пример | Назначение |
|---|---:|---|
| `hostname` | `compute-01` | Имя compute host в Nova/Masakari. Должно совпадать с `hypervisor_hostname` / Masakari host name. |
| `address` | `10.10.20.101` | IP/FQDN BMC/iLO/iDRAC/XCC/Redfish контроллера. |
| `scheme` | `https` | Обычно `https`; `http` допустим только для lab/test. |
| `port` | `443` | TCP port Redfish service. |
| `base_uri` | `/redfish/v1` | Redfish service root/base URI. |
| `systems_uri` | `/redfish/v1/Systems/1` | URI конкретного ComputerSystem, который соответствует compute host. |
| `auth_type` | `session` | `session` или `basic`. Для production предпочтительно `session`. |
| `username` | `masakari-fencer` | BMC account с минимальными правами на чтение ComputerSystem и power reset. |
| `password_secret_ref` / `password_file` | `barbican://...` | Секрет для BMC account. Не хранить пароль в etcd. |
| `tls_verify` | `true` | Проверять TLS-сертификат BMC. |
| `ca_cert` | `/etc/masakari/certs/bmc-ca.pem` | CA для BMC TLS, если `tls_verify=true` и CA не системный. |

Обязательные runtime-параметры fencing:

| Параметр | Значение | Назначение |
|---|---:|---|
| `reset_type` | `ForceOff` | Жесткое выключение host. Не использовать graceful shutdown. |
| `expected_power_state` | `Off` | Состояние, которое считается доказательством fencing. |
| `power_off_timeout` | `180` | Сколько ждать перехода BMC `PowerState` в `Off`. |
| `poll_interval` | `5` | Интервал polling `GET systems_uri`. |
| `connect_timeout` | `5` | Timeout TCP/TLS connect к BMC. |
| `read_timeout` | `30` | Timeout чтения HTTP response от BMC. |
| `max_attempts` | `3` | Количество попыток Redfish operation перед fail-closed. |
| `retry_interval` | `5` | Пауза между попытками. |

### Настоятельно рекомендуемые anti-miswire параметры

Чтобы не выключить не тот сервер из-за ошибки CMDB/inventory, поддержать optional guards:

| Параметр | Пример | Назначение |
|---|---:|---|
| `expected_system_uuid` | `4c4c4544-0031-...` | Проверка `UUID` из Redfish ComputerSystem. |
| `expected_serial_number` | `ABC12345` | Проверка `SerialNumber` из ComputerSystem. |
| `expected_asset_tag` | `rack12-u07` | Проверка `AssetTag`, если используется. |
| `expected_manufacturer` | `Dell Inc.` | Optional sanity check. |
| `expected_model` | `PowerEdge R750` | Optional sanity check. |

Правило:

```text
Если задан любой expected_* guard и Redfish возвращает другое значение,
не выполнять ForceOff, записать FENCE_FAILED, завершить workflow fail-closed.
```

---

## Direct Redfish API: какие HTTP-запросы выполняет task

### 1. Получить Service Root

```http
GET https://<address>:<port>/redfish/v1
Accept: application/json
```

Используется для проверки доступности Redfish service и, при необходимости, discovery URI `Systems` и `SessionService`.

### 2. Создать session, если `auth_type=session`

```http
POST https://<address>:<port>/redfish/v1/SessionService/Sessions
Content-Type: application/json
Accept: application/json

{
  "UserName": "<username>",
  "Password": "<password>"
}
```

Из response headers взять:

```text
X-Auth-Token: <token>
Location: /redfish/v1/SessionService/Sessions/<id>
```

Все следующие запросы выполняются с header:

```http
X-Auth-Token: <token>
```

В конце Redfish client должен удалить session:

```http
DELETE https://<address>:<port><session_location>
X-Auth-Token: <token>
```

### 3. Прочитать ComputerSystem

```http
GET https://<address>:<port><systems_uri>
Accept: application/json
```

Из ответа использовать:

```json
{
  "PowerState": "On",
  "UUID": "...",
  "SerialNumber": "...",
  "Actions": {
    "#ComputerSystem.Reset": {
      "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
      "ResetType@Redfish.AllowableValues": [
        "On",
        "ForceOff",
        "GracefulShutdown",
        "GracefulRestart",
        "ForceRestart",
        "PowerCycle"
      ]
    }
  }
}
```

Правила:

```text
- Если PowerState == Off, host уже считается fenced только после guard validation.
- Если PowerState == On, нужно выполнить ForceOff.
- Если ResetType@Redfish.AllowableValues есть и там нет ForceOff, fail closed.
- Если Actions.#ComputerSystem.Reset.target есть, использовать именно его.
- Если target отсутствует, можно fallback на <systems_uri>/Actions/ComputerSystem.Reset,
  но это должно логироваться как compatibility fallback.
```

### 4. Выполнить hard power off

```http
POST https://<address>:<port><reset_target>
Content-Type: application/json
Accept: application/json

{
  "ResetType": "ForceOff"
}
```

Запрещено использовать для split-brain fencing:

```text
GracefulShutdown
GracefulRestart
ForceRestart
PowerCycle
PushPowerButton
reboot
```

Для anti-split-brain default должен быть именно:

```json
{"ResetType": "ForceOff"}
```

### 5. Polling до `PowerState=Off`

```text
until timeout:
  GET <systems_uri>
  if PowerState == Off for required stable reads:
      mark FENCED
      return success
  sleep poll_interval

on timeout:
  mark FENCE_FAILED
  raise HostRecoveryFailureException
```

Рекомендуемый default:

```ini
power_off_timeout = 180
poll_interval = 5
stable_power_state_reads = 2
```

`stable_power_state_reads = 2` защищает от случайного кратковременного/устаревшего ответа BMC.

---

## Вариант через `fence_redfish`: mapping параметров

Основная реализация должна быть direct Redfish client на Python. Но можно добавить optional driver `fence_redfish`, чтобы использовать установленный fence agent.

Mapping config -> `fence_redfish`:

| Наш параметр | `fence_redfish` option | Пример |
|---|---|---|
| `reset_type=ForceOff` | `--action off` | `--action off` |
| `address` | `--ip` | `--ip 10.10.20.101` |
| `port` | `--ipport` | `--ipport 443` |
| `username` | `--username` | `--username masakari-fencer` |
| `password_file` | `--password-script` | `--password-script /usr/local/bin/get-bmc-pass compute-01` |
| `base_uri` | `--redfish-uri` | `--redfish-uri /redfish/v1` |
| `systems_uri` | `--systems-uri` | `--systems-uri /redfish/v1/Systems/1` |
| `tls_verify=true` | `--ssl-secure` | `--ssl-secure` |
| `tls_verify=false` | `--ssl-insecure` | `--ssl-insecure` |
| `power_off_timeout` | `--power-timeout` | `--power-timeout 180` |
| `poll_interval` | `--stonith-status-sleep` | `--stonith-status-sleep 5` |
| `power_wait` | `--power-wait` | `--power-wait 0` |
| `login_timeout` | `--login-timeout` | `--login-timeout 5` |
| `shell_timeout` | `--shell-timeout` | `--shell-timeout 30` |

Пример команды для ручного теста:

```bash
fence_redfish \
  --action off \
  --ip 10.10.20.101 \
  --ipport 443 \
  --username masakari-fencer \
  --password-script /usr/local/bin/get-bmc-pass-compute-01 \
  --redfish-uri /redfish/v1 \
  --systems-uri /redfish/v1/Systems/1 \
  --ssl-secure \
  --power-timeout 180 \
  --stonith-status-sleep 5
```

Правило безопасности:

```text
Не передавать BMC password через command-line --password в production,
потому что пароль может попасть в process list, audit/logs или crash dump.
Использовать password_script, файл с правами 0400, Barbican/Vault или direct Python client.
```

---

## Новая конфигурационная группа `[redfish_fencing]`

Создать:

```text
masakari/conf/redfish_fencing.py
```

Пример runtime config:

```ini
[redfish_fencing]
enabled = true

# driver: direct Python Redfish API client or external fence_redfish agent.
driver = redfish

# Host-to-BMC mapping file. Do not store credentials in etcd.
hosts_file = /etc/masakari/redfish-hosts.yaml

# etcd state is stored under the same staged_recovery prefix by default.
etcd_prefix = /masakari/staged-recovery/v1

# Multi-host failure coordination.
multi_host_batch_window = 20
max_auto_fence_hosts_per_segment = 2
min_surviving_compute_hosts = 2
segment_lock_ttl = 600
host_fence_lock_ttl = 300

# Redfish behavior.
auth_type = session
reset_type = ForceOff
expected_power_state = Off
power_off_timeout = 180
poll_interval = 5
stable_power_state_reads = 2
connect_timeout = 5
read_timeout = 30
max_attempts = 3
retry_interval = 5

# TLS/security defaults. Per-host mapping may override ca_cert only if needed.
tls_verify = true
ca_cert = /etc/masakari/certs/bmc-ca.pem

# Fail-closed behavior.
fail_closed_on_etcd_unavailable = true
fail_closed_on_bmc_unreachable = true
fail_closed_on_tls_error = true
fail_closed_on_mapping_mismatch = true
fail_closed_on_unsupported_reset_type = true
fail_closed_on_threshold_exceeded = true

# Nova integration after fencing.
disable_failure_set_before_fencing = true
set_forced_down_after_fencing = true
clear_forced_down_on_manual_return = false

# Optional support for external fence_redfish.
fence_redfish_path = /usr/sbin/fence_redfish
fence_redfish_use_password_script = true
```

### Oslo config opts skeleton

```python
from oslo_config import cfg

redfish_fencing_group = cfg.OptGroup(
    'redfish_fencing',
    title='Redfish fencing options',
    help='Options for Redfish-based compute host fencing before evacuation.',
)

redfish_fencing_opts = [
    cfg.BoolOpt('enabled', default=False,
                help='Enable Redfish fencing gate for host failure recovery.'),
    cfg.StrOpt('driver', default='redfish', choices=['redfish', 'fence_redfish'],
               help='Fencing backend driver.'),
    cfg.StrOpt('hosts_file', default='/etc/masakari/redfish-hosts.yaml',
               help='YAML mapping of Nova compute hostnames to Redfish BMC endpoints.'),
    cfg.StrOpt('etcd_prefix', default='/masakari/staged-recovery/v1',
               help='Key prefix for Redfish fencing state in etcd.'),
    cfg.IntOpt('multi_host_batch_window', default=20, min=0,
               help='Seconds to collect near-simultaneous host failures in one segment.'),
    cfg.IntOpt('max_auto_fence_hosts_per_segment', default=2, min=1,
               help='Maximum failed hosts per segment eligible for automatic fencing.'),
    cfg.IntOpt('min_surviving_compute_hosts', default=2, min=0,
               help='Minimum non-failed compute hosts required to continue automatic recovery.'),
    cfg.IntOpt('segment_lock_ttl', default=600, min=60,
               help='TTL for segment recovery lock in etcd.'),
    cfg.IntOpt('host_fence_lock_ttl', default=300, min=60,
               help='TTL for per-host fencing lock in etcd.'),
    cfg.StrOpt('auth_type', default='session', choices=['session', 'basic'],
               help='Default Redfish authentication type.'),
    cfg.StrOpt('reset_type', default='ForceOff', choices=['ForceOff'],
               help='Redfish ResetType for anti-split-brain fencing. Must be ForceOff.'),
    cfg.StrOpt('expected_power_state', default='Off', choices=['Off'],
               help='PowerState required before evacuation can proceed.'),
    cfg.IntOpt('power_off_timeout', default=180, min=10,
               help='Seconds to wait until Redfish PowerState becomes Off.'),
    cfg.IntOpt('poll_interval', default=5, min=1,
               help='Seconds between Redfish PowerState polls.'),
    cfg.IntOpt('stable_power_state_reads', default=2, min=1,
               help='Number of consecutive Off reads required to mark a host fenced.'),
    cfg.IntOpt('connect_timeout', default=5, min=1,
               help='TCP/TLS connection timeout for Redfish requests.'),
    cfg.IntOpt('read_timeout', default=30, min=1,
               help='HTTP read timeout for Redfish requests.'),
    cfg.IntOpt('max_attempts', default=3, min=1,
               help='Maximum attempts for Redfish fencing operation.'),
    cfg.IntOpt('retry_interval', default=5, min=0,
               help='Seconds between Redfish retry attempts.'),
    cfg.BoolOpt('tls_verify', default=True,
                help='Verify BMC TLS certificates.'),
    cfg.StrOpt('ca_cert', default=None,
               help='CA certificate bundle for BMC TLS verification.'),
    cfg.BoolOpt('fail_closed_on_etcd_unavailable', default=True,
                help='Fail recovery if etcd is unavailable.'),
    cfg.BoolOpt('fail_closed_on_bmc_unreachable', default=True,
                help='Fail recovery if BMC cannot be reached.'),
    cfg.BoolOpt('fail_closed_on_tls_error', default=True,
                help='Fail recovery on BMC TLS verification error.'),
    cfg.BoolOpt('fail_closed_on_mapping_mismatch', default=True,
                help='Fail recovery if expected_* guard does not match Redfish system data.'),
    cfg.BoolOpt('fail_closed_on_unsupported_reset_type', default=True,
                help='Fail recovery if Redfish action does not allow ForceOff.'),
    cfg.BoolOpt('fail_closed_on_threshold_exceeded', default=True,
                help='Fail recovery when multi-host failure threshold is exceeded.'),
    cfg.BoolOpt('disable_failure_set_before_fencing', default=True,
                help='Disable nova-compute services for all hosts in current failure set before fencing.'),
    cfg.BoolOpt('set_forced_down_after_fencing', default=True,
                help='Set Nova service forced_down after verified fencing.'),
    cfg.BoolOpt('clear_forced_down_on_manual_return', default=False,
                help='Do not automatically clear forced_down when a host returns.'),
    cfg.StrOpt('fence_redfish_path', default='/usr/sbin/fence_redfish',
               help='Path to external fence_redfish agent when driver=fence_redfish.'),
    cfg.BoolOpt('fence_redfish_use_password_script', default=True,
                help='Use password-script instead of command-line password for fence_redfish.'),
]


def register_opts(conf):
    conf.register_group(redfish_fencing_group)
    conf.register_opts(redfish_fencing_opts, group=redfish_fencing_group)


def list_opts():
    return {
        redfish_fencing_group: redfish_fencing_opts,
    }
```

Register this module using the same pattern as existing `masakari/conf/*.py` modules.

---

## Host mapping file `/etc/masakari/redfish-hosts.yaml`

### YAML schema

```yaml
schema_version: 1

defaults:
  scheme: https
  port: 443
  base_uri: /redfish/v1
  auth_type: session
  tls_verify: true
  ca_cert: /etc/masakari/certs/bmc-ca.pem
  reset_type: ForceOff
  expected_power_state: Off

hosts:
  compute-01:
    address: 10.10.20.101
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password_secret_ref: barbican://5b7f2a4f-1111-2222-3333-abcdefabcdef
    expected_system_uuid: 4c4c4544-0031-4210-8053-b6c04f4d4d33
    expected_serial_number: ABC12345

  compute-02:
    address: 10.10.20.102
    systems_uri: /redfish/v1/Systems/System.Embedded.1
    username: masakari-fencer
    password_file: /etc/masakari/secrets/bmc-compute-02.password
    expected_serial_number: XYZ98765

  compute-03:
    address: bmc-compute-03.example.internal
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    # Not recommended for production; allowed only for dev/test if implemented.
    password: change-me
    tls_verify: false
```

### Validation rules

Codex must implement YAML validation:

```text
- `hosts` must be a mapping.
- Every host must have `address`, `systems_uri`, `username` and exactly one password source.
- Supported password sources: password_secret_ref, password_file, password.
- `password` inline is allowed only if CONF.debug or explicit allow_insecure_inline_password=true.
- `scheme` must be http or https.
- `tls_verify=false` must log WARNING.
- `systems_uri` must start with /redfish/v1/Systems/.
- Unknown host -> fail closed.
- Invalid YAML -> fail closed.
```

Do not store this host mapping in etcd unless secrets are removed. etcd should store only state/proof, not BMC credentials.

---

## etcd key model for Redfish fencing

Use the same base prefix as staged recovery unless overridden:

```text
<etcd_prefix>/fencing/...
```

Default:

```text
/masakari/staged-recovery/v1/fencing
```

### Host failure event

```text
<etcd_prefix>/fencing/segments/<segment_uuid>/events/<event_id>/hosts/<hostname>
```

Example value:

```json
{
  "schema_version": 1,
  "event_id": "2026-06-09T20:15:00Z-segment-a",
  "notification_uuid": "7ce6d9a3-1111-2222-3333-abcdefabcdef",
  "segment_uuid": "segment-a",
  "hostname": "compute-01",
  "state": "DETECTED",
  "source": "masakari-hostmonitor",
  "created_at": "2026-06-09T20:15:00Z",
  "updated_at": "2026-06-09T20:15:00Z"
}
```

### Current fencing state per host

```text
<etcd_prefix>/fencing/hosts/<hostname>
```

Example value after success:

```json
{
  "schema_version": 1,
  "event_id": "2026-06-09T20:15:00Z-segment-a",
  "notification_uuid": "7ce6d9a3-1111-2222-3333-abcdefabcdef",
  "segment_uuid": "segment-a",
  "hostname": "compute-01",
  "state": "FENCED",
  "method": "redfish",
  "bmc_address_hash": "sha256:...",
  "systems_uri": "/redfish/v1/Systems/1",
  "reset_target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
  "requested_reset_type": "ForceOff",
  "verified_power_state": "Off",
  "power_state_reads": 2,
  "expected_system_uuid": "4c4c4544-0031-4210-8053-b6c04f4d4d33",
  "observed_system_uuid": "4c4c4544-0031-4210-8053-b6c04f4d4d33",
  "expected_serial_number": "ABC12345",
  "observed_serial_number": "ABC12345",
  "attempt": 1,
  "owner": "masakari-engine-controller-1-12345",
  "fenced_at": "2026-06-09T20:15:42Z",
  "last_error": null,
  "created_at": "2026-06-09T20:15:01Z",
  "updated_at": "2026-06-09T20:15:42Z"
}
```

Do not write raw BMC password, session token, X-Auth-Token or full Authorization header to etcd.

### Locks

```text
<etcd_prefix>/fencing/locks/segments/<segment_uuid>/recovery
<etcd_prefix>/fencing/locks/hosts/<hostname>/fencing
```

Locks must be etcd TTL lease keys with keepalive while task is running.

---

## Fencing state machine

Use constants, not string literals scattered through code.

```python
FENCE_REQUIRED = 'FENCE_REQUIRED'
FENCING = 'FENCING'
FENCED = 'FENCED'
FENCE_FAILED = 'FENCE_FAILED'
FENCE_SKIPPED_MANUAL = 'FENCE_SKIPPED_MANUAL'
```

Transitions:

| Current | Condition | Next |
|---|---|---|
| missing | host is in failure_set | `FENCE_REQUIRED` |
| `FENCE_REQUIRED` | task acquired host fencing lock | `FENCING` |
| `FENCING` | Redfish already reports `PowerState=Off` and guards pass | `FENCED` |
| `FENCING` | ForceOff sent and polling confirms `PowerState=Off` | `FENCED` |
| `FENCING` | BMC unreachable / TLS error / timeout / guard mismatch | `FENCE_FAILED` |
| any non-final | operator manually marks fenced | `FENCE_SKIPPED_MANUAL` only if manual override is explicitly enabled |

For automatic recovery, only this is acceptable:

```text
state == FENCED and verified_power_state == Off
```

`FENCE_SKIPPED_MANUAL` must not automatically allow evacuation unless there is a separate explicit operator override command with audit fields.

---

## New files

Add:

```text
masakari/conf/redfish_fencing.py
masakari/engine/drivers/taskflow/redfish_fencing.py
masakari/engine/drivers/taskflow/fencing_state_etcd.py
masakari/engine/drivers/taskflow/fenced_host_failure.py
masakari/tests/unit/engine/drivers/taskflow/test_redfish_fencing.py
masakari/tests/unit/engine/drivers/taskflow/test_fencing_state_etcd.py
masakari/tests/unit/engine/drivers/taskflow/test_fenced_host_failure.py
etc/masakari/redfish-hosts.yaml.sample
etc/masakari/masakari-redfish-fencing-methods.conf
```

Modify:

```text
masakari/conf/__init__.py
masakari/conf/opts.py
setup.cfg
etc/masakari/masakari-staged-recovery-methods.conf
```

Do not modify:

```text
Nova scheduler
libvirt/QEMU directly
masakari-monitors
```

---

## New internal API: `RedfishFencingClient`

Create:

```text
masakari/engine/drivers/taskflow/redfish_fencing.py
```

### Public API

```python
class RedfishFencingClient(object):
    def __init__(self, host_config, conf, session=None):
        pass

    def assert_reachable(self):
        """GET service root and fail fast if Redfish endpoint is unavailable."""

    def get_system(self):
        """GET systems_uri and return parsed ComputerSystem JSON."""

    def validate_system_identity(self, system):
        """Validate expected_system_uuid/serial/model guards."""

    def get_power_state(self):
        """Return ComputerSystem PowerState."""

    def get_reset_target_and_allowable_values(self, system):
        """Return reset target URI and allowed ResetType values."""

    def force_off(self):
        """POST ComputerSystem.Reset with {'ResetType': 'ForceOff'}."""

    def wait_power_off(self):
        """Poll until PowerState is Off for stable_power_state_reads."""

    def fence_off(self):
        """Complete fencing operation: validate -> ForceOff if needed -> verify Off."""

    def close(self):
        """Delete Redfish session if session auth was used."""
```

### Requirements

```text
- Use requests.Session or existing project-approved HTTP client.
- Support Basic auth and Session auth.
- Verify TLS by default.
- Do not log passwords, tokens or Authorization headers.
- Include request id / notification_uuid / hostname in log context.
- Treat HTTP 401/403/404/409/5xx as fencing failure unless explicitly recoverable.
- Delete Redfish session in finally block.
```

---

## New internal API: `EtcdFencingStore`

Create:

```text
masakari/engine/drivers/taskflow/fencing_state_etcd.py
```

### Public API

```python
class EtcdFencingStore(object):
    def __init__(self, conf, owner, client=None):
        pass

    def assert_available(self):
        """Fail fast if etcd is unavailable."""

    def register_host_failure(self, segment_uuid, event_id, hostname, notification_uuid):
        """Create/update detected host failure event."""

    def list_recent_failures(self, segment_uuid, window_seconds):
        """Return host failures detected within batch window."""

    def acquire_segment_recovery_lock(self, segment_uuid, event_id, ttl):
        """Acquire etcd TTL lock for segment recovery."""

    def release_segment_recovery_lock(self, segment_uuid, lock):
        """Release segment recovery lock."""

    def acquire_host_fencing_lock(self, hostname, event_id, ttl):
        """Acquire etcd TTL lock for one host fencing operation."""

    def release_host_fencing_lock(self, hostname, lock):
        """Release host fencing lock."""

    def get_fencing_state(self, hostname):
        """Return current host fencing state or None."""

    def transition_fencing_state(self, hostname, expected_states, new_state, patch=None):
        """CAS transition for fencing state."""

    def mark_fenced(self, hostname, event_id, values):
        """Mark host as FENCED with verified_power_state=Off."""

    def mark_fence_failed(self, hostname, event_id, error, values=None):
        """Mark host as FENCE_FAILED."""

    def assert_hosts_fenced(self, hostnames):
        """Raise unless all hosts are FENCED with verified_power_state=Off."""
```

### CAS requirement

All updates to host fencing state must use etcd CAS/compare-and-replace. Do not blindly overwrite JSON state.

---

## New TaskFlow tasks

Create:

```text
masakari/engine/drivers/taskflow/fenced_host_failure.py
```

Implement:

```text
RegisterHostFailureEventTask
AcquireSegmentRecoveryLockTask
CollectFailureSetTask
DisableFailureSetTask
RedfishFenceFailureSetTask
AssertFailureSetFencedTask
ReleaseSegmentRecoveryLockTask
```

### Task: `RegisterHostFailureEventTask`

Requires:

```text
host_name
notification_uuid
```

Provides:

```text
event_id
segment_uuid
```

Algorithm:

```text
1. Assert redfish_fencing.enabled=true.
2. Assert etcd available.
3. Resolve segment_uuid for host_name.
4. Generate deterministic or timestamped event_id.
5. Register host failure event in etcd.
6. Return event_id and segment_uuid.
```

### Task: `AcquireSegmentRecoveryLockTask`

Requires:

```text
segment_uuid
event_id
```

Provides:

```text
segment_recovery_lock
```

Algorithm:

```text
1. Acquire etcd TTL lease lock for segment.
2. Start keepalive while TaskFlow is running.
3. If lock cannot be acquired, wait/retry until lock timeout or task timeout.
4. If etcd unavailable, fail closed.
```

`revert()` must release the lock if acquired.

### Task: `CollectFailureSetTask`

Requires:

```text
segment_uuid
event_id
host_name
```

Provides:

```text
failure_set
```

Algorithm:

```text
1. Sleep multi_host_batch_window seconds.
2. Read recent host failure events from etcd for segment_uuid.
3. failure_set = unique hosts from recent events plus current host_name.
4. Validate len(failure_set) <= max_auto_fence_hosts_per_segment.
5. Validate surviving host count >= min_surviving_compute_hosts.
6. Validate every host in failure_set has Redfish mapping.
7. On threshold/mapping failure, fail closed before evacuation.
```

### Task: `DisableFailureSetTask`

Requires:

```text
failure_set
```

Algorithm:

```text
1. For every host in failure_set:
   - disable nova-compute service if enabled;
   - optionally set forced_down before or after fencing depending local Nova behavior.
2. Do not fail open if disabling one host fails.
3. If Nova API unavailable, fail closed.
```

This task exists because one host notification must not allow Nova scheduler to use another host that is already in the same failure_set.

### Task: `RedfishFenceFailureSetTask`

Requires:

```text
failure_set
event_id
segment_uuid
notification_uuid
```

Provides:

```text
fenced_hosts
```

Algorithm:

```text
for host in failure_set:
    acquire host fencing lock
    current = get_fencing_state(host)

    if current is FENCED and verified_power_state == Off:
        add host to fenced_hosts
        continue

    transition host -> FENCING

    load host Redfish config from hosts_file
    create RedfishFencingClient

    try:
        system = client.get_system()
        client.validate_system_identity(system)

        power_state = system.PowerState
        if power_state == 'Off':
            mark_fenced(host, verified_power_state='Off')
            add host to fenced_hosts
            continue

        reset_target, allowable = client.get_reset_target_and_allowable_values(system)
        if allowable is not None and 'ForceOff' not in allowable:
            mark_fence_failed(host, 'ForceOff not supported')
            raise HostRecoveryFailureException

        client.force_off()
        client.wait_power_off()
        mark_fenced(host, verified_power_state='Off')
        add host to fenced_hosts

    except Exception as exc:
        mark_fence_failed(host, str(exc))
        raise HostRecoveryFailureException

    finally:
        client.close()
        release host fencing lock

assert all failure_set hosts are FENCED
return fenced_hosts
```

### Task: `AssertFailureSetFencedTask`

Requires:

```text
failure_set
```

Algorithm:

```text
1. Read etcd fencing state for every host.
2. Require state == FENCED.
3. Require verified_power_state == Off.
4. Require fenced_at exists.
5. If any host is not fenced, raise HostRecoveryFailureException.
```

This task is a final safety gate before any VMove/Nova evacuation task.

### Task: `ReleaseSegmentRecoveryLockTask`

Requires:

```text
segment_recovery_lock
```

Algorithm:

```text
Release segment lock after evacuate_to_stopped_task completes.
Do not hold this lock for batched_start_instances_task.
```

`revert()` should release the lock if possible.

---

## Recovery workflow config

Update or add sample:

```text
etc/masakari/masakari-redfish-fencing-methods.conf
```

### Auto recovery with staged start

```ini
[taskflow_driver_recovery_flows]

host_auto_failure_recovery_tasks = {
    'pre': [
        'disable_compute_service_task',
        'register_host_failure_event_task',
        'acquire_segment_recovery_lock_task',
        'collect_failure_set_task',
        'disable_failure_set_task',
        'redfish_fence_failure_set_task',
        'assert_failure_set_fenced_task'
    ],
    'main': [
        'prepare_HA_enabled_instances_task',
        'reconcile_staged_recovery_task',
        'evacuate_to_stopped_task',
        'release_segment_recovery_lock_task'
    ],
    'post': [
        'batched_start_instances_task'
    ]
}
```

### Reserved-host recovery with staged start

```ini
[taskflow_driver_recovery_flows]

host_rh_failure_recovery_tasks = {
    'pre': [
        'disable_compute_service_task',
        'register_host_failure_event_task',
        'acquire_segment_recovery_lock_task',
        'collect_failure_set_task',
        'disable_failure_set_task',
        'redfish_fence_failure_set_task',
        'assert_failure_set_fenced_task'
    ],
    'main': [
        'prepare_HA_enabled_instances_task',
        'reconcile_staged_recovery_task',
        'evacuate_to_stopped_task',
        'release_segment_recovery_lock_task'
    ],
    'post': [
        'batched_start_instances_task'
    ]
}
```

Important:

```text
release_segment_recovery_lock_task is placed after evacuate_to_stopped_task.
The lock must protect fencing + evacuation decision inside the segment.
The lock must not be held during batched_start_instances_task.
```

---

## Entry points

Update `setup.cfg`:

```ini
[entry_points]
masakari.task_flow.tasks =
    register_host_failure_event_task = masakari.engine.drivers.taskflow.fenced_host_failure:RegisterHostFailureEventTask
    acquire_segment_recovery_lock_task = masakari.engine.drivers.taskflow.fenced_host_failure:AcquireSegmentRecoveryLockTask
    collect_failure_set_task = masakari.engine.drivers.taskflow.fenced_host_failure:CollectFailureSetTask
    disable_failure_set_task = masakari.engine.drivers.taskflow.fenced_host_failure:DisableFailureSetTask
    redfish_fence_failure_set_task = masakari.engine.drivers.taskflow.fenced_host_failure:RedfishFenceFailureSetTask
    assert_failure_set_fenced_task = masakari.engine.drivers.taskflow.fenced_host_failure:AssertFailureSetFencedTask
    release_segment_recovery_lock_task = masakari.engine.drivers.taskflow.fenced_host_failure:ReleaseSegmentRecoveryLockTask
```

Keep existing staged recovery entry points:

```ini
    reconcile_staged_recovery_task = masakari.engine.drivers.taskflow.staged_host_failure:ReconcileStagedRecoveryTask
    evacuate_to_stopped_task = masakari.engine.drivers.taskflow.staged_host_failure:EvacuateToStoppedTask
    batched_start_instances_task = masakari.engine.drivers.taskflow.staged_host_failure:BatchedStartInstancesTask
```

---

## Multi-host failure behavior

### Scenario A: one failed host

```text
failure_set = [compute-01]
Redfish reachable
ForceOff succeeds
PowerState=Off verified
-> evacuation allowed
```

### Scenario B: two failed hosts within batch window

```text
failure_set = [compute-01, compute-02]
Both hosts have valid Redfish mapping
Both BMCs reachable
Both PowerState=Off verified
-> evacuation allowed for current notification
-> second notification reuses existing FENCED state
```

### Scenario C: too many failed hosts

```text
len(failure_set) > max_auto_fence_hosts_per_segment
-> disable known failed hosts if possible
-> do not Redfish mass-fence automatically
-> do not evacuate
-> mark notification failed/error
-> operator alert
```

### Scenario D: BMC unreachable

```text
Redfish unreachable or TLS verification error
-> mark FENCE_FAILED
-> do not evacuate
-> operator alert/manual fencing
```

### Scenario E: source host already Off

```text
PowerState=Off on first GET
identity guards pass
stable Off reads pass
-> mark FENCED without POST ForceOff
-> evacuation allowed
```

---

## Cascade failure during evacuation

Add lightweight guard to `EvacuateToStoppedTask` or before each evacuation batch:

```text
1. Read current failure events for same segment.
2. If a new failed host appears that is not in original failure_set:
   - pause evacuation;
   - disable new failed host;
   - fence new host through Redfish;
   - assert new host FENCED;
   - continue evacuation.
```

This prevents Nova scheduler from selecting a newly failed but not-yet-fenced host as destination.

---

## Nova integration rules

After verified fencing:

```text
- nova-compute service for failed host must be disabled.
- Nova service may be marked forced_down according to deployment policy.
- Do not clear disabled/forced_down automatically when the physical host returns.
- Returning host requires manual validation: no stale libvirt domains, nova-compute disabled until cleanup, then operator enables it.
```

Do not call libvirt directly. Do not define/delete/start domains directly. Use Nova APIs only.

---

## Exceptions

Add exceptions if missing:

```python
class RedfishFencingException(exception.MasakariException):
    msg_fmt = _('Redfish fencing failed: %(reason)s')

class RedfishMappingNotFound(RedfishFencingException):
    msg_fmt = _('No Redfish mapping found for host %(host)s')

class RedfishIdentityMismatch(RedfishFencingException):
    msg_fmt = _('Redfish identity mismatch for host %(host)s: %(reason)s')

class RedfishPowerOffTimeout(RedfishFencingException):
    msg_fmt = _('Timed out waiting for host %(host)s PowerState=Off')
```

All fencing failures must ultimately raise `HostRecoveryFailureException` or a subclass that causes host recovery to fail closed before evacuation.

---

## Logging and audit

Log these fields for every fencing attempt:

```text
notification_uuid
event_id
segment_uuid
hostname
bmc_address_hash, not raw secret
systems_uri
reset_target
requested_reset_type
initial_power_state
final_power_state
attempt
elapsed_seconds
result
last_error
```

Never log:

```text
password
password_secret_ref value contents
X-Auth-Token
Authorization header
session token
```

---

## Manual test commands

### Direct Redfish with session auth

```bash
BMC=10.10.20.101
USER=masakari-fencer
PASS='***'

TOKEN_AND_LOCATION=$(curl -sk -D - \
  -H 'Content-Type: application/json' \
  -X POST "https://${BMC}/redfish/v1/SessionService/Sessions" \
  -d "{\"UserName\":\"${USER}\",\"Password\":\"${PASS}\"}")
```

For actual operational testing, use a safer script that extracts `X-Auth-Token` and `Location`, then runs:

```bash
curl -sk -H "X-Auth-Token: $TOKEN" \
  "https://${BMC}/redfish/v1/Systems/1"

curl -sk -H "X-Auth-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -X POST "https://${BMC}/redfish/v1/Systems/1/Actions/ComputerSystem.Reset" \
  -d '{"ResetType":"ForceOff"}'

curl -sk -H "X-Auth-Token: $TOKEN" \
  "https://${BMC}/redfish/v1/Systems/1" | jq .PowerState
```

Expected final result:

```json
"Off"
```

---

## Unit tests

Add tests with fake/mocked Redfish and fake/mocked etcd. Do not require real BMC or real etcd in normal unit tests.

### Redfish client tests

```text
- session auth creates session and sends X-Auth-Token.
- basic auth sends HTTP basic auth when configured.
- get_system reads PowerState and Actions.#ComputerSystem.Reset.
- ForceOff POST uses ResetType=ForceOff.
- client polls until PowerState=Off.
- client fails if ForceOff is not in ResetType@Redfish.AllowableValues.
- client fails on TLS error when tls_verify=true.
- client fails on 401/403/404/500.
- client validates expected_system_uuid and expected_serial_number.
- client never logs password/token.
- client deletes session in finally path.
```

### etcd fencing state tests

```text
- register_host_failure creates event key.
- list_recent_failures returns hosts within batch window.
- acquire_segment_recovery_lock is exclusive.
- acquire_host_fencing_lock is exclusive.
- transition_fencing_state uses CAS/retry.
- mark_fenced writes verified_power_state=Off.
- mark_fence_failed writes state=FENCE_FAILED and last_error.
- assert_hosts_fenced rejects missing/FENCE_FAILED/PowerState!=Off.
```

### Task tests

```text
RegisterHostFailureEventTask:
- registers current host and returns segment_uuid/event_id.

CollectFailureSetTask:
- collects multiple host failures in one segment.
- fails closed when threshold exceeded.
- fails closed when host mapping missing.

DisableFailureSetTask:
- disables all hosts in failure_set, not only current host.

RedfishFenceFailureSetTask:
- fences every host in failure_set before returning.
- reuses existing FENCED state idempotently.
- fails closed when one host cannot be fenced.
- does not call Nova evacuate on fencing failure.

AssertFailureSetFencedTask:
- blocks evacuation unless all hosts are FENCED and PowerState=Off.

Multi-engine:
- two workflows for different hosts in same segment use one segment lock.
- second workflow reuses fencing state written by first workflow.
```

---

## Integration test plan

### Test 1: one compute failure

Setup:

```text
compute-01 has HA-enabled ACTIVE VMs
compute-02/03 are healthy
Redfish mapping exists for compute-01
```

Action:

```text
Trigger host failure notification for compute-01.
```

Expected:

```text
compute-01 nova service disabled
Redfish ForceOff sent to compute-01 BMC
PowerState=Off verified in etcd
Nova evacuation starts only after FENCED state
VMs are evacuated to stopped state
batched_start_instances_task starts originally ACTIVE VMs with existing etcd limiter
```

### Test 2: two simultaneous compute failures

Setup:

```text
compute-01 and compute-02 fail within multi_host_batch_window
max_auto_fence_hosts_per_segment = 2
```

Expected:

```text
failure_set contains compute-01 and compute-02
both nova services disabled
both hosts Redfish fenced and verified Off
first evacuation starts only after both hosts are FENCED
second notification reuses existing FENCED state
```

### Test 3: BMC unreachable

Expected:

```text
FENCE_FAILED in etcd
notification fails before prepare/evacuate
no Nova evacuate
no VM duplicate risk introduced by Masakari
```

### Test 4: wrong BMC mapping

Setup:

```text
expected_serial_number in YAML does not match Redfish SerialNumber
```

Expected:

```text
No ForceOff sent
FENCE_FAILED with identity mismatch
no evacuation
```

### Test 5: etcd outage

Expected:

```text
fencing gate fails closed
no evacuation
clear error points to etcd/fencing state unavailability
```

---

## Acceptance criteria

The fork is acceptable when all conditions are true:

1. Redfish fencing can be enabled via `[redfish_fencing] enabled=true`.
2. Every compute host can be mapped to a BMC endpoint via `redfish-hosts.yaml`.
3. Required Redfish parameters are validated at startup or first use.
4. Direct Redfish client supports session auth and basic auth.
5. Redfish client sends `ComputerSystem.Reset` with `ResetType=ForceOff` only.
6. Evacuation does not start until every host in the segment failure_set is marked `FENCED` with `verified_power_state=Off` in etcd.
7. Multiple failed hosts in one segment are handled under one segment-level lock.
8. Fencing state updates use etcd CAS and are idempotent.
9. BMC unreachable, TLS error, missing mapping, identity mismatch, unsupported `ForceOff`, etcd outage, or threshold exceeded all fail closed.
10. No BMC credentials or Redfish tokens are written to etcd or logs.
11. Returning compute hosts are not automatically re-enabled by this code.
12. Unit tests cover Redfish parameters, Redfish client behavior, etcd state, TaskFlow gating, multi-host/multi-engine scenarios, and fail-closed paths.

---

## Operator notes to include in docs/release note

Document these points:

```text
- Redfish fencing is required before Nova evacuation for split-brain protection.
- Redfish proof is PowerState=Off from the BMC ComputerSystem resource.
- reset_type must be ForceOff; graceful shutdown/reboot are not acceptable fencing proofs.
- BMC network must be reachable independently of compute management/storage/tenant networks.
- BMC credentials must be stored in a secret backend or protected files, not in etcd.
- redfish-hosts.yaml should include expected UUID/serial guards where possible.
- If Redfish or etcd is unavailable, Masakari fails closed and does not evacuate.
- Operators must manually validate and re-enable fenced compute hosts.
```

---

## References for Codex

Use these references while implementing:

```text
Redfish specification / DMTF:
https://www.dmtf.org/standards/redfish
https://www.dmtf.org/sites/default/files/standards/documents/DSP0266_1.18.0.html
https://redfish.dmtf.org/schemas/v1/DSP2046_2025.2.html

Redfish session auth:
https://www.dmtf.org/sites/default/files/Redfish_School-Sessions.pdf

fence_redfish man page:
https://www.mankier.com/8/fence_redfish

OpenStack Masakari custom recovery tasks:
https://docs.openstack.org/masakari/latest/configuration/recovery_workflow_sample_config.html
```
