===============================
Staged Host Failure Recovery
===============================

Masakari can be configured to evacuate instances after a compute host failure
and then start the evacuated instances in controlled batches per destination
hypervisor. This mode uses Nova evacuate microversion ``2.95`` or newer so
evacuated servers remain stopped until Masakari starts them.

Staged recovery is disabled by default. To use it, configure the staged task
flow and enable the ``[staged_recovery]`` options.

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
