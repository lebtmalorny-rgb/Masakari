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

from http import HTTPStatus
from unittest import mock

from oslo_serialization import jsonutils
from webob import exc

from masakari.api.openstack.ha import staged_recovery
from masakari import exception
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class StagedRecoveryTestCase(test.TestCase):

    def setUp(self):
        super(StagedRecoveryTestCase, self).setUp()
        self.controller = staged_recovery.StagedRecoveryController()
        self.req = fakes.HTTPRequest.blank(
            '/v1/staged-recovery/start-limit',
            use_admin_context=True)
        self.override_config('max_parallel_starts_per_host', 2,
                             group='staged_recovery')

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_show_start_limit(self, mock_store_cls):
        store = mock_store_cls.return_value
        store.get_max_parallel_starts_per_host_with_source.return_value = (
            3, 'runtime')

        result = self.controller.show(self.req, 'start-limit')

        self.assertEqual(
            {'start_limit': {'max_parallel_starts_per_host': 3,
                             'source': 'runtime'}},
            result)

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_update_start_limit(self, mock_store_cls):
        store = mock_store_cls.return_value
        store.set_max_parallel_starts_per_host.return_value = 4

        result = self.controller.update(
            self.req, 'start-limit',
            body={'start_limit': {'max_parallel_starts_per_host': 4}})

        store.set_max_parallel_starts_per_host.assert_called_once_with(4)
        self.assertEqual(
            {'start_limit': {'max_parallel_starts_per_host': 4,
                             'source': 'runtime'}},
            result)

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_update_start_limit_invalid_value(self, mock_store_cls):
        store = mock_store_cls.return_value
        store.set_max_parallel_starts_per_host.side_effect = (
            exception.InvalidInput(reason='limit must be >= 1'))

        self.assertRaises(
            exc.HTTPBadRequest, self.controller.update, self.req,
            'start-limit', body={'start_limit': {
                'max_parallel_starts_per_host': 0}})

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_delete_start_limit(self, mock_store_cls):
        result = self.controller.delete(self.req, 'start-limit')

        store = mock_store_cls.return_value
        store.clear_max_parallel_starts_per_host.assert_called_once_with()
        self.assertIsNone(result)

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_show_instances(self, mock_store_cls):
        store = mock_store_cls.return_value
        store.list_all_instance_states.return_value = [{
            'notification_uuid': uuidsentinel.notification,
            'instance_uuid': uuidsentinel.instance,
            'source_host': 'compute-1',
            'dest_host': 'compute-2',
            'step': 'STARTING',
        }]
        req = fakes.HTTPRequest.blank(
            '/v1/staged-recovery/instances?notification_uuid=%s&'
            'step=STARTING&limit=5' % uuidsentinel.notification,
            use_admin_context=True)

        result = self.controller.show(req, 'instances')

        store.list_all_instance_states.assert_called_once_with(
            filters={'notification_uuid': uuidsentinel.notification,
                     'step': 'STARTING'},
            limit=5)
        self.assertEqual(1, len(result['instances']))

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    def test_show_leases(self, mock_store_cls):
        store = mock_store_cls.return_value
        store.list_all_start_leases.return_value = [{
            'notification_uuid': uuidsentinel.notification,
            'instance_uuid': uuidsentinel.instance,
            'dest_host': 'compute-2',
            'expires_at': '2026-06-10T12:00:00Z',
        }]
        req = fakes.HTTPRequest.blank(
            '/v1/staged-recovery/leases?dest_host=compute-2&limit=10',
            use_admin_context=True)

        result = self.controller.show(req, 'leases')

        store.list_all_start_leases.assert_called_once_with(
            filters={'dest_host': 'compute-2'}, limit=10)
        self.assertEqual(1, len(result['leases']))

    def test_show_rejects_invalid_resource(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'unknown')

    def test_show_rejects_invalid_limit(self):
        req = fakes.HTTPRequest.blank(
            '/v1/staged-recovery/instances?limit=0',
            use_admin_context=True)

        self.assertRaises(exc.HTTPBadRequest, self.controller.show, req,
                          'instances')

    @mock.patch('masakari.api.openstack.ha.staged_recovery.'
                'staged_state.EtcdStagedRecoveryStore')
    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_start_limit_route(self, mock_notification_api, mock_store_cls):
        store = mock_store_cls.return_value
        store.get_max_parallel_starts_per_host_with_source.return_value = (
            3, 'runtime')
        req = fakes.HTTPRequest.blank('/v1/staged-recovery/start-limit',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertEqual(3, body['start_limit'][
            'max_parallel_starts_per_host'])
