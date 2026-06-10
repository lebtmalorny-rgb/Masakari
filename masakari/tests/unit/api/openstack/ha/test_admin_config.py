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

from masakari.api.openstack.ha import admin_config
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class AdminConfigTestCase(test.TestCase):

    def setUp(self):
        super(AdminConfigTestCase, self).setUp()
        self.controller = admin_config.AdminConfigController()
        self.req = fakes.HTTPRequest.blank('/v1/admin-config/schema',
                                           use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_show_schema_returns_staged_recovery_options(self):
        result = self.controller.show(self.req, 'schema')

        groups = {group['name']: group for group in result['schema']['groups']}
        self.assertIn('staged_recovery', groups)
        option_names = [opt['name'] for opt in groups[
            'staged_recovery']['options']]
        self.assertIn('max_parallel_starts_per_host', option_names)
        self.assertIn('start_only_originally_active', option_names)

    def test_show_effective_masks_secret_like_values(self):
        self.override_config('max_parallel_starts_per_host', 5,
                             group='staged_recovery')
        self.override_config('etcd_key_file', '/etc/masakari/key.pem',
                             group='staged_recovery')

        result = self.controller.show(self.req, 'effective')

        staged = result['config']['staged_recovery']
        self.assertEqual(5, staged['max_parallel_starts_per_host'])
        self.assertEqual({'masked': True, 'configured': True},
                         staged['etcd_key_file'])

    def test_show_rejects_unknown_resource(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'unknown')

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_schema_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config/schema',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('staged_recovery',
                      [group['name'] for group in body['schema']['groups']])

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_effective_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config/effective',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('staged_recovery', body['config'])
