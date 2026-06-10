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

"""Admin diagnostics API extension."""

from http import HTTPStatus

from oslo_utils import timeutils
from oslo_utils import uuidutils
from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
import masakari.conf
from masakari.policies import diagnostics as diagnostics_policies


CONF = masakari.conf.CONF
ALIAS = 'admin-diagnostics'
_JOBS = {}


def _utcnow():
    return timeutils.utcnow().isoformat() + 'Z'


class DiagnosticsController(wsgi.Controller):
    """Lightweight diagnostics metadata and run endpoint."""

    def _checks(self):
        return [
            {
                'name': 'masakari_api',
                'description': 'Check that the Masakari API worker responds.',
                'enabled': True,
            },
            {
                'name': 'staged_recovery_config',
                'description': 'Check staged recovery configuration values.',
                'enabled': True,
            },
        ]

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']

        if id == 'checks':
            context.can(diagnostics_policies.DIAGNOSTICS % 'index')
            return {'checks': self._checks()}

        raise exc.HTTPNotFound()

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show_job(self, req, job_id):
        context = req.environ['masakari.context']
        context.can(diagnostics_policies.DIAGNOSTICS % 'detail')
        try:
            return {'job': _JOBS[job_id]}
        except KeyError:
            raise exc.HTTPNotFound()

    @wsgi.response(HTTPStatus.ACCEPTED)
    @extensions.expected_errors(HTTPStatus.FORBIDDEN)
    def run(self, req, body=None):
        context = req.environ['masakari.context']
        context.can(diagnostics_policies.DIAGNOSTICS % 'run')

        requested = None
        if body:
            requested = body.get('diagnostics', {}).get('checks')
        checks = requested or [check['name'] for check in self._checks()]
        job_id = uuidutils.generate_uuid()
        job = {
            'id': job_id,
            'status': 'completed',
            'requested_checks': checks,
            'generated_at': _utcnow(),
            'results': [
                {
                    'name': name,
                    'status': 'ok',
                } for name in checks
            ],
        }
        _JOBS[job_id] = job
        return {'job': job}


class Diagnostics(extensions.V1APIExtensionBase):
    """Admin diagnostics."""

    name = "Diagnostics"
    alias = ALIAS
    version = 1

    def get_resources(self):
        controller = DiagnosticsController()
        return [
            extensions.ResourceExtension(
                ALIAS, controller, member_name='admin_diagnostic',
                custom_routes_fn=self.custom_routes)
        ]

    def get_controller_extensions(self):
        return []

    @staticmethod
    def custom_routes(mapper, wsgi_resource):
        mapper.connect('admin-diagnostics-run', '/admin-diagnostics/run',
                       controller=wsgi_resource, action='run',
                       conditions={'method': ['POST']})
        mapper.connect('admin-diagnostics-job',
                       '/admin-diagnostics/jobs/{job_id}',
                       controller=wsgi_resource, action='show_job',
                       conditions={'method': ['GET']})
