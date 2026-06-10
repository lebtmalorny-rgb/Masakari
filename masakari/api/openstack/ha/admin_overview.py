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

"""Admin overview API extension."""

from http import HTTPStatus

from oslo_utils import timeutils
from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
import masakari.conf
from masakari.policies import admin_overview as admin_overview_policies


CONF = masakari.conf.CONF
ALIAS = 'admin-overview'


def _utcnow():
    return timeutils.utcnow().isoformat() + 'Z'


class AdminOverviewController(wsgi.Controller):
    """Aggregated admin overview for Horizon."""

    def _staged_recovery_summary(self):
        return {
            'enabled': CONF.staged_recovery.enabled,
            'max_parallel_starts_per_host': (
                CONF.staged_recovery.max_parallel_starts_per_host),
        }

    @extensions.expected_errors(HTTPStatus.FORBIDDEN)
    def index(self, req):
        context = req.environ['masakari.context']
        context.can(admin_overview_policies.ADMIN_OVERVIEW % 'index')

        return {'overview': {
            'generated_at': _utcnow(),
            'masakari_api': {
                'status': 'ok',
                'version': '1.0',
            },
            'staged_recovery': self._staged_recovery_summary(),
        }}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        if id != 'health':
            raise exc.HTTPNotFound()

        context = req.environ['masakari.context']
        context.can(admin_overview_policies.ADMIN_OVERVIEW % 'health')

        staged_status = 'ok' if CONF.staged_recovery.enabled else 'disabled'
        return {'health': {
            'generated_at': _utcnow(),
            'status': 'ok',
            'checks': [
                {
                    'name': 'masakari_api',
                    'status': 'ok',
                },
                {
                    'name': 'staged_recovery_config',
                    'status': staged_status,
                },
            ],
        }}


class AdminOverview(extensions.V1APIExtensionBase):
    """Admin overview and health summary."""

    name = "AdminOverview"
    alias = ALIAS
    version = 1

    def get_resources(self):
        return [
            extensions.ResourceExtension(ALIAS,
                                         AdminOverviewController(),
                                         member_name='admin_overview')
        ]

    def get_controller_extensions(self):
        return []
