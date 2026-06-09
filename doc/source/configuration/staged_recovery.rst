===============================
Staged Host Failure Recovery
===============================

Masakari can be configured to evacuate instances after a compute host failure
and then start the evacuated instances in controlled batches per destination
hypervisor. This mode uses Nova evacuate microversion ``2.95`` or newer so
evacuated servers remain stopped until Masakari starts them.

Staged recovery is disabled by default. To use it, configure the staged task
flow and enable the ``[staged_recovery]`` options.

Implemented Components
----------------------

The staged recovery implementation adds these components to Masakari:

* A ``[staged_recovery]`` configuration group for enabling staged recovery,
  selecting etcd state storage, tuning per-host start concurrency, selecting
  the Nova evacuation microversion, and configuring stale recovery
  reconciliation.
* Nova wrapper methods that can use a dedicated microversion for
  evacuate-to-stopped behavior without changing the default Nova client
  microversion used by existing Masakari workflows.
* Three TaskFlow tasks registered through ``masakari.task_flow.tasks``:
  ``reconcile_staged_recovery_task``, ``evacuate_to_stopped_task``, and
  ``batched_start_instances_task``.
* An etcd state store for idempotent per-instance staged recovery state and
  per-destination-host start lease keys.
* A start limiter that combines a short Tooz lock with etcd TTL leases. The
  lock protects only slot allocation; the TTL lease represents the long-running
  start slot.
* A manager periodic task that detects stale in-progress staged recovery state
  and marks the matching running notification ``ERROR`` so normal unfinished
  notification processing can retry it.
* A separate staged workflow sample file,
  ``etc/masakari/masakari-staged-recovery-methods.conf``. The default recovery
  workflow file is unchanged.

Required Configuration
----------------------

Configure coordination with an etcd backend:

.. code-block:: ini

   [coordination]
   backend_url = etcd3+http://etcd.example.internal:2379

Enable staged recovery:

.. code-block:: ini

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

If ``[staged_recovery] etcd_backend_url`` is empty, Masakari reuses
``[coordination] backend_url`` for direct etcd state access.

Configure the host recovery flow with the staged tasks. The installed
``etc/masakari/masakari-staged-recovery-methods.conf`` file contains a sample:

.. code-block:: ini

   [taskflow_driver_recovery_flows]
   host_auto_failure_recovery_tasks = pre:['disable_compute_service_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']
   host_rh_failure_recovery_tasks = pre:['disable_compute_service_task'],main:['prepare_HA_enabled_instances_task', 'reconcile_staged_recovery_task', 'evacuate_to_stopped_task'],post:['batched_start_instances_task']

Behavior
--------

The staged workflow keeps per-instance recovery state in etcd under
``[staged_recovery] etcd_prefix``. Start concurrency is controlled with etcd
TTL lease keys per destination host. A short Tooz lock protects only the slot
allocation section; it is not held while Nova starts an instance.

``slot_lease_ttl`` must be greater than or equal to ``start_timeout`` plus
``batch_delay``. This prevents a second engine from reusing a start slot before
the first start attempt has either completed or timed out.

If staged recovery is enabled but etcd or coordination is unavailable, Masakari
fails closed instead of starting an unlimited number of instances.

Only instances whose original ``vm_state`` was ``active`` are automatically
started when ``start_only_originally_active`` is true. Instances that were
already stopped before the host failure remain stopped.

Failure Handling
----------------

The engine periodically checks stale staged recovery state. If etcd shows an
in-progress staged recovery that has not been updated for
``stale_recovery_timeout`` seconds, the corresponding running notification is
marked ``ERROR`` under a distributed lock. Existing unfinished notification
processing can then retry the workflow and continue from the persisted etcd
state.

REST API
--------

The staged recovery API extension is an admin API for runtime control and
diagnostics. The same routes are available with and without the project ID
prefix because the v1 router registers both forms.

``GET /v1/{project_id}/staged-recovery/start-limit``
    Shows the effective start limit:

    .. code-block:: json

       {
         "start_limit": {
           "max_parallel_starts_per_host": 2,
           "source": "config"
         }
       }

    ``source`` is ``runtime`` when an etcd override is set and ``config`` when
    the value comes from ``masakari.conf``.

``PUT /v1/{project_id}/staged-recovery/start-limit``
    Sets the runtime override in etcd:

    .. code-block:: json

       {
         "start_limit": {
           "max_parallel_starts_per_host": 3
         }
       }

    The value must be an integer greater than or equal to ``1``.

``DELETE /v1/{project_id}/staged-recovery/start-limit``
    Clears the runtime override. New start-slot allocations then use
    ``[staged_recovery] max_parallel_starts_per_host``.

``GET /v1/{project_id}/staged-recovery/instances``
    Lists staged recovery instance state. Supported query parameters are
    ``notification_uuid``, ``instance_uuid``, ``source_host``, ``dest_host``,
    ``step``, and ``limit``.

``GET /v1/{project_id}/staged-recovery/leases``
    Lists live start leases. Supported query parameters are
    ``notification_uuid``, ``instance_uuid``, ``dest_host``, and ``limit``.
    Filtering by ``dest_host`` narrows the etcd prefix scan.

List APIs default to ``limit=100`` and reject values less than ``1`` or greater
than ``1000``. Use filters for large clusters; an unfiltered diagnostic request
still scans the staged recovery etcd prefix before applying the response limit.

Example:

.. code-block:: console

   $ export MASAKARI_API=http://masakari-api.example.com:15868
   $ curl -s -H "X-Auth-Token: $OS_TOKEN" \
       "$MASAKARI_API/v1/$OS_PROJECT_ID/staged-recovery/start-limit"

Internal Python APIs and Entry Points
-------------------------------------

The staged recovery feature also adds internal Python APIs and TaskFlow entry
points intended for Masakari engine code and operator-selected recovery flows.

Nova wrapper
~~~~~~~~~~~~

``masakari.compute.nova.novaclient(context, timeout=None, api_version='2.53')``
    Creates a Nova client for the requested microversion. Existing callers use
    the default ``2.53`` value. Staged recovery passes
    ``[staged_recovery] nova_evacuate_microversion`` when it needs Nova's
    evacuate-to-stopped behavior.

``masakari.compute.nova.API.evacuate_instance_stopped(context, uuid, target=None)``
    Evacuates a server using the staged recovery Nova microversion. If
    ``target`` is ``None``, no destination host is passed and Nova scheduler
    selects the target host. If ``target`` is set, it is passed as ``host`` for
    reserved-host recovery. The method does not use ``force=True``.

``masakari.compute.nova.API.get_server_with_microversion(context, uuid, api_version)``
    Fetches a server using a caller-selected Nova microversion. This is a small
    helper for code that needs to inspect Nova state with a non-default
    microversion.

TaskFlow entry points
~~~~~~~~~~~~~~~~~~~~~

``reconcile_staged_recovery_task``
    Maps to ``ReconcileStagedRecoveryTask``. It reads ``VMove`` records and Nova
    server state, then creates or updates etcd instance state. It is safe to run
    on retry because it uses create-or-get semantics and does not overwrite the
    original VM state.

``evacuate_to_stopped_task``
    Maps to ``EvacuateToStoppedTask``. It processes pending and ongoing
    ``VMove`` records, locks instances while evacuating, calls
    ``evacuate_instance_stopped()``, records the destination host, and marks the
    etcd state ``EVACUATED_STOPPED`` or ``ACTIVE``.

``batched_start_instances_task``
    Maps to ``BatchedStartInstancesTask``. It starts only candidates whose etcd
    state is ``EVACUATED_STOPPED`` and whose original VM state was ``active``
    when ``start_only_originally_active`` is true. It holds a start slot until
    the server becomes ``ACTIVE``, goes ``ERROR``, or the configured timeout is
    reached.

etcd state store
~~~~~~~~~~~~~~~~

``EtcdStagedRecoveryStore``
    Owns staged recovery state under ``[staged_recovery] etcd_prefix``. The
    main instance-state methods are ``create_or_get_instance_state()``,
    ``get_instance_state()``, ``list_instance_states()``,
    ``list_stale_instance_states()``, ``update_instance_state()``,
    ``transition_instance_state()``, and ``mark_failed()``.

    The start-lease methods are ``list_start_leases()``,
    ``list_all_start_leases()``, ``create_start_lease()``,
    ``refresh_start_lease()``, and ``release_start_lease()``. Lease keys are
    stored under
    ``<etcd_prefix>/start-leases/<encoded_dest_host>/<instance_uuid>`` and are
    attached to etcd TTL leases.

    The diagnostic methods ``list_all_instance_states()`` and
    ``list_all_start_leases()`` support exact-match filters and a response
    limit for the REST API.

``encode_key_part(value)`` and ``decode_key_part(value)``
    Encode etcd key path components so host names and UUID-like values cannot
    break the key layout.

Start limiter
~~~~~~~~~~~~~

``EtcdStartLimiter.acquire(notification_uuid, instance_uuid, dest_host)``
    Acquires a start slot for a destination host. It first obtains the Tooz lock
    ``staged-start-lock-<dest_host>``, counts live etcd start leases, creates a
    new TTL lease key when capacity is available, releases the lock, and returns
    a ``StartSlot`` object. If coordination is unavailable, it raises a
    Masakari exception instead of allowing unbounded starts.

``EtcdStartLimiter.release(slot)``
    Deletes the start lease key and revokes the associated etcd lease.

Runtime Limit Management
~~~~~~~~~~~~~~~~~~~~~~~~

The configured ``max_parallel_starts_per_host`` value can be overridden at
runtime in etcd. The engine reads the effective value when allocating new start
slots, so changing the runtime override does not require restarting
``masakari-engine``.

Use ``masakari-manage`` on a host or container with access to Masakari
configuration:

.. code-block:: console

   $ masakari-manage staged_recovery get_start_limit
   $ masakari-manage staged_recovery set_start_limit 3
   $ masakari-manage staged_recovery clear_start_limit

``clear_start_limit`` removes the runtime override. After that, the effective
limit falls back to ``[staged_recovery] max_parallel_starts_per_host`` from
``masakari.conf``.

The Masakari server now exposes REST API support for this operation. Native
OpenStackClient commands such as ``openstack masakari staged recovery start
limit set`` still need to be added in the ``python-masakariclient`` plugin
repository.

Manager reconciliation
~~~~~~~~~~~~~~~~~~~~~~

``MasakariManager._process_stale_staged_recoveries(context)``
    Periodically scans etcd for stale ``EVACUATING``,
    ``WAITING_START_SLOT``, and ``STARTING`` states. For each affected
    notification it acquires ``staged-recovery-notification-<uuid>`` and marks
    a still-running notification ``ERROR``. This deliberately does not start a
    second workflow directly; it lets existing unfinished notification handling
    retry the idempotent staged tasks.
