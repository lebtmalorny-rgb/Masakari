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

"""Admin audit API extension."""

from http import HTTPStatus

from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
from masakari.policies import audit as audit_policies


ALIAS = 'admin-audit'


class AuditController(wsgi.Controller):
    """Read-only audit API placeholder."""

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        if id != 'events':
            raise exc.HTTPNotFound()

        context = req.environ['masakari.context']
        context.can(audit_policies.AUDIT % 'index')
        return {'events': []}


class Audit(extensions.V1APIExtensionBase):
    """Admin audit events."""

    name = "Audit"
    alias = ALIAS
    version = 1

    def get_resources(self):
        return [
            extensions.ResourceExtension(ALIAS,
                                         AuditController(),
                                         member_name='admin_audit')
        ]

    def get_controller_extensions(self):
        return []
