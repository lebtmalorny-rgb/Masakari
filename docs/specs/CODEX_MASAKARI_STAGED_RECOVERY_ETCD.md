# CODEX TASK: staged VM start in Masakari fork with etcd state store

## Назначение

Этот Markdown — техническое задание для Codex/AI coding agent, работающего в fork-е `openstack/masakari` ветки `stable/2025.1`.

Нужно реализовать механизм поэтапного запуска ВМ после отказа compute-гипервизора в Masakari, без Mistral, с хранением state/lease в **etcd**.

Цель:

```text
После host failure recovery запускать не более N ВМ одновременно на один destination hypervisor,
включая каскадные отказы compute-хостов и несколько активных masakari-engine.
```

---

## Ключевое решение

Реализация делается внутри fork-а Masakari:

```text
Masakari TaskFlow custom/built-in tasks
        ↓
Nova evacuate microversion >= 2.95
        ↓
etcd persistent state + TTL start leases
        ↓
Nova server start батчами
```

Использовать etcd для двух вещей:

1. **Persistent recovery state** по каждой ВМ.
2. **Distributed per-dest-host start leases** для ограничения числа одновременно стартующих ВМ.

Tooz/`masakari.coordination` использовать только для короткого distributed mutex при атомарном выделении start slot. Не использовать Tooz как хранилище state: Tooz даёт coordination primitives, а не полноценный прикладной KV API для нашей state machine.

---

## Upstream context, который нужно учитывать

Перед началом работ Codex должен проверить локальный checkout fork-а. Ориентиры для `stable/2025.1`:

```text
masakari/engine/drivers/taskflow/host_failure.py
```

В этом файле находятся штатные task-и host failure recovery:

```text
DisableComputeServiceTask
PrepareHAEnabledInstancesTask
EvacuateInstancesTask
```

`PrepareHAEnabledInstancesTask` создаёт `VMove` records. `EvacuateInstancesTask` берёт `VMove` со статусом `PENDING`, выполняет evacuation через Nova и использует `GreenPool(CONF.host_failure_recovery_threads)`.

Reference:

```text
https://raw.githubusercontent.com/openstack/masakari/stable/2025.1/masakari/engine/drivers/taskflow/host_failure.py
```

Nova client Masakari находится здесь:

```text
masakari/compute/nova.py
```

В upstream `stable/2025.1` там используется:

```python
NOVA_API_VERSION = "2.53"
```

Штатный метод evacuation вызывает Nova через этот microversion. Для staged start нужен отдельный метод с Nova microversion `2.95+`.

Reference:

```text
https://raw.githubusercontent.com/openstack/masakari/stable/2025.1/masakari/compute/nova.py
```

Nova Compute API начиная с microversion `2.95` оставляет эвакуированную ВМ остановленной на destination host до ручного запуска. Это требуемое поведение для staged start.

Reference:

```text
https://docs.openstack.org/api-ref/compute/
```

Masakari поддерживает custom recovery tasks через entry point:

```text
masakari.task_flow.tasks
```

Reference:

```text
https://docs.openstack.org/masakari/2025.1/configuration/recovery_workflow_custom_task.html
```

Masakari имеет coordination wrapper:

```text
masakari/coordination.py
```

Он создаёт Tooz coordinator из `[coordination] backend_url` и предоставляет `get_lock(name)`. Это можно использовать для distributed lock.

Reference:

```text
https://raw.githubusercontent.com/openstack/masakari/stable/2025.1/masakari/coordination.py
```

Для etcd3 через Tooz обычно используется драйвер `etcd3+http`, а пакет `etcd3gw` требуется для такого backend-а. Для direct KV state store также использовать `etcd3gw`.

References:

```text
https://docs.openstack.org/tooz/latest/user/drivers.html
https://docs.openstack.org/etcd3gw/latest/api/etcd3gw.client.html
```

---

## Что должно получиться

Новый staged workflow для host failure:

```ini
[taskflow_driver_recovery_flows]

host_auto_failure_recovery_tasks = {
  'pre': ['disable_compute_service_task'],
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
  'pre': ['disable_compute_service_task'],
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

Логика:

```text
1. Masakari получает host failure notification.
2. Штатная task отключает compute service.
3. Штатная task создаёт VMove для ВМ.
4. Новая reconcile task создаёт/обновляет etcd state.
5. Новая evacuation task вызывает Nova evacuate с microversion >= 2.95.
6. Эвакуированные ВМ остаются stopped на destination host.
7. Новая start task запускает только ранее ACTIVE ВМ.
8. Для каждого dest_host используется distributed semaphore в etcd.
9. Слот удерживается до ACTIVE / ERROR / TIMEOUT, а не только до возврата nova start API.
```

---

## Важные ограничения

### Не использовать SQL DB Masakari для staged state

В этой версии state storage — etcd. Не добавлять обязательные SQL migration для:

```text
staged_recovery_instances
staged_start_leases
```

Можно использовать SQL только для штатных объектов Masakari, например `Notification` и `VMove`.

### Не использовать in-memory state

Запрещено:

```python
self.active_starts[dest_host] += 1
```

Такой state не работает при нескольких `masakari-engine` и теряется при рестарте.

### Не держать distributed lock во время старта ВМ

Tooz lock использовать только для короткой критической секции:

```text
- прочитать live lease keys для dest_host;
- если их меньше N, создать новый lease key;
- отпустить lock.
```

Не держать lock пока ВМ грузится. Долгий слот должен быть etcd TTL lease key.

### Не обходить Nova scheduler

В auto recovery не указывать destination host. Nova scheduler должен выбрать host сам, сохранив placement/server group/AZ/aggregate rules.

В reserved-host recovery, если destination host указывается явно, не использовать `force=True`.

### Fail closed

Если staged recovery включён, но etcd недоступен или coordination не настроена, recovery не должен переходить в неограниченный старт ВМ. Правильное поведение: fail closed с понятной ошибкой.

---

## Файлы для изменения

Ожидаемый change set:

```text
requirements.txt                                      # добавить/проверить etcd3gw
masakari/compute/nova.py                              # Nova microversion 2.95 method
masakari/conf/staged_recovery.py                      # new config group
masakari/conf/__init__.py                             # register opts
masakari/conf/opts.py                                 # config generator opts
masakari/engine/drivers/taskflow/staged_host_failure.py
masakari/engine/drivers/taskflow/staged_state_etcd.py
masakari/engine/drivers/taskflow/staged_limiter.py
setup.cfg                                             # entry points
etc/masakari/masakari-custom-recovery-methods.conf.sample
masakari/engine/manager.py                            # optional but recommended stale RUNNING reconcile
masakari/tests/unit/engine/drivers/taskflow/test_staged_host_failure.py
masakari/tests/unit/engine/drivers/taskflow/test_staged_state_etcd.py
masakari/tests/unit/engine/drivers/taskflow/test_staged_limiter.py
masakari/tests/unit/compute/test_nova_staged.py
```

Не менять:

```text
masakari-monitors
Nova scheduler
libvirt/QEMU напрямую
```

---

## Dependency

Проверить, есть ли `etcd3gw` уже в dependency chain через Tooz backend. Если нет, добавить в `requirements.txt` согласно OpenStack constraints:

```text
etcd3gw>=2.4.0
```

Не фиксировать версию жёстко, если в проекте используется upper-constraints.

---

## Новая конфигурация

Создать группу `[staged_recovery]`.

Пример runtime config:

```ini
[staged_recovery]
enabled = true
state_backend = etcd

# If empty, use [coordination] backend_url when it is etcd3+http or etcd3+https.
etcd_backend_url =
etcd_prefix = /masakari/staged-recovery/v1
etcd_timeout = 5

# Strict limit per destination hypervisor.
max_parallel_starts_per_host = 2

# Start timeout and lease TTL.
# For strictness, slot_lease_ttl must be >= start_timeout + batch_delay + safety_margin.
start_timeout = 900
batch_delay = 30
slot_lease_ttl = 990
slot_retry_interval = 5

# Nova evacuate-to-stopped behavior.
nova_evacuate_microversion = 2.95

# Only VMs that were ACTIVE at failure time should be started automatically.
start_only_originally_active = true

# Optional manager reconcile.
reconcile_interval = 60
stale_recovery_timeout = 300
```

Coordination must also be configured:

```ini
[coordination]
backend_url = etcd3+http://etcd.example.internal:2379
```

For TLS:

```ini
[coordination]
backend_url = etcd3+https://etcd.example.internal:2379
```

If direct etcd KV access needs separate cert settings, add:

```ini
[staged_recovery]
etcd_ca_cert = /etc/ssl/certs/etcd-ca.pem
etcd_cert_file = /etc/masakari/etcd-client.pem
etcd_key_file = /etc/masakari/etcd-client-key.pem
```

### Config opts skeleton

Create:

```text
masakari/conf/staged_recovery.py
```

Skeleton:

```python
from oslo_config import cfg

staged_recovery_group = cfg.OptGroup(
    'staged_recovery',
    title='Staged recovery options',
    help='Options for staged VM start after host failure recovery.',
)

staged_recovery_opts = [
    cfg.BoolOpt(
        'enabled', default=False,
        help='Enable staged VM start workflow for host failure recovery.',
    ),
    cfg.StrOpt(
        'state_backend', default='etcd', choices=['etcd'],
        help='Persistent state backend for staged recovery.',
    ),
    cfg.StrOpt(
        'etcd_backend_url', default=None,
        help='etcd3 gateway URL for staged recovery state. If empty, reuse '
             '[coordination] backend_url when it uses etcd3+http(s).',
    ),
    cfg.StrOpt(
        'etcd_prefix', default='/masakari/staged-recovery/v1',
        help='Key prefix for staged recovery state in etcd.',
    ),
    cfg.IntOpt(
        'etcd_timeout', default=5, min=1,
        help='Timeout in seconds for etcd KV requests.',
    ),
    cfg.StrOpt(
        'etcd_ca_cert', default=None,
        help='CA certificate for direct etcd KV client.',
    ),
    cfg.StrOpt(
        'etcd_cert_file', default=None,
        help='Client certificate for direct etcd KV client.',
    ),
    cfg.StrOpt(
        'etcd_key_file', default=None,
        help='Client private key for direct etcd KV client.',
    ),
    cfg.IntOpt(
        'max_parallel_starts_per_host', default=2, min=1,
        help='Maximum number of simultaneously starting VMs per destination host.',
    ),
    cfg.IntOpt(
        'start_timeout', default=900, min=60,
        help='Maximum time to wait until instance becomes ACTIVE.',
    ),
    cfg.IntOpt(
        'batch_delay', default=30, min=0,
        help='Additional delay after VM becomes ACTIVE before releasing slot.',
    ),
    cfg.IntOpt(
        'slot_lease_ttl', default=990, min=60,
        help='TTL for per-host start slot lease. Must cover start_timeout + batch_delay.',
    ),
    cfg.IntOpt(
        'slot_retry_interval', default=5, min=1,
        help='Delay between attempts to acquire a start slot.',
    ),
    cfg.StrOpt(
        'nova_evacuate_microversion', default='2.95',
        help='Nova microversion used for evacuate-to-stopped.',
    ),
    cfg.BoolOpt(
        'start_only_originally_active', default=True,
        help='Only start instances whose original vm_state was active.',
    ),
    cfg.IntOpt(
        'reconcile_interval', default=60, min=10,
        help='Periodic interval for stale staged recovery reconciliation.',
    ),
    cfg.IntOpt(
        'stale_recovery_timeout', default=300, min=60,
        help='Recovery is stale if updated_at is older than this timeout.',
    ),
]


def register_opts(conf):
    conf.register_group(staged_recovery_group)
    conf.register_opts(staged_recovery_opts, group=staged_recovery_group)


def list_opts():
    return {
        staged_recovery_group: staged_recovery_opts,
    }
```

Register this module using the same pattern as the existing `masakari/conf/*.py` modules.

---

## etcd key model

All keys live under:

```text
<etcd_prefix>
```

Default:

```text
/masakari/staged-recovery/v1
```

### Instance state key

```text
/masakari/staged-recovery/v1/notifications/<notification_uuid>/instances/<instance_uuid>
```

Value: JSON.

Example:

```json
{
  "schema_version": 1,
  "notification_uuid": "8dc7b4a8-53f7-4c4f-a5d2-1c8b6bb5a111",
  "instance_uuid": "6b1d1f6e-e0a3-48d2-998e-5a6d3a4c2222",
  "instance_name": "vm-001",
  "source_host": "compute-01",
  "dest_host": "compute-10",
  "original_vm_state": "active",
  "original_task_state": null,
  "original_power_state": 1,
  "current_vm_state": "stopped",
  "step": "EVACUATED_STOPPED",
  "attempt": 1,
  "last_error": null,
  "owner": "masakari-engine-controller-1-12345",
  "created_at": "2026-06-09T12:00:00Z",
  "updated_at": "2026-06-09T12:04:00Z"
}
```

### Start lease key

```text
/masakari/staged-recovery/v1/start-leases/<encoded_dest_host>/<instance_uuid>
```

Value: JSON.

Example:

```json
{
  "schema_version": 1,
  "notification_uuid": "8dc7b4a8-53f7-4c4f-a5d2-1c8b6bb5a111",
  "instance_uuid": "6b1d1f6e-e0a3-48d2-998e-5a6d3a4c2222",
  "dest_host": "compute-10",
  "owner": "masakari-engine-controller-1-12345",
  "etcd_lease_id": 123456789,
  "created_at": "2026-06-09T12:05:00Z",
  "expires_at": "2026-06-09T12:21:30Z"
}
```

The key must be attached to an etcd TTL lease. If the `masakari-engine` dies, the key is eventually removed by etcd.

### Optional owner/progress key

For easier debugging:

```text
/masakari/staged-recovery/v1/notifications/<notification_uuid>/owner
```

Value:

```json
{
  "owner": "masakari-engine-controller-1-12345",
  "updated_at": "2026-06-09T12:05:00Z"
}
```

This is optional; correctness should rely on per-instance state and start leases.

---

## Step values

Use constants, not string literals scattered through code.

```python
STEP_DISCOVERED = 'DISCOVERED'
STEP_EVACUATING = 'EVACUATING'
STEP_EVACUATED_STOPPED = 'EVACUATED_STOPPED'
STEP_WAITING_START_SLOT = 'WAITING_START_SLOT'
STEP_STARTING = 'STARTING'
STEP_ACTIVE = 'ACTIVE'
STEP_FAILED = 'FAILED'
STEP_IGNORED = 'IGNORED'
```

Final states:

```text
ACTIVE
FAILED
IGNORED
```

State transitions:

| Current step | Condition | Next step |
|---|---|---|
| missing | VMove exists | DISCOVERED |
| DISCOVERED | evacuation starts | EVACUATING |
| EVACUATING | server moved and stopped | EVACUATED_STOPPED |
| EVACUATED_STOPPED | original state was not active | IGNORED |
| EVACUATED_STOPPED | waiting for slot | WAITING_START_SLOT |
| WAITING_START_SLOT | lease acquired | STARTING |
| STARTING | Nova reports ACTIVE | ACTIVE |
| STARTING | Nova reports ERROR or timeout | FAILED |
| any non-final | unrecoverable error | FAILED |

Reconcile may safely jump:

```text
EVACUATING -> EVACUATED_STOPPED
STARTING -> ACTIVE
DISCOVERED -> ACTIVE
STARTING -> EVACUATED_STOPPED, if lease expired and VM is still stopped
```

---

## Encoding rules for etcd keys

Host names are usually safe, but implement encoding anyway.

```python
from urllib import parse


def encode_key_part(value):
    return parse.quote(str(value), safe='')


def decode_key_part(value):
    return parse.unquote(value)
```

Use encoded host names in paths:

```text
start-leases/<encoded_dest_host>/<instance_uuid>
```

Do not use raw user-controlled strings directly in etcd key paths.

---

## etcd state module

Create:

```text
masakari/engine/drivers/taskflow/staged_state_etcd.py
```

Purpose:

```text
Hide etcd3gw details behind a small state-store API.
Task code should not build etcd JSON/key paths directly.
```

### Public API to implement

```python
class EtcdStagedRecoveryStore(object):
    def __init__(self, conf, owner):
        pass

    def assert_available(self):
        """Fail fast if etcd state backend is not usable."""

    def create_or_get_instance_state(self, notification_uuid, instance_uuid, values):
        """Create instance state if missing. Return current state dict."""

    def get_instance_state(self, notification_uuid, instance_uuid):
        """Return state dict or None."""

    def list_instance_states(self, notification_uuid):
        """Return all instance state dicts for notification."""

    def list_stale_instance_states(self, older_than_seconds, steps):
        """Scan prefix and return stale states matching steps."""

    def update_instance_state(self, notification_uuid, instance_uuid, patch):
        """Patch JSON state using CAS/retry."""

    def transition_instance_state(
        self, notification_uuid, instance_uuid, expected_steps, new_step, patch=None,
    ):
        """CAS transition if current step is in expected_steps."""

    def mark_failed(self, notification_uuid, instance_uuid, error):
        """Set step FAILED and last_error."""

    def list_start_leases(self, dest_host):
        """Return live start lease dicts under start-leases/<dest_host>."""

    def create_start_lease(self, notification_uuid, instance_uuid, dest_host, ttl):
        """Create a TTL lease key atomically if not already present."""

    def refresh_start_lease(self, lease_record):
        """Refresh etcd lease if implementation uses heartbeat."""

    def release_start_lease(self, dest_host, instance_uuid):
        """Delete lease key. Also revoke etcd lease if safe."""
```

### etcd client creation

Use direct `etcd3gw` client:

```python
import etcd3gw

client = etcd3gw.client(
    host=host,
    port=port,
    protocol=protocol,
    ca_cert=CONF.staged_recovery.etcd_ca_cert,
    cert_key=CONF.staged_recovery.etcd_key_file,
    cert_cert=CONF.staged_recovery.etcd_cert_file,
    timeout=CONF.staged_recovery.etcd_timeout,
    api_path=api_path,
)
```

Parse either:

```text
[staged_recovery] etcd_backend_url
```

or fallback to:

```text
[coordination] backend_url
```

when it starts with:

```text
etcd3+http://
etcd3+https://
```

Remove the `etcd3+` prefix before passing protocol into etcd3gw.

Examples:

```text
etcd3+http://etcd.example.internal:2379
  -> protocol=http, host=etcd.example.internal, port=2379

etcd3+https://etcd.example.internal:2379
  -> protocol=https, host=etcd.example.internal, port=2379
```

If multiple endpoints are desired, support comma-separated URLs or require a VIP/load balancer. Minimal implementation may support one URL and document that HA etcd should be exposed through a stable endpoint.

### JSON serialization

Use deterministic JSON to make compare-and-replace possible:

```python
import json


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def loads(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return json.loads(value)
```

### CAS updates

etcd3gw provides `create()` and `replace()` helper methods. Use them for idempotency.

Pattern:

```python
def update_instance_state(self, notification_uuid, instance_uuid, patch):
    key = self.instance_key(notification_uuid, instance_uuid)
    for attempt in range(MAX_CAS_RETRIES):
        current_raw = self._get_raw(key)
        if current_raw is None:
            raise StateNotFound(...)

        current = loads(current_raw)
        new = dict(current)
        new.update(patch)
        new['updated_at'] = utcnow_iso()

        if self.client.replace(key, current_raw, dumps(new)):
            return new

        eventlet.sleep(0)

    raise ConcurrentStateUpdate(...)
```

Do not blindly overwrite whole state without CAS.

---

## etcd start limiter

Create:

```text
masakari/engine/drivers/taskflow/staged_limiter.py
```

Purpose:

```text
Guarantee max N simultaneously STARTING VMs per dest_host across all masakari-engine processes.
```

### Invariant

For each `dest_host`:

```text
count(live etcd keys under start-leases/<dest_host>/) <= max_parallel_starts_per_host
```

### Important TTL invariant

For strict boot-storm protection:

```text
slot_lease_ttl >= start_timeout + batch_delay + safety_margin
```

Reason: if the engine dies after `nova start`, etcd should keep the slot occupied until the worst-case start window is over. Otherwise a slot could expire while the VM is still booting, allowing overbooking.

### Acquire algorithm

```text
while True:
  acquire Tooz lock: staged-start-lock:<dest_host>
    list live start lease keys for dest_host
    if live_count < max_parallel_starts_per_host:
       create etcd TTL lease with ttl=slot_lease_ttl
       create start lease key using etcd create(key, value, lease=lease)
       if create succeeded:
          return lease_record
    release Tooz lock
  sleep(slot_retry_interval)
```

Use Tooz lock only for this short critical section. The actual VM startup happens after the lock is released.

### Release algorithm

```text
on ACTIVE / ERROR / TIMEOUT:
  delete start lease key
  revoke etcd lease if possible
```

### Heartbeat

Two acceptable modes:

#### Preferred simple mode

Set:

```text
slot_lease_ttl = start_timeout + batch_delay + safety_margin
```

Then heartbeat is optional. The key expires automatically if engine dies, but not before the configured start timeout.

#### Extended mode

If heartbeat is implemented:

```text
- refresh lease while waiting for ACTIVE;
- if refresh fails before nova start was called, do not start VM;
- if refresh fails after nova start, continue reconcile but log critical warning;
- never acquire a new slot locally without a live etcd lease.
```

Do not rely on a short heartbeat TTL for strict start limiting during etcd/network partitions.

### Pseudocode

```python
class StartSlot(object):
    def __init__(self, notification_uuid, instance_uuid, dest_host, lease_id):
        self.notification_uuid = notification_uuid
        self.instance_uuid = instance_uuid
        self.dest_host = dest_host
        self.lease_id = lease_id


class EtcdStartLimiter(object):
    def __init__(self, context, state_store, owner):
        self.context = context
        self.state_store = state_store
        self.owner = owner
        self.max_parallel = CONF.staged_recovery.max_parallel_starts_per_host
        self.retry_interval = CONF.staged_recovery.slot_retry_interval
        self.ttl = CONF.staged_recovery.slot_lease_ttl

    def _lock_name(self, dest_host):
        return 'staged-start-lock-%s' % dest_host

    def acquire(self, notification_uuid, instance_uuid, dest_host):
        while True:
            lock = coordination.COORDINATOR.get_lock(self._lock_name(dest_host))
            if lock is None:
                raise exception.MasakariException(
                    reason='[coordination] backend_url is required for staged recovery'
                )

            with lock:
                live = self.state_store.list_start_leases(dest_host)
                if len(live) < self.max_parallel:
                    slot = self.state_store.create_start_lease(
                        notification_uuid=notification_uuid,
                        instance_uuid=instance_uuid,
                        dest_host=dest_host,
                        ttl=self.ttl,
                    )
                    if slot:
                        return slot

            eventlet.sleep(self.retry_interval)

    def release(self, slot):
        self.state_store.release_start_lease(slot.dest_host, slot.instance_uuid)
```

---

## Nova client changes

Modify:

```text
masakari/compute/nova.py
```

Do not globally change:

```python
NOVA_API_VERSION = "2.53"
```

Instead parameterize `novaclient()`:

```python
def novaclient(context, timeout=None, api_version=NOVA_API_VERSION):
    ...
    nova_extensions = [
        ext for ext in nova_client.discover_extensions(api_version)
        if ext.name in ("list_extensions",)
    ]
    client_obj = nova_client.Client(
        api_versions.APIVersion(api_version),
        session=keystone_session,
        insecure=CONF.nova_api_insecure,
        timeout=timeout,
        global_request_id=context.global_id,
        region_name=CONF.os_region_name,
        endpoint_type=endpoint_type,
        service_type=service_type,
        service_name=service_name,
        cacert=CONF.nova_ca_certificates_file,
        extensions=nova_extensions,
    )
    return client_obj
```

Add methods:

```python
@translate_nova_exception
def evacuate_instance_stopped(self, context, uuid, target=None):
    """Evacuate an instance and leave it stopped on destination host.

    Requires Nova microversion >= 2.95.
    """
    api_version = CONF.staged_recovery.nova_evacuate_microversion
    nova = novaclient(context, api_version=api_version)
    LOG.info(
        'Call evacuate-to-stopped for instance %(uuid)s on target %(target)s '
        'using Nova microversion %(api_version)s',
        {'uuid': uuid, 'target': target, 'api_version': api_version},
    )
    if target:
        return nova.servers.evacuate(uuid, host=target)
    return nova.servers.evacuate(uuid)


@translate_nova_exception
def get_server_with_microversion(self, context, uuid, api_version):
    nova = novaclient(context, api_version=api_version)
    return nova.servers.get(uuid)
```

Keep existing `start_server()`, `stop_server()`, `get_server()`, etc., unless a microversion-specific behavior is needed.

---

## New TaskFlow tasks

Create:

```text
masakari/engine/drivers/taskflow/staged_host_failure.py
```

Implement:

```text
ReconcileStagedRecoveryTask
EvacuateToStoppedTask
BatchedStartInstancesTask
```

Use existing `base.MasakariTask` style.

---

## Task 1: ReconcileStagedRecoveryTask

Purpose:

```text
Make workflow idempotent and resumable using etcd state.
```

Class skeleton:

```python
class ReconcileStagedRecoveryTask(base.MasakariTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['host_name', 'notification_uuid']
        super(ReconcileStagedRecoveryTask, self).__init__(
            context, novaclient, **kwargs)
```

Algorithm:

```text
1. Assert staged_recovery.enabled=true.
2. Assert etcd state store is available.
3. Get all VMove rows for notification_uuid.
4. For each VMove:
   4.1. Get current server from Nova.
   4.2. Create etcd state key if missing.
   4.3. Do not overwrite original_vm_state if state already exists.
   4.4. If server host != source_host and server state is stopped/shutoff:
        set dest_host and step=EVACUATED_STOPPED.
   4.5. If server host != source_host and server state is active:
        set dest_host and step=ACTIVE.
   4.6. If original_vm_state was not active and VM is evacuated:
        set step=IGNORED when start_only_originally_active=true.
5. List STARTING rows and reconcile:
   5.1. If VM is ACTIVE, mark ACTIVE and release lease.
   5.2. If lease is gone and VM is stopped, mark EVACUATED_STOPPED.
   5.3. If VM is ERROR, mark FAILED.
```

Important:

```text
original_vm_state = state at first discovery;
current_vm_state = observed current Nova state;
updated_at must be refreshed on every meaningful state update.
```

---

## Task 2: EvacuateToStoppedTask

Purpose:

```text
Replace evacuation phase of EvacuateInstancesTask and use Nova microversion 2.95.
```

Class skeleton:

```python
class EvacuateToStoppedTask(base.MasakariTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['host_name', 'notification_uuid']
        self.update_host_method = kwargs.get('update_host_method')
        super(EvacuateToStoppedTask, self).__init__(
            context, novaclient, **kwargs)

    def execute(self, host_name, notification_uuid, reserved_host=None):
        ...
```

Algorithm:

```text
1. Get VMove rows for notification_uuid with status PENDING or ONGOING.
2. For each VMove:
   2.1. Load etcd state.
   2.2. If step in EVACUATED_STOPPED, ACTIVE, IGNORED: skip.
   2.3. Get server from Nova.
   2.4. Save original_vm_state/task_state/power_state if absent.
   2.5. Preserve upstream lock/unlock behavior:
        - if server not locked, lock before evacuation;
        - unlock at end only if this task locked it.
   2.6. Preserve upstream vm_state/task_state reset logic where needed.
   2.7. Set etcd step=EVACUATING.
   2.8. Set VMove status=ONGOING and start_time.
   2.9. Call novaclient.evacuate_instance_stopped(context, uuid, target=reserved_host).
   2.10. Wait until server host != source_host.
   2.11. Wait until server state is stopped/shutoff.
   2.12. Record dest_host in etcd and VMove.
   2.13. Set etcd step=EVACUATED_STOPPED.
   2.14. Set VMove status=SUCCEEDED and end_time.
```

Reuse carefully from upstream `EvacuateInstancesTask`:

```text
- VMove updates;
- lock/unlock behavior;
- reset_instance_state behavior;
- loopingcall.FixedIntervalWithTimeoutLoopingCall;
- reserved host aggregate handling;
- update_host_method for reserved host;
- HostRecoveryFailureException on failures.
```

Main difference:

```text
Use evacuate_instance_stopped(), not evacuate_instance().
Do not immediately start active VMs.
Do not call _stop_after_evacuation() as the normal path for 2.95.
```

---

## Task 3: BatchedStartInstancesTask

Purpose:

```text
Start only originally ACTIVE VMs, with strict N-per-dest-host limiter in etcd.
```

Class skeleton:

```python
class BatchedStartInstancesTask(base.MasakariTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['notification_uuid']
        super(BatchedStartInstancesTask, self).__init__(
            context, novaclient, **kwargs)
```

Algorithm:

```text
1. Load etcd states for notification_uuid.
2. Select candidates:
   - step == EVACUATED_STOPPED;
   - original_vm_state == active, if start_only_originally_active=true.
3. For rows with original_vm_state != active:
   - mark IGNORED;
   - do not start.
4. Start workers using GreenPool.
5. Each worker:
   5.1. Set step=WAITING_START_SLOT.
   5.2. Acquire etcd start slot for dest_host.
   5.3. Set step=STARTING.
   5.4. Call nova.start_server(instance_uuid).
   5.5. Wait until ACTIVE / ERROR / TIMEOUT.
   5.6. If ACTIVE: set step=ACTIVE.
   5.7. If ERROR/TIMEOUT: set step=FAILED and last_error.
   5.8. Sleep batch_delay.
   5.9. Release start slot.
6. If any originally ACTIVE VM failed to start, fail the task and notification.
```

Important:

```text
The GreenPool size is not the correctness boundary.
Correctness comes from EtcdStartLimiter.
```

Suggested worker pool:

```python
pool_size = max(
    CONF.host_failure_recovery_threads,
    CONF.staged_recovery.max_parallel_starts_per_host,
)
```

But even if multiple workflows and engines run in parallel, etcd limiter must enforce the global per-host limit.

---

## Entry points

Update `setup.cfg`:

```ini
[entry_points]
masakari.task_flow.tasks =
    disable_compute_service_task = masakari.engine.drivers.taskflow.host_failure:DisableComputeServiceTask
    prepare_HA_enabled_instances_task = masakari.engine.drivers.taskflow.host_failure:PrepareHAEnabledInstancesTask
    evacuate_instances_task = masakari.engine.drivers.taskflow.host_failure:EvacuateInstancesTask
    reconcile_staged_recovery_task = masakari.engine.drivers.taskflow.staged_host_failure:ReconcileStagedRecoveryTask
    evacuate_to_stopped_task = masakari.engine.drivers.taskflow.staged_host_failure:EvacuateToStoppedTask
    batched_start_instances_task = masakari.engine.drivers.taskflow.staged_host_failure:BatchedStartInstancesTask
```

Keep existing upstream entry points. Add new ones.

---

## Custom recovery config sample

Add staged sample to:

```text
etc/masakari/masakari-custom-recovery-methods.conf.sample
```

or the relevant sample source in the fork.

Sample:

```ini
[taskflow_driver_recovery_flows]

host_auto_failure_recovery_tasks = {
    'pre': ['disable_compute_service_task'],
    'main': [
        'prepare_HA_enabled_instances_task',
        'reconcile_staged_recovery_task',
        'evacuate_to_stopped_task'
    ],
    'post': ['batched_start_instances_task']
}

host_rh_failure_recovery_tasks = {
    'pre': ['disable_compute_service_task'],
    'main': [
        'prepare_HA_enabled_instances_task',
        'reconcile_staged_recovery_task',
        'evacuate_to_stopped_task'
    ],
    'post': ['batched_start_instances_task']
}
```

Match the exact formatting style of existing Masakari samples.

---

## VMove handling

Still use Masakari `VMove` for API/audit compatibility.

Rules:

```text
- PrepareHAEnabledInstancesTask creates VMove rows.
- ReconcileStagedRecoveryTask creates etcd state based on VMove.
- EvacuateToStoppedTask updates VMove PENDING -> ONGOING -> SUCCEEDED/FAILED.
- dest_host must be written to both VMove and etcd state.
- BatchedStartInstancesTask does not create VMove rows.
```

Start failure should be represented in etcd state and notification status. Do not falsely mark evacuation as failed if evacuation succeeded but VM start failed.

Recommended semantics:

```text
VMove = evacuation result.
etcd state = full staged recovery result.
Notification final status = failed/error if any originally ACTIVE VM failed to start.
```

---

## Host attribute names

Upstream Masakari uses:

```python
getattr(server, "OS-EXT-SRV-ATTR:hypervisor_hostname")
```

Use the same attribute for source/destination host comparison unless the local fork already uses a different field.

VM state attributes:

```python
vm_state = getattr(server, "OS-EXT-STS:vm_state", None)
task_state = getattr(server, "OS-EXT-STS:task_state", None)
power_state = getattr(server, "OS-EXT-STS:power_state", None)
```

---

## Original VM state preservation

For every VM, store in etcd on first discovery:

```text
original_vm_state
original_task_state
original_power_state
```

Do not overwrite these on retry/reconcile.

Startup rule:

```text
If original_vm_state == 'active': start after evacuation.
If original_vm_state == 'stopped': keep stopped.
If original_vm_state == 'error': keep error/stopped according to existing Masakari semantics; do not auto-start.
```

---

## Split-brain safety

Do not solve split-brain in this task by direct hypervisor control.

Keep Nova evacuation preconditions:

```text
- failed host must be fenced;
- failed host must be down or forced_down;
- original server must no longer be running on source host.
```

The staged recovery code must not start or define VMs through libvirt directly. Only Nova API.

---

## Failure and reconcile behavior

### Engine dies before evacuation

Expected:

```text
etcd state exists: DISCOVERED or EVACUATING
VMove maybe PENDING/ONGOING
another engine retries workflow
reconcile reads Nova and VMove
safe to continue
```

### Engine dies after evacuation but before etcd update

Expected:

```text
reconcile reads Nova
server host != source_host and vm_state stopped
set dest_host
set step=EVACUATED_STOPPED
continue staged start
```

### Engine dies after start slot acquired but before nova start

Expected:

```text
start lease key remains until TTL then expires
reconcile sees VM still stopped
sets step=EVACUATED_STOPPED or WAITING_START_SLOT
another engine can acquire new slot later
```

### Engine dies after nova start

Expected:

```text
start lease key remains until TTL or until another reconcile releases it
reconcile reads Nova:
  if ACTIVE -> step=ACTIVE, release lease if still exists
  if ERROR -> step=FAILED, release lease
  if still stopped and lease expired -> retry start
```

### etcd unavailable

Expected:

```text
No unthrottled VM start.
Task fails closed with explicit error.
```

---

## Optional but recommended manager patch

Problem:

If `masakari-engine` dies while notification status is `RUNNING`, another engine may not immediately pick it up. To make etcd state useful for takeover, add a periodic reconcile in:

```text
masakari/engine/manager.py
```

Add periodic task guarded by `CONF.staged_recovery.enabled`:

```python
@periodic_task.periodic_task(spacing=CONF.staged_recovery.reconcile_interval)
def _process_stale_staged_recoveries(self, context):
    ...
```

Algorithm:

```text
1. Scan etcd prefix for instance states with:
   step in EVACUATING, WAITING_START_SLOT, STARTING
   updated_at older than stale_recovery_timeout
2. Group by notification_uuid.
3. For each notification_uuid:
   3.1. Acquire distributed lock staged-recovery-notification:<uuid>.
   3.2. Load Masakari Notification from DB.
   3.3. If status is RUNNING and staged state is stale:
        set notification status to ERROR.
   3.4. Let existing unfinished notification processing retry it.
```

This relies on idempotent tasks to continue from etcd state. Do not directly run two workflows for the same notification.

If this manager patch is not acceptable, add a separate admin command or service:

```text
masakari-staged-recovery-reconciler
```

But for fork behavior, built-in periodic reconcile is preferred.

---

## Unit tests

Add tests with fake/mocked etcd store. Do not require a real etcd server for normal unit tests.

Suggested files:

```text
masakari/tests/unit/engine/drivers/taskflow/test_staged_state_etcd.py
masakari/tests/unit/engine/drivers/taskflow/test_staged_limiter.py
masakari/tests/unit/engine/drivers/taskflow/test_staged_host_failure.py
masakari/tests/unit/compute/test_nova_staged.py
```

### etcd state store tests

```text
- create_or_get_instance_state creates missing key.
- create_or_get_instance_state returns existing key and does not overwrite original_vm_state.
- update_instance_state uses CAS/retry.
- transition_instance_state succeeds only from expected steps.
- list_instance_states returns keys under one notification.
- list_stale_instance_states filters by step and updated_at.
- create_start_lease attaches TTL lease and creates key.
- release_start_lease deletes key.
```

### limiter tests

```text
- acquire succeeds when live leases < max.
- acquire waits/retries when live leases == max.
- acquire across two limiter instances cannot exceed max.
- release frees slot.
- fail closed when coordination lock is unavailable.
```

### Nova client tests

```text
- default novaclient still uses NOVA_API_VERSION=2.53.
- evacuate_instance_stopped uses CONF.staged_recovery.nova_evacuate_microversion.
- target host is omitted when None.
- target host is passed when reserved_host is set.
```

### task tests

```text
ReconcileStagedRecoveryTask:
- creates etcd state from VMove.
- marks already moved/stopped VM as EVACUATED_STOPPED.
- marks already ACTIVE VM as ACTIVE.
- does not overwrite original state.

EvacuateToStoppedTask:
- skips EVACUATED_STOPPED/ACTIVE/IGNORED rows.
- calls evacuate_instance_stopped.
- records dest_host in etcd and VMove.
- preserves lock/unlock behavior.
- marks FAILED on timeout.

BatchedStartInstancesTask:
- starts only originally active VMs.
- ignores originally stopped VMs.
- holds lease until ACTIVE/ERROR/TIMEOUT.
- releases lease on success and failure.
- fails notification if any originally active VM fails to start.
```

### multi-engine/cascade tests

Use two instances of limiter/task with same fake etcd store:

```text
- two notifications targeting same dest_host cannot create more than N live start leases.
- stale STARTING row with expired/missing lease is reconciled safely.
- already ACTIVE VM is not started again.
```

---

## Integration test plan

### Test 1: one failed compute

Setup:

```text
compute-01 has 10 ACTIVE HA-enabled VMs
compute-02/03 are available
max_parallel_starts_per_host = 2
```

Action:

```text
Fence/stop compute-01 using existing Masakari HA procedure.
Trigger/observe host failure notification.
```

Expected:

```text
All VMs are evacuated to available hosts.
Evacuated VMs remain stopped first.
Only two VMs at a time are STARTING per destination host.
All originally ACTIVE VMs eventually ACTIVE.
Originally stopped VMs remain stopped.
```

### Test 2: cascade compute failure

Setup:

```text
compute-01 and compute-02 fail nearly simultaneously.
Nova scheduler places recovered VMs on compute-03.
max_parallel_starts_per_host = 2
```

Expected:

```text
Across both notifications and all masakari-engine processes,
there are never more than 2 live etcd start-leases for compute-03.
```

### Test 3: masakari-engine failure during STARTING

Setup:

```text
Run at least two masakari-engine services.
Kill engine-A while it owns a start lease and waits for VM ACTIVE.
```

Expected:

```text
No duplicate evacuation.
No unthrottled start.
After lease TTL/reconcile, engine-B marks VM ACTIVE or retries safely.
```

### Test 4: etcd outage

Setup:

```text
Enable staged recovery.
Make etcd unavailable before batched start.
```

Expected:

```text
Task fails closed.
No uncontrolled nova start loop.
Error message clearly points to etcd/coordination backend.
```

---

## Acceptance criteria

The fork is acceptable when all conditions are true:

1. Staged recovery can be enabled via config without breaking existing non-staged workflows.
2. Nova evacuate uses microversion `2.95+` only for staged evacuation.
3. Evacuated VMs are first placed stopped on destination host.
4. Only originally ACTIVE VMs are auto-started.
5. etcd stores per-instance recovery state.
6. etcd TTL keys store per-destination-host start leases.
7. Across multiple notifications and multiple `masakari-engine` processes, live start leases per `dest_host` never exceed `max_parallel_starts_per_host` while etcd quorum is available.
8. Task retries are idempotent.
9. Engine crash during recovery does not cause duplicate evacuation or duplicate start storm.
10. If etcd is unavailable, staged start fails closed.
11. Unit tests cover state store, limiter, Nova microversion, task idempotency, and cascade/multi-engine behavior.
12. Operator documentation explains etcd prefix, TTL sizing, TLS, and failure behavior.

---

## Operator notes to include in docs/release note

Document these points:

```text
- staged recovery requires etcd available to masakari-engine;
- [coordination] backend_url must use a distributed backend, recommended etcd3+http(s);
- [staged_recovery] etcd_backend_url may reuse coordination backend_url;
- etcd_prefix should be unique per OpenStack region/cloud;
- etcd TTL must be sized to cover start_timeout + batch_delay + margin;
- etcd ACL should restrict Masakari to the configured prefix;
- TLS is recommended for etcd;
- if etcd is down, recovery fails closed rather than starting VMs unthrottled.
```

---

## Implementation order for Codex

1. Inspect local fork and confirm file layout.
2. Add `etcd3gw` dependency if needed.
3. Add `[staged_recovery]` config group.
4. Add Nova `api_version` parameter and `evacuate_instance_stopped()`.
5. Implement `staged_state_etcd.py`.
6. Implement `staged_limiter.py`.
7. Implement `staged_host_failure.py` tasks.
8. Add entry points in `setup.cfg`.
9. Add staged sample recovery config.
10. Add tests for etcd state, limiter, Nova microversion, tasks.
11. Add optional manager stale RUNNING reconcile.
12. Run existing Masakari unit tests and new tests.

---

## Definition of done

The fork must provide a documented Masakari staged recovery mode using etcd state storage that enforces:

```text
max_parallel_starts_per_host per destination hypervisor
```

across:

```text
- one failed compute host;
- multiple simultaneous failed compute hosts;
- multiple active masakari-engine processes;
- crash/restart of one masakari-engine during recovery.
```

The implementation must include:

```text
- code;
- config opts;
- etcd state store;
- etcd start lease limiter;
- TaskFlow task entry points;
- sample custom recovery config;
- unit tests;
- operator documentation/release note.
```
