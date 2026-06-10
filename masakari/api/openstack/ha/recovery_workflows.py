# Copyright 2026
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Recovery workflow API extension."""

from http import HTTPStatus

from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
import masakari.conf
from masakari.policies import recovery_workflows as recovery_policies


CONF = masakari.conf.CONF
ALIAS = 'admin-recovery-workflows'

STAGED_TASKS = [
    {
        'name': 'reconcile_staged_recovery_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'staged_host_failure:ReconcileStagedRecoveryTask'),
        'builtin': True,
        'allowed': True,
    },
    {
        'name': 'evacuate_to_stopped_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'staged_host_failure:EvacuateToStoppedTask'),
        'builtin': True,
        'allowed': True,
    },
    {
        'name': 'batched_start_instances_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'staged_host_failure:BatchedStartInstancesTask'),
        'builtin': True,
        'allowed': True,
    },
]

STANDARD_TASKS = [
    {
        'name': 'disable_compute_service_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'host_failure:DisableComputeServiceTask'),
        'builtin': True,
        'allowed': True,
    },
    {
        'name': 'prepare_HA_enabled_instances_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'host_failure:PrepareHAEnabledInstancesTask'),
        'builtin': True,
        'allowed': True,
    },
    {
        'name': 'evacuate_instances_task',
        'entry_point': ('masakari.engine.drivers.taskflow.'
                        'host_failure:EvacuateInstancesTask'),
        'builtin': True,
        'allowed': True,
    },
]

TEMPLATES = [
    {
        'name': 'standard',
        'description': 'Default Masakari host failure workflow',
        'tasks': [task['name'] for task in STANDARD_TASKS],
    },
    {
        'name': 'staged_recovery_etcd',
        'description': 'Evacuate to stopped, then start instances in batches',
        'tasks': [task['name'] for task in STAGED_TASKS],
    },
]


class RecoveryWorkflowsController(wsgi.Controller):
    """Read-only recovery workflow metadata for Horizon."""

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']

        if id == 'schema':
            context.can(recovery_policies.RECOVERY_WORKFLOWS % 'schema')
            return {'schema': {
                'available_tasks': STANDARD_TASKS + STAGED_TASKS,
                'templates': TEMPLATES,
            }}

        if id == 'effective':
            context.can(recovery_policies.RECOVERY_WORKFLOWS % 'effective')
            return {'workflow': {
                'staged_recovery_enabled': CONF.staged_recovery.enabled,
                'templates': TEMPLATES,
            }}

        raise exc.HTTPNotFound()


class RecoveryWorkflows(extensions.V1APIExtensionBase):
    """Recovery workflow metadata."""

    name = "RecoveryWorkflows"
    alias = ALIAS
    version = 1

    def get_resources(self):
        return [
            extensions.ResourceExtension(ALIAS,
                                         RecoveryWorkflowsController(),
                                         member_name='admin_recovery_workflow')
        ]

    def get_controller_extensions(self):
        return []
