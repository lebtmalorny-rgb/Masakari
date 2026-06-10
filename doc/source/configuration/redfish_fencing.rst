=============================
Redfish Host Failure Fencing
=============================

Masakari can fence failed compute hosts through Redfish before Nova evacuation
starts. The feature is optional and disabled by default. It is designed for the
etcd-backed staged recovery workflow, where recovery must be both:

* fenced before evacuation, to avoid split brain;
* rate-limited after evacuation, to avoid a boot storm.

The core safety rule is:

.. code-block:: text

   no verified Redfish PowerState=Off proof -> no Nova evacuate

If Redfish fencing cannot prove that every failed host in the failure set is
powered off, the host recovery workflow raises ``HostRecoveryFailureException``
and does not run Nova evacuation tasks.

What Was Added
--------------

The implementation adds these internal components:

* ``masakari.conf.redfish_fencing``: the ``[redfish_fencing]`` configuration
  group.
* ``masakari.engine.drivers.taskflow.redfish_fencing``: Redfish host mapping
  loader and direct Redfish client.
* ``masakari.engine.drivers.taskflow.fencing_state_etcd``: etcd-backed
  fencing state, proof records, and TTL locks.
* ``masakari.engine.drivers.taskflow.fenced_host_failure``: TaskFlow tasks
  that gate host recovery before evacuation.
* TaskFlow entry points:

  * ``collect_fencing_failure_set_task``
  * ``disable_failure_set_task``
  * ``redfish_fence_failure_set_task``
  * ``assert_failure_set_fenced_task``

* Sample files:

  * ``etc/masakari/masakari-redfish-fencing-methods.conf``
  * ``etc/masakari/redfish-fencing-hosts.yaml.sample``

* Operator documentation:

  * ``doc/source/configuration/redfish_fencing.rst``

* Release note:

  * ``releasenotes/notes/redfish-fencing-etcd-6ec1a3c66fd83bd0.yaml``

The host failure execution context now also carries ``segment_uuid`` and
``event_id`` into the TaskFlow store. ``event_id`` defaults to the notification
UUID. The fencing tasks use those values for failure grouping and etcd locks.

Recovery Flow
-------------

With Redfish fencing and staged recovery enabled, the host failure flow is:

.. code-block:: text

   host failure notification
     -> collect_fencing_failure_set_task
     -> disable_failure_set_task
     -> redfish_fence_failure_set_task
     -> assert_failure_set_fenced_task
     -> prepare_HA_enabled_instances_task
     -> reconcile_staged_recovery_task
     -> evacuate_to_stopped_task
     -> batched_start_instances_task

The important boundary is between ``assert_failure_set_fenced_task`` and
``prepare_HA_enabled_instances_task``. The evacuation path is reachable only
after the gate verifies persisted Redfish proof for all hosts in the failure
set.

Redfish HTTP Behavior
---------------------

For each host, the Redfish client performs this sequence:

1. Read the Redfish service root:

   .. code-block:: text

      GET /redfish/v1

2. Create a Redfish session when ``auth_type = session``:

   .. code-block:: text

      POST /redfish/v1/SessionService/Sessions
      {"UserName": "<username>", "Password": "<password>"}

   The client stores ``X-Auth-Token`` and deletes the session when the fencing
   attempt finishes.

3. Read the configured ``systems_uri``:

   .. code-block:: text

      GET /redfish/v1/Systems/1

4. Validate optional identity guards, such as ``expected_serial_number``.

5. If the system is not already ``PowerState = Off``, send:

   .. code-block:: text

      POST <reset_target>
      {"ResetType": "ForceOff"}

   ``ForceOff`` is the only configured reset type. Graceful shutdown, reboot,
   restart, power cycle, and push-button semantics are not used for the
   split-brain gate.

6. Poll ``systems_uri`` until ``PowerState = Off`` is observed for
   ``stable_power_state_reads`` consecutive reads.

7. Persist proof in etcd and allow the workflow to continue.

BMC Algorithm
-------------

The BMC part of the workflow is intentionally small and fail-closed. It is not
a general power-management integration. It performs only the operations needed
to prove that the failed compute host cannot continue running instances.

For each host in the failure set:

1. Load the host mapping by Nova/Masakari host name.
2. Read the BMC password from ``password_file`` or, only when explicitly
   allowed for lab use, from inline ``password``.
3. Open a Redfish session or configure basic authentication.
4. Read the configured ``systems_uri``.
5. Validate all configured identity guards before any power operation.
6. Read the reset action target and allowed reset types from
   ``Actions.#ComputerSystem.Reset``.
7. Reject the host if ``ForceOff`` is not in
   ``ResetType@Redfish.AllowableValues`` when that list is provided.
8. If ``PowerState`` is already ``Off``, skip the reset request and continue
   to stable-read verification.
9. If ``PowerState`` is not ``Off``, send ``{"ResetType": "ForceOff"}`` to
   the reset target.
10. Poll ``systems_uri`` until ``PowerState = Off`` is observed for
    ``stable_power_state_reads`` consecutive reads.
11. Return proof to the TaskFlow task.
12. Delete the Redfish session when session authentication was used.

The proof returned by the BMC algorithm is not enough by itself. The TaskFlow
task must persist it in etcd and the later gate task must read it back before
evacuation can proceed.

The BMC algorithm must not be used as a liveness detector for controller
services. It is a last-step isolation action for compute hosts that Masakari has
already selected for host failure recovery.

Backend / etcd State Algorithm
------------------------------

The backend part of the workflow owns three things:

* failure-set records;
* distributed locks;
* durable fencing proof.

The backend algorithm is:

1. ``collect_fencing_failure_set_task`` asserts that the Redfish fencing
   backend is available.
2. It writes a failure record under
   ``<prefix>/failures/<segment_uuid>/<event_id>/<hostname>``.
3. It creates or resets the host state under ``<prefix>/hosts/<hostname>`` to
   ``FENCE_REQUIRED``.
4. It lists recent failure records for the same segment. If
   ``multi_host_batch_window = 0``, only records with the same ``event_id`` are
   grouped. Otherwise, records inside the configured time window are grouped.
5. It validates the failure-set size against
   ``max_auto_fence_hosts_per_segment``.
6. It validates that enough non-failed compute hosts remain according to
   ``min_surviving_compute_hosts``.
7. ``redfish_fence_failure_set_task`` acquires a segment recovery lock under
   ``<prefix>/locks/segments/<segment_uuid>``.
8. For each host, it acquires a host fencing lock under
   ``<prefix>/locks/hosts/<hostname>``.
9. It transitions host state from ``FENCE_REQUIRED`` to ``FENCING`` with
   compare-and-replace semantics.
10. It runs the BMC algorithm.
11. It writes the BMC proof and transitions the host state to ``FENCED``.
12. If the BMC algorithm fails, it transitions the host state to
    ``FENCE_FAILED`` and raises an error.
13. It releases host locks and the segment lock. If an engine dies, etcd TTL
    leases eventually remove the lock keys.
14. ``assert_failure_set_fenced_task`` reads host states back from etcd and
    accepts only ``state = FENCED`` with ``verified_power_state = Off``.

This is why the feature fails closed when etcd is unavailable. Without the
backend, Masakari cannot coordinate concurrent engines or persist fencing
proof, so it must not send Redfish power operations and then evacuate blindly.

The backend is also the retry boundary. If an engine powers off a host but dies
before writing ``FENCED``, a later retry reads the BMC state again. If the BMC
now reports stable ``PowerState = Off``, the retry can persist proof and
continue. If proof cannot be persisted, evacuation stays blocked.

Configuration
-------------

Prerequisites
~~~~~~~~~~~~~

Before enabling the flow, verify these prerequisites:

* each compute host has a Redfish BMC endpoint reachable from
  ``masakari-engine``;
* the BMC account can read ``ComputerSystem`` and perform reset actions;
* the Redfish ``systems_uri`` points to the physical system that hosts the Nova
  compute service;
* etcd is available through ``etcd3gw``;
* staged recovery is configured if the workflow uses
  ``reconcile_staged_recovery_task``, ``evacuate_to_stopped_task``, and
  ``batched_start_instances_task``.

etcd
~~~~

Configure etcd through ``[coordination] backend_url`` or directly through
``[redfish_fencing] etcd_backend_url``:

.. code-block:: ini

   [coordination]
   backend_url = etcd3+http://etcd.example.internal:2379

If ``[redfish_fencing] etcd_backend_url`` is empty, the fencing store reuses
``[staged_recovery] etcd_backend_url``. If that is empty too, it reuses
``[coordination] backend_url``.

Redfish fencing options
~~~~~~~~~~~~~~~~~~~~~~~

Enable the feature in ``masakari.conf``:

.. code-block:: ini

   [redfish_fencing]
   enabled = true
   hosts_config_path = /etc/masakari/redfish-fencing-hosts.yaml

   # etcd state and lock namespace.
   etcd_prefix = /masakari/redfish-fencing/v1
   etcd_timeout = 5

   # Redfish fencing behavior.
   reset_type = ForceOff
   expected_power_state = Off
   power_off_timeout = 180
   poll_interval = 5
   stable_power_state_reads = 2
   connect_timeout = 5
   read_timeout = 30
   max_attempts = 3
   retry_interval = 5

   # Distributed lock TTL.
   lock_ttl = 300

   # Multi-host safety gates.
   multi_host_batch_window = 30
   max_auto_fence_hosts_per_segment = 1
   min_surviving_compute_hosts = 1

Use ``multi_host_batch_window = 0`` when you want to group only failure records
with the same ``event_id``. Use a positive value to group recent host failures
in the same segment into one fencing set.

Use conservative initial values for ``max_auto_fence_hosts_per_segment`` and
``min_surviving_compute_hosts``. For example, keeping
``max_auto_fence_hosts_per_segment = 1`` prevents automatic fencing of multiple
hosts until the operator has tested the inventory and BMC behavior.

Staged recovery options
~~~~~~~~~~~~~~~~~~~~~~~

The sample Redfish workflow also uses staged recovery tasks. Enable staged
recovery when using that workflow:

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

``slot_lease_ttl`` should cover ``start_timeout + batch_delay`` plus a safety
margin.

Host mapping
~~~~~~~~~~~~

Create ``/etc/masakari/redfish-fencing-hosts.yaml`` from the installed sample:

.. code-block:: yaml

   schema_version: 1
   defaults:
     scheme: https
     port: 443
     base_uri: /redfish/v1
     auth_type: session
     tls_verify: true
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

The ``hosts`` keys must match the host names used by Masakari and Nova compute
services. This is usually the same value that appears in the host failure
notification and in ``openstack compute service list``.

Use ``password_file`` for production deployments. Inline ``password`` values
are rejected unless this option is explicitly enabled:

.. code-block:: ini

   [redfish_fencing]
   allow_insecure_inline_password = true

That option is intended only for lab or unit-test environments.

Identity guards
~~~~~~~~~~~~~~~

Each host mapping may include optional anti-miswire guards:

* ``expected_system_uuid``
* ``expected_serial_number``
* ``expected_asset_tag``
* ``expected_manufacturer``
* ``expected_model``

If any configured guard does not match the Redfish ``ComputerSystem`` response,
Masakari refuses to send ``ForceOff`` and records ``FENCE_FAILED``. Use at
least one stable guard, preferably serial number or system UUID, before enabling
automatic fencing outside a lab.

TaskFlow workflow
~~~~~~~~~~~~~~~~~

Use the installed ``masakari-redfish-fencing-methods.conf`` as the recovery
workflow reference. It configures these phases:

.. code-block:: text

   pre:
     collect_fencing_failure_set_task
     disable_failure_set_task
     redfish_fence_failure_set_task
     assert_failure_set_fenced_task
   main:
     prepare_HA_enabled_instances_task
     reconcile_staged_recovery_task
     evacuate_to_stopped_task
   post:
     batched_start_instances_task

The exact deployment mechanism depends on how the cloud manages
``recovery_workflow_sample_config.conf``. The important part is that the four
fencing tasks must run before any task that prepares or evacuates instances.

Restart ``masakari-engine`` after changing ``masakari.conf`` or the recovery
workflow file.

etcd State
----------

Fencing state is stored under ``[redfish_fencing] etcd_prefix``:

.. code-block:: text

   <prefix>/failures/<segment_uuid>/<event_id>/<hostname>
   <prefix>/hosts/<hostname>
   <prefix>/locks/segments/<segment_uuid>
   <prefix>/locks/hosts/<hostname>

The host state moves through:

.. code-block:: text

   FENCE_REQUIRED -> FENCING -> FENCED
                              -> FENCE_FAILED

``FENCED`` records include Redfish proof fields:

* ``systems_uri``
* ``reset_target``
* ``requested_reset_type``
* ``verified_power_state``
* ``power_state_reads``
* ``updated_at``
* ``owner``

The gate task accepts only records with ``state = FENCED`` and
``verified_power_state = Off``.

Multiple Failed Nodes
---------------------

Masakari can receive several host failure notifications close together. Redfish
fencing handles this as a failure set for one failover segment.

Single compute failure
~~~~~~~~~~~~~~~~~~~~~~

For one failed compute host:

1. The failure set contains only that host.
2. The host is disabled in Nova.
3. The host is fenced through Redfish.
4. The gate verifies one ``FENCED`` proof record.
5. Staged evacuation starts.

This is the default and safest rollout mode. Keep
``max_auto_fence_hosts_per_segment = 1`` until this path is proven in the
target cloud.

Multiple compute failures in one segment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For multiple failed compute hosts in the same segment:

1. Each notification writes a failure record in etcd.
2. ``collect_fencing_failure_set_task`` groups recent failures according to
   ``multi_host_batch_window`` and ``event_id``.
3. The grouped set is sorted and deduplicated.
4. If the set size is greater than ``max_auto_fence_hosts_per_segment``, the
   workflow stops before BMC power operations.
5. If the number of non-failed compute hosts is lower than
   ``min_surviving_compute_hosts``, the workflow stops before BMC power
   operations.
6. If both safety checks pass, the task acquires one segment lock and then one
   host lock per host.
7. Redfish fencing runs for every host in the set.
8. Evacuation starts only when every host has valid ``FENCED`` proof.

If any host in the set fails identity validation, Redfish authentication, reset,
or stable ``PowerState = Off`` verification, that host is marked
``FENCE_FAILED`` and the whole failure-set evacuation is blocked. This is
intentional. Evacuating instances from only part of a suspected multi-host
failure can still create split-brain risk for the unverified hosts.

Concurrent notifications
~~~~~~~~~~~~~~~~~~~~~~~~

The segment lock prevents two engines from fencing or evacuating the same
segment failure set at the same time. Host locks prevent concurrent Redfish
operations against the same BMC.

If two notifications race:

* one engine obtains the segment lock and proceeds;
* another engine sees the lock and fails the workflow for that attempt;
* normal notification retry can run again after the lock is released or its TTL
  expires;
* existing ``FENCED`` proof is reused on retry after it is verified.

The locks are TTL-backed because the recovery path can be interrupted by an
engine process or controller failure. A retry after TTL expiry must still read
and validate host state; lock expiry alone is never treated as fencing proof.

Control-Plane Node Failures
---------------------------

The Redfish fencing gate is scoped to Masakari host failure recovery for
compute hosts. It does not replace the HA design for OpenStack controller
services such as Keystone, Nova API, the database, RabbitMQ, HAProxy, or etcd.

Control node failed, quorum remains
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a control-plane node fails but the required services remain available
through the remaining controllers, recovery can continue:

* another ``masakari-engine`` service can process or retry the notification;
* etcd locks and state remain usable if etcd quorum is intact;
* Nova service disable and evacuation calls can continue if Nova API and
  Keystone remain reachable;
* a lock left by the failed engine expires after ``lock_ttl``.

In this case the algorithm is the same as a normal compute-host recovery. The
important requirement is that the surviving control plane can still serve
Masakari's dependencies.

Control node failed, backend unavailable
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If the failed control-plane node makes etcd unavailable, or if etcd quorum is
lost, the fencing workflow fails closed before Redfish power operations. The
engine cannot safely coordinate locks or persist proof.

Expected behavior:

* ``collect_fencing_failure_set_task`` or ``redfish_fence_failure_set_task``
  raises an error while checking or writing backend state;
* no valid ``FENCED`` proof is created;
* ``assert_failure_set_fenced_task`` cannot pass;
* Nova evacuation does not start.

The operator must restore etcd quorum or point Masakari to a healthy etcd
backend before retrying recovery.

Control node failed, Nova or Keystone unavailable
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If Nova API or Keystone is unavailable, the workflow also cannot complete:

* Nova service disable may fail before fencing starts;
* evacuation cannot start even if an earlier retry already fenced the host;
* persisted ``FENCED`` proof remains in etcd and can be reused after the
  control plane recovers.

This can produce a state where the failed compute host has been powered off,
but evacuation has not completed yet. That is safer than evacuating without
proof. After Nova and Keystone recover, retry the notification or let normal
unfinished-notification processing continue.

Control node is also a compute host
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some small deployments place compute workloads on nodes that also run control
services. Masakari should fence such a node only if all of these are true:

* the node is registered in the Masakari segment as a host that should be
  recovered;
* its host name has a Redfish mapping;
* the remaining control plane can still provide etcd, Nova, Keystone, database,
  and messaging services;
* ``min_surviving_compute_hosts`` and
  ``max_auto_fence_hosts_per_segment`` allow the failure set.

If fencing that node would remove the last healthy controller, the operator
should not allow automatic fencing. Keep controller quorum and service topology
outside the Redfish fencing task's assumptions; encode the desired safety
margin with segment design, host mappings, and conservative fencing limits.

Recommended control-plane policy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use these policy rules for production:

* Do not put pure control-plane nodes in ``redfish-fencing-hosts.yaml`` unless
  there is a separate, reviewed operational reason.
* Keep etcd for fencing state highly available and independent enough that one
  controller loss does not remove quorum.
* Start with ``max_auto_fence_hosts_per_segment = 1``.
* Set ``min_surviving_compute_hosts`` high enough to prevent evacuating into an
  undersized or partially failed segment.
* For converged controller-compute nodes, test the exact failure mode in a lab
  before enabling automatic Redfish fencing.

Verification
------------

Static verification
~~~~~~~~~~~~~~~~~~~

Run the focused unit tests:

.. code-block:: console

   $ python -m unittest \
       masakari.tests.unit.engine.drivers.taskflow.test_redfish_fencing \
       masakari.tests.unit.engine.drivers.taskflow.test_fencing_state_etcd \
       masakari.tests.unit.engine.drivers.taskflow.test_fenced_host_failure

Expected result:

.. code-block:: text

   Ran 12 tests
   OK

Run syntax checks for the new modules:

.. code-block:: console

   $ python -m py_compile \
       masakari/engine/drivers/taskflow/redfish_fencing.py \
       masakari/engine/drivers/taskflow/fencing_state_etcd.py \
       masakari/engine/drivers/taskflow/fenced_host_failure.py \
       masakari/conf/redfish_fencing.py

Check that the configuration group is registered and the sample YAML parses:

.. code-block:: console

   $ python -c "from masakari.conf import opts; \
       assert 'redfish_fencing' in dict(opts.list_opts())"
   $ python -c "import yaml; \
       yaml.safe_load(open('etc/masakari/redfish-fencing-hosts.yaml.sample'))"

BMC connectivity checks
~~~~~~~~~~~~~~~~~~~~~~~

Before enabling automatic fencing, validate each BMC mapping manually from the
same network namespace or host where ``masakari-engine`` runs.

Read the service root:

.. code-block:: console

   $ curl --fail --cacert /etc/masakari/certs/bmc-ca.pem \
       https://10.10.20.101:443/redfish/v1

Create a Redfish session:

.. code-block:: console

   $ curl --fail --cacert /etc/masakari/certs/bmc-ca.pem \
       -D /tmp/redfish.headers \
       -H 'Content-Type: application/json' \
       -d '{"UserName":"masakari-fencer","Password":"<secret>"}' \
       https://10.10.20.101:443/redfish/v1/SessionService/Sessions

Read the configured system URI:

.. code-block:: console

   $ TOKEN=$(awk '/X-Auth-Token:/ {print $2}' /tmp/redfish.headers)
   $ curl --fail --cacert /etc/masakari/certs/bmc-ca.pem \
       -H "X-Auth-Token: $TOKEN" \
       https://10.10.20.101:443/redfish/v1/Systems/1

Confirm that the response contains the values used by the host mapping guards,
for example ``SerialNumber`` or ``UUID``.

Do not send ``ForceOff`` to production hosts during this connectivity check
unless the host is in a controlled maintenance window.

Lab fencing smoke test
~~~~~~~~~~~~~~~~~~~~~~

Use a lab compute host or a host already in a maintenance window.

1. Enable Redfish fencing and staged recovery.
2. Configure one host in ``redfish-fencing-hosts.yaml``.
3. Keep ``max_auto_fence_hosts_per_segment = 1``.
4. Trigger or replay a host failure notification for the lab host.
5. Verify that ``masakari-engine`` logs show the fencing tasks before
   evacuation tasks.
6. Verify that the BMC reports ``PowerState = Off``.
7. Verify that the etcd host state is ``FENCED``.
8. Verify that evacuation starts only after the fencing proof exists.

With ``etcdctl`` configured for the same etcd cluster, inspect the state:

.. code-block:: console

   $ etcdctl get --prefix /masakari/redfish-fencing/v1/hosts/
   $ etcdctl get --prefix /masakari/redfish-fencing/v1/failures/

A successful host proof should include values similar to:

.. code-block:: json

   {
     "state": "FENCED",
     "hostname": "compute-01",
     "method": "redfish",
     "systems_uri": "/redfish/v1/Systems/1",
     "reset_target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
     "requested_reset_type": "ForceOff",
     "verified_power_state": "Off",
     "power_state_reads": 2
   }

Fail-closed checks
~~~~~~~~~~~~~~~~~~

Before production rollout, verify the negative paths in a lab:

* Configure a wrong ``expected_serial_number`` and confirm that Masakari records
  ``FENCE_FAILED`` and does not evacuate.
* Configure an unreachable BMC address and confirm that recovery fails closed
  after ``max_attempts``.
* Set ``max_auto_fence_hosts_per_segment = 1`` and create two host failure
  records in the same segment window. Confirm that Masakari refuses the
  automatic fencing set.
* Hold a host lock key in etcd and confirm that a second fencing attempt does
  not run concurrently for the same host.

Production rollout checklist
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use this order for rollout:

1. Build and deploy packages that include the Redfish fencing modules and task
   entry points.
2. Populate ``redfish-fencing-hosts.yaml`` for a small number of hosts.
3. Validate every BMC endpoint and identity guard manually.
4. Enable ``[redfish_fencing] enabled = true`` on one Masakari engine group.
5. Use the Redfish fencing workflow only for a test segment first.
6. Run a maintenance-window smoke test on one compute host.
7. Inspect etcd proof and Masakari logs.
8. Expand the host mapping and segments gradually.
9. Increase ``max_auto_fence_hosts_per_segment`` only after multi-host behavior
   has been tested.

Troubleshooting
---------------

``No Redfish host mapping found``
    The host name in the notification does not match a key under ``hosts`` in
    ``redfish-fencing-hosts.yaml``.

``Inline Redfish password ... is disabled``
    The mapping uses ``password`` while
    ``allow_insecure_inline_password = false``. Move the secret to
    ``password_file``.

``Redfish system identity does not match expected host``
    One of the ``expected_*`` guards does not match the BMC response. Check the
    inventory, ``systems_uri``, and physical cabling before retrying.

``does not allow reset type ForceOff``
    The Redfish ``ResetType@Redfish.AllowableValues`` response does not include
    ``ForceOff``. The host is not accepted for this fencing method.

``Timed out waiting for Redfish PowerState=Off``
    The BMC accepted the request but did not report stable ``Off`` reads before
    ``power_off_timeout``. Check BMC health, permissions, and host power state.

``Redfish segment recovery lock is already held``
    Another engine is processing the same segment recovery event, or a previous
    engine died and the lock TTL has not expired yet.

``Redfish fencing proof is missing or invalid``
    ``assert_failure_set_fenced_task`` could not find valid ``FENCED`` proof
    for every host in the failure set. Evacuation remains blocked by design.
