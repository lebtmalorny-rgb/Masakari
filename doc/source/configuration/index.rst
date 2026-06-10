===================
Configuration Guide
===================

The configuration for masakari lies in below described files.

Configuration
-------------

Masakari has two main config files:
``masakari.conf`` and ``recovery_workflow_sample_config.conf``.

* :doc:`Config Reference <config>`: A complete reference of all
  config points in masakari and what they impact.

.. only:: html

   * :doc:`Sample Config File <sample_config>`: A sample config
     file with inline documentation.

* :doc:`Recovery Config Reference <recovery_config>`: A complete reference of all
  config points in masakari and what they impact.

.. only:: html

   * :doc:`Sample recovery workflow File <recovery_workflow_sample_config>`: A
     complete reference of defining the monitoring processes.

* :doc:`Staged Recovery <staged_recovery>`: Operator guide for etcd-backed
  staged VM start after host failure recovery.

* :doc:`Redfish Fencing <redfish_fencing>`: Operator guide for fencing failed
  compute hosts before evacuation.

Policy
------

Masakari, like most OpenStack projects, uses a policy language to restrict
permissions on REST API actions.

* :doc:`Policy Reference <policy>`: A complete reference of all
  policy points in masakari and what they impact.

* :doc:`Sample policy File <sample_policy>`: A sample policy
  file with inline documentation.


API configuration settings
--------------------------

* :doc:`API configuration <api-paste.ini>`: A complete reference of API
  configuration settings.
