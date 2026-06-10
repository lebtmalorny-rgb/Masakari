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

from masakari.api.openstack.ha import recovery_workflows
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class RecoveryWorkflowsTestCase(test.TestCase):

    def setUp(self):
        super(RecoveryWorkflowsTestCase, self).setUp()
        self.controller = recovery_workflows.RecoveryWorkflowsController()
        self.req = fakes.HTTPRequest.blank(
            '/v1/admin-recovery-workflows/schema',
            use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_show_schema_lists_staged_host_failure_tasks(self):
        result = self.controller.show(self.req, 'schema')

        entry_points = [task['entry_point'] for task in
                        result['schema']['available_tasks']]
        self.assertIn('masakari.engine.drivers.taskflow.staged_host_failure:'
                      'ReconcileStagedRecoveryTask', entry_points)
        self.assertIn('masakari.engine.drivers.taskflow.staged_host_failure:'
                      'BatchedStartInstancesTask', entry_points)

    def test_show_effective_returns_staged_template(self):
        result = self.controller.show(self.req, 'effective')

        self.assertIn('staged_recovery_etcd',
                      [template['name'] for template in
                       result['workflow']['templates']])

    def test_show_rejects_unknown_resource(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'unknown')

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_schema_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-recovery-workflows/schema',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('available_tasks', body['schema'])

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_effective_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-recovery-workflows/effective',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('templates', body['workflow'])
