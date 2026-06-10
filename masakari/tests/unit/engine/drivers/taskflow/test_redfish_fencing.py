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

import os
import tempfile
from unittest import mock

from requests import exceptions as requests_exc

from masakari import conf
from masakari.engine.drivers.taskflow import redfish_fencing
from masakari import exception
from masakari import test


CONF = conf.CONF


class FakeResponse(object):
    def __init__(self, payload=None, status_code=200, headers=None):
        self.payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_exc.HTTPError('HTTP %s' % self.status_code)


class FakeSession(object):
    def __init__(self, systems):
        self.systems = list(systems)
        self.posts = []
        self.deleted = []
        self.headers = {}

    def get(self, url, **kwargs):
        if url.endswith('/redfish/v1'):
            return FakeResponse({'RedfishVersion': '1.18.0'})
        if '/Systems/1' in url:
            return FakeResponse(self.systems.pop(0))
        return FakeResponse(status_code=404)

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json, kwargs))
        if url.endswith('/SessionService/Sessions'):
            return FakeResponse(
                headers={'X-Auth-Token': 'token-1',
                         'Location': '/redfish/v1/SessionService/Sessions/1'})
        return FakeResponse()

    def delete(self, url, **kwargs):
        self.deleted.append((url, kwargs))
        return FakeResponse()


class RedfishFencingTestCase(test.NoDBTestCase):

    def setUp(self):
        super(RedfishFencingTestCase, self).setUp()
        self.override_config('enabled', True, group='redfish_fencing')
        self.override_config('allow_insecure_inline_password', False,
                             group='redfish_fencing')

    def _write_hosts_file(self, content):
        tmp = tempfile.NamedTemporaryFile(delete=False, mode='w')
        self.addCleanup(os.unlink, tmp.name)
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_load_host_config_reads_password_file(self):
        password = tempfile.NamedTemporaryFile(delete=False, mode='w')
        self.addCleanup(os.unlink, password.name)
        password.write('secret\n')
        password.close()
        hosts_file = self._write_hosts_file("""
schema_version: 1
defaults:
  scheme: https
  port: 443
  base_uri: /redfish/v1
  auth_type: session
  tls_verify: true
hosts:
  compute-1:
    address: 10.0.0.1
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password_file: %s
    expected_serial_number: ABC123
""" % password.name)

        config = redfish_fencing.load_host_config('compute-1', CONF,
                                                  hosts_file=hosts_file)

        self.assertEqual('compute-1', config.hostname)
        self.assertEqual('10.0.0.1', config.address)
        self.assertEqual('secret', config.password)
        self.assertEqual('ABC123', config.expected_serial_number)

    def test_inline_password_rejected_unless_explicitly_allowed(self):
        hosts_file = self._write_hosts_file("""
schema_version: 1
hosts:
  compute-1:
    address: 10.0.0.1
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password: secret
""")

        self.assertRaises(exception.RedfishFencingException,
                          redfish_fencing.load_host_config, 'compute-1',
                          CONF, hosts_file=hosts_file)

    def test_fence_off_uses_session_auth_forceoff_and_stable_off_reads(self):
        self.override_config('stable_power_state_reads', 2,
                             group='redfish_fencing')
        hosts_file = self._write_hosts_file("""
schema_version: 1
hosts:
  compute-1:
    address: 10.0.0.1
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password: secret
""")
        self.override_config('allow_insecure_inline_password', True,
                             group='redfish_fencing')
        config = redfish_fencing.load_host_config('compute-1', CONF,
                                                  hosts_file=hosts_file)
        systems = [
            {'PowerState': 'On',
             'SerialNumber': 'ABC123',
             'Actions': {'#ComputerSystem.Reset': {
                 'target': '/redfish/v1/Systems/1/Actions/'
                           'ComputerSystem.Reset',
                 'ResetType@Redfish.AllowableValues': ['ForceOff']}}},
            {'PowerState': 'Off', 'SerialNumber': 'ABC123'},
            {'PowerState': 'Off', 'SerialNumber': 'ABC123'},
        ]
        session = FakeSession(systems)

        client = redfish_fencing.RedfishFencingClient(
            config, CONF, session=session)
        proof = client.fence_off()
        client.close()

        reset_posts = [post for post in session.posts
                       if post[1] == {'ResetType': 'ForceOff'}]
        self.assertEqual(1, len(reset_posts))
        self.assertEqual('Off', proof['verified_power_state'])
        self.assertEqual(2, proof['power_state_reads'])
        self.assertEqual('/redfish/v1/Systems/1/Actions/'
                         'ComputerSystem.Reset', proof['reset_target'])
        self.assertEqual(1, len(session.deleted))

    def test_validate_system_identity_rejects_serial_mismatch(self):
        self.override_config('allow_insecure_inline_password', True,
                             group='redfish_fencing')
        hosts_file = self._write_hosts_file("""
schema_version: 1
hosts:
  compute-1:
    address: 10.0.0.1
    systems_uri: /redfish/v1/Systems/1
    username: masakari-fencer
    password: secret
    expected_serial_number: ABC123
""")
        config = redfish_fencing.load_host_config('compute-1', CONF,
                                                  hosts_file=hosts_file)
        client = redfish_fencing.RedfishFencingClient(
            config, CONF, session=mock.Mock())

        self.assertRaises(exception.RedfishIdentityMismatch,
                          client.validate_system_identity,
                          {'SerialNumber': 'XYZ987'})
