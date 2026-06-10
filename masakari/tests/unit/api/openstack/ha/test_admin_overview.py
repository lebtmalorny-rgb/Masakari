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

from masakari.api.openstack.ha import admin_overview
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class AdminOverviewTestCase(test.TestCase):

    def setUp(self):
        super(AdminOverviewTestCase, self).setUp()
        self.controller = admin_overview.AdminOverviewController()
        self.req = fakes.HTTPRequest.blank('/v1/admin-overview',
                                           use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_index_returns_api_and_staged_recovery_summary(self):
        self.override_config('enabled', True, group='staged_recovery')
        self.override_config('max_parallel_starts_per_host', 4,
                             group='staged_recovery')

        result = self.controller.index(self.req)

        self.assertEqual('ok', result['overview']['masakari_api']['status'])
        self.assertTrue(result['overview']['staged_recovery']['enabled'])
        self.assertEqual(4, result['overview']['staged_recovery'][
            'max_parallel_starts_per_host'])

    def test_show_health_returns_checks(self):
        result = self.controller.show(self.req, 'health')

        self.assertEqual('ok', result['health']['status'])
        check_names = [check['name'] for check in result['health']['checks']]
        self.assertIn('masakari_api', check_names)
        self.assertIn('staged_recovery_config', check_names)

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_index_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-overview',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertEqual('ok', body['overview']['masakari_api']['status'])

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_health_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-overview/health',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertEqual('ok', body['health']['status'])
