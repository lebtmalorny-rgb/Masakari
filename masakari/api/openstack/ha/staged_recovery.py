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

"""Staged recovery API extension."""

from http import HTTPStatus

from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack.ha.schemas import staged_recovery as schema
from masakari.api.openstack import wsgi
from masakari.api import validation
import masakari.conf
from masakari.engine.drivers.taskflow import staged_state_etcd as staged_state
from masakari import exception
from masakari.i18n import _
from masakari.policies import staged_recovery as staged_recovery_policies


CONF = masakari.conf.CONF
ALIAS = 'staged-recovery'

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

INSTANCE_FILTERS = (
    'notification_uuid',
    'instance_uuid',
    'source_host',
    'dest_host',
    'step',
)
LEASE_FILTERS = (
    'notification_uuid',
    'instance_uuid',
    'dest_host',
)


class StagedRecoveryController(wsgi.Controller):
    """Controller for staged recovery runtime state."""

    def _store(self):
        return staged_state.EtcdStagedRecoveryStore(CONF, owner='api')

    def _start_limit_view(self, value, source):
        return {'start_limit': {
            'max_parallel_starts_per_host': value,
            'source': source,
        }}

    def _parse_limit(self, req):
        raw_limit = req.params.get('limit')
        if raw_limit is None:
            return DEFAULT_LIST_LIMIT
        try:
            limit = int(raw_limit)
        except ValueError:
            msg = _('Invalid limit value')
            raise exc.HTTPBadRequest(explanation=msg)
        if limit < 1 or limit > MAX_LIST_LIMIT:
            msg = _('Invalid limit value')
            raise exc.HTTPBadRequest(explanation=msg)
        return limit

    def _filters(self, req, allowed):
        return {name: req.params[name] for name in allowed
                if name in req.params and req.params[name]}

    @extensions.expected_errors((HTTPStatus.BAD_REQUEST,
                                 HTTPStatus.FORBIDDEN,
                                 HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']

        if id == 'start-limit':
            context.can(staged_recovery_policies.STAGED_RECOVERY %
                        'start_limit:show')
            store = self._store()
            limit, source = store.get_max_parallel_starts_per_host_with_source(
                CONF.staged_recovery.max_parallel_starts_per_host)
            return self._start_limit_view(limit, source)

        if id == 'instances':
            context.can(staged_recovery_policies.STAGED_RECOVERY %
                        'instances:index')
            limit = self._parse_limit(req)
            store = self._store()
            return {'instances': store.list_all_instance_states(
                filters=self._filters(req, INSTANCE_FILTERS),
                limit=limit)}

        if id == 'leases':
            context.can(staged_recovery_policies.STAGED_RECOVERY %
                        'leases:index')
            limit = self._parse_limit(req)
            store = self._store()
            return {'leases': store.list_all_start_leases(
                filters=self._filters(req, LEASE_FILTERS),
                limit=limit)}

        raise exc.HTTPNotFound()

    @extensions.expected_errors((HTTPStatus.BAD_REQUEST,
                                 HTTPStatus.FORBIDDEN,
                                 HTTPStatus.NOT_FOUND))
    @validation.schema(schema.update_start_limit)
    def update(self, req, id, body):
        if id != 'start-limit':
            raise exc.HTTPNotFound()

        context = req.environ['masakari.context']
        context.can(staged_recovery_policies.STAGED_RECOVERY %
                    'start_limit:update')
        value = body['start_limit']['max_parallel_starts_per_host']
        try:
            value = self._store().set_max_parallel_starts_per_host(value)
        except exception.InvalidInput as err:
            raise exc.HTTPBadRequest(explanation=err.format_message())

        return self._start_limit_view(value, 'runtime')

    @wsgi.response(HTTPStatus.NO_CONTENT)
    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def delete(self, req, id):
        if id != 'start-limit':
            raise exc.HTTPNotFound()

        context = req.environ['masakari.context']
        context.can(staged_recovery_policies.STAGED_RECOVERY %
                    'start_limit:delete')
        self._store().clear_max_parallel_starts_per_host()


class StagedRecovery(extensions.V1APIExtensionBase):
    """Staged recovery runtime state and controls."""

    name = "StagedRecovery"
    alias = ALIAS
    version = 1

    def get_resources(self):
        resources = [
            extensions.ResourceExtension(ALIAS,
                                         StagedRecoveryController(),
                                         member_name='staged_recovery')
        ]
        return resources

    def get_controller_extensions(self):
        return []
