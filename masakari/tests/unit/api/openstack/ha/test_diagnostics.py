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

from masakari.api.openstack.ha import diagnostics
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class DiagnosticsTestCase(test.TestCase):

    def setUp(self):
        super(DiagnosticsTestCase, self).setUp()
        self.controller = diagnostics.DiagnosticsController()
        self.req = fakes.HTTPRequest.blank('/v1/admin-diagnostics/checks',
                                           use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_show_checks_returns_available_checks(self):
        result = self.controller.show(self.req, 'checks')

        names = [check['name'] for check in result['checks']]
        self.assertIn('masakari_api', names)
        self.assertIn('staged_recovery_config', names)

    def test_run_returns_completed_job(self):
        result = self.controller.run(self.req, body={
            'diagnostics': {'checks': ['masakari_api']}})

        self.assertEqual('completed', result['job']['status'])
        self.assertEqual(['masakari_api'], result['job']['requested_checks'])

    def test_show_job_returns_not_found_for_unknown_job(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'jobs/missing')

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_checks_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-diagnostics/checks',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('checks', body)

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_run_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-diagnostics/run',
                                      use_admin_context=True)
        req.method = 'POST'
        req.headers['Content-Type'] = 'application/json'
        req.body = jsonutils.dump_as_bytes({
            'diagnostics': {'checks': ['masakari_api']}})

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.ACCEPTED, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertEqual('completed', body['job']['status'])
