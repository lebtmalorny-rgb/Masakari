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

from masakari.api.openstack.ha import audit
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class AuditTestCase(test.TestCase):

    def setUp(self):
        super(AuditTestCase, self).setUp()
        self.controller = audit.AuditController()
        self.req = fakes.HTTPRequest.blank('/v1/admin-audit/events',
                                           use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_show_events_returns_empty_event_list_for_mvp(self):
        result = self.controller.show(self.req, 'events')

        self.assertEqual([], result['events'])

    def test_show_rejects_unknown_resource(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'unknown')

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_events_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-audit/events',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertEqual([], body['events'])
