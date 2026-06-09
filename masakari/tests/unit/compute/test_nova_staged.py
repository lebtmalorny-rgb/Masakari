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

from unittest import mock

from masakari.compute import nova
from masakari import context
from masakari import test
from masakari.tests import uuidsentinel


class NovaStagedRecoveryTestCase(test.TestCase):

    def setUp(self):
        super(NovaStagedRecoveryTestCase, self).setUp()
        self.api = nova.API()
        self.ctx = context.get_admin_context()

    @mock.patch('masakari.compute.nova.novaclient')
    def test_evacuate_instance_stopped_uses_configured_microversion(
            self, mock_novaclient):
        mock_servers = mock.MagicMock()
        mock_novaclient.return_value = mock.MagicMock(servers=mock_servers)
        self.override_config('nova_evacuate_microversion', '2.95',
                             group='staged_recovery')

        self.api.evacuate_instance_stopped(
            self.ctx, uuidsentinel.fake_server, target='compute-2')

        mock_novaclient.assert_called_once_with(
            self.ctx, api_version='2.95')
        mock_servers.evacuate.assert_called_once_with(
            uuidsentinel.fake_server, host='compute-2')

    @mock.patch('masakari.compute.nova.novaclient')
    def test_evacuate_instance_stopped_omits_target_when_none(
            self, mock_novaclient):
        mock_servers = mock.MagicMock()
        mock_novaclient.return_value = mock.MagicMock(servers=mock_servers)

        self.api.evacuate_instance_stopped(
            self.ctx, uuidsentinel.fake_server)

        mock_servers.evacuate.assert_called_once_with(
            uuidsentinel.fake_server)

    @mock.patch('masakari.compute.nova.novaclient')
    def test_get_server_with_microversion_uses_requested_version(
            self, mock_novaclient):
        mock_servers = mock.MagicMock()
        mock_novaclient.return_value = mock.MagicMock(servers=mock_servers)

        self.api.get_server_with_microversion(
            self.ctx, uuidsentinel.fake_server, '2.95')

        mock_novaclient.assert_called_once_with(
            self.ctx, api_version='2.95')
        mock_servers.get.assert_called_once_with(uuidsentinel.fake_server)
