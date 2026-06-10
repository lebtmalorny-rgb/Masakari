=============================
Redfish Host Failure Fencing
=============================

Masakari can be configured to fence failed compute hosts through Redfish before
Nova evacuation starts. This mode writes fencing state and proof to etcd and
fails closed:

.. code-block:: text

   no verified Redfish PowerState=Off proof -> no Nova evacuate

This feature is optional and disabled by default. It is intended to be used with
the etcd-backed staged recovery workflow so that recovery is both fenced before
evacuation and rate-limited after evacuation.

Required Configuration
----------------------

Configure etcd through ``[coordination] backend_url`` or
``[redfish_fencing] etcd_backend_url``:

.. code-block:: ini

   [coordination]
   backend_url = etcd3+http://etcd.example.internal:2379

Enable Redfish fencing:

.. code-block:: ini

   [redfish_fencing]
   enabled = true
   hosts_config_path = /etc/masakari/redfish-fencing-hosts.yaml
   etcd_prefix = /masakari/redfish-fencing/v1
   reset_type = ForceOff
   expected_power_state = Off
   power_off_timeout = 180
   poll_interval = 5
   stable_power_state_reads = 2
   max_attempts = 3
   retry_interval = 5
   multi_host_batch_window = 30
   max_auto_fence_hosts_per_segment = 1
   min_surviving_compute_hosts = 1

Configure a Redfish host mapping. The installed
``etc/masakari/redfish-fencing-hosts.yaml.sample`` file contains a template:

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

Use ``password_file`` for production deployments. Inline ``password`` values
are rejected unless ``allow_insecure_inline_password`` is explicitly enabled.

TaskFlow
--------

The installed ``etc/masakari/masakari-redfish-fencing-methods.conf`` file shows
the fence-before-evacuate task sequence:

.. code-block:: text

   collect_fencing_failure_set_task
   disable_failure_set_task
   redfish_fence_failure_set_task
   assert_failure_set_fenced_task
   prepare_HA_enabled_instances_task
   reconcile_staged_recovery_task
   evacuate_to_stopped_task
   batched_start_instances_task

The fencing tasks run before ``prepare_HA_enabled_instances_task`` and
``evacuate_to_stopped_task``. If Redfish fencing fails, the host recovery
workflow raises ``HostRecoveryFailureException`` and evacuation is not started.

Host Mapping Guards
-------------------

Each host mapping may include optional identity guards:

* ``expected_system_uuid``
* ``expected_serial_number``
* ``expected_asset_tag``
* ``expected_manufacturer``
* ``expected_model``

If any configured guard does not match the Redfish ``ComputerSystem`` response,
Masakari refuses to send ``ForceOff`` and records a failed fencing state. These
guards reduce the risk of powering off the wrong physical server because of an
inventory or cabling mistake.

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

``FENCED`` records include Redfish proof such as ``systems_uri``,
``reset_target``, ``requested_reset_type``, ``verified_power_state``, and the
number of stable power-state reads. The gate task accepts only
``verified_power_state = Off``.

Multi-Host Safety
-----------------

``multi_host_batch_window`` groups recent host failures in the same segment.
When it is ``0``, only failures with the same event id are grouped.

``max_auto_fence_hosts_per_segment`` limits how many hosts Masakari may fence
automatically in one segment recovery event. ``min_surviving_compute_hosts``
requires a minimum number of non-failed compute hosts to remain before fencing
is allowed. Both checks are evaluated before Redfish power operations start.
