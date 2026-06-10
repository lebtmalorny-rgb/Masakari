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

from masakari import conf
from masakari import context
from masakari.engine.drivers.taskflow import fenced_host_failure
from masakari.engine.drivers.taskflow import fencing_state_etcd as store_mod
from masakari import exception
from masakari import test
from masakari.tests import uuidsentinel
from masakari.tests.unit.engine.drivers.taskflow import (
    test_staged_state_etcd)


CONF = conf.CONF


class FakeNovaApi(object):
    def __init__(self):
        self.disabled = []

    def enable_disable_service(self, _context, host_name, enable=False,
                               reason=None):
        self.disabled.append((host_name, enable, reason))


class FakeRedfishClient(object):
    fenced_hosts = []

    def __init__(self, host_config, conf):
        self.host_config = host_config

    def fence_off(self):
        self.fenced_hosts.append(self.host_config.hostname)
        return {
            'method': 'redfish',
            'systems_uri': self.host_config.systems_uri,
            'reset_target': self.host_config.systems_uri + (
                '/Actions/ComputerSystem.Reset'),
            'requested_reset_type': 'ForceOff',
            'verified_power_state': 'Off',
            'power_state_reads': 2,
        }

    def close(self):
        pass


class FakeHostConfig(object):
    def __init__(self, hostname):
        self.hostname = hostname
        self.systems_uri = '/redfish/v1/Systems/%s' % hostname


class FencedHostFailureTaskTestCase(test.TestCase):

    def setUp(self):
        super(FencedHostFailureTaskTestCase, self).setUp()
        self.ctxt = context.get_admin_context()
        self.novaclient = FakeNovaApi()
        self.store = store_mod.EtcdFencingStore(
            CONF, owner='engine-1',
            client=test_staged_state_etcd.FakeEtcdClient())
        self.override_config('enabled', True, group='redfish_fencing')
        self.override_config('multi_host_batch_window', 0,
                             group='redfish_fencing')
        self.override_config('max_auto_fence_hosts_per_segment', 2,
                             group='redfish_fencing')
        self.override_config('min_surviving_compute_hosts', 1,
                             group='redfish_fencing')
        FakeRedfishClient.fenced_hosts = []

    def _register(self, hostname):
        self.store.register_host_failure(
            uuidsentinel.segment, 'event-1', hostname,
            uuidsentinel.notification)

    def _host_config_loader(self, hostname, _conf):
        return FakeHostConfig(hostname)

    def test_disable_failure_set_disables_every_host(self):
        task = fenced_host_failure.DisableFailureSetTask(
            self.ctxt, self.novaclient)

        task.execute(['compute-1', 'compute-2'])

        self.assertEqual([
            ('compute-1', False, CONF.host_failure.service_disable_reason),
            ('compute-2', False, CONF.host_failure.service_disable_reason),
        ], self.novaclient.disabled)

    def test_redfish_fence_failure_set_marks_every_host_fenced(self):
        self._register('compute-1')
        self._register('compute-2')
        task = fenced_host_failure.RedfishFenceFailureSetTask(
            self.ctxt, self.novaclient, fencing_store=self.store,
            client_cls=FakeRedfishClient,
            host_config_loader=self._host_config_loader)

        result = task.execute(['compute-1', 'compute-2'], 'event-1',
                              uuidsentinel.segment,
                              uuidsentinel.notification)

        self.assertEqual(['compute-1', 'compute-2'], result)
        self.assertEqual(['compute-1', 'compute-2'],
                         FakeRedfishClient.fenced_hosts)
        self.store.assert_hosts_fenced(['compute-1', 'compute-2'])

    def test_assert_failure_set_fenced_blocks_missing_state(self):
        task = fenced_host_failure.AssertFailureSetFencedTask(
            self.ctxt, self.novaclient, fencing_store=self.store)

        self.assertRaises(exception.HostRecoveryFailureException,
                          task.execute, ['compute-1'])

    def test_collect_failure_set_fails_when_threshold_exceeded(self):
        self._register('compute-1')
        self._register('compute-2')
        self.override_config('max_auto_fence_hosts_per_segment', 1,
                             group='redfish_fencing')
        task = fenced_host_failure.CollectFailureSetTask(
            self.ctxt, self.novaclient, fencing_store=self.store,
            host_config_loader=self._host_config_loader,
            host_list_method=mock.Mock(return_value=[
                mock.Mock(name='compute-1'),
                mock.Mock(name='compute-2'),
                mock.Mock(name='compute-3'),
            ]))

        self.assertRaises(exception.HostRecoveryFailureException,
                          task.execute, uuidsentinel.segment, 'event-1',
                          'compute-1')
