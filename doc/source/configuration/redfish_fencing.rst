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
