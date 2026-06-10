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
from masakari.engine.drivers.taskflow import fencing_state_etcd as store_mod
from masakari import exception
from masakari import test
from masakari.tests import uuidsentinel
from masakari.tests.unit.engine.drivers.taskflow import (
    test_staged_state_etcd)


CONF = conf.CONF


class EtcdFencingStoreTestCase(test.NoDBTestCase):

    def setUp(self):
        super(EtcdFencingStoreTestCase, self).setUp()
        self.client = test_staged_state_etcd.FakeEtcdClient()
        self.store = store_mod.EtcdFencingStore(
            CONF, owner='engine-1', client=self.client)

    def test_register_host_failure_creates_event_and_required_state(self):
        with mock.patch.object(store_mod, 'utcnow_iso',
                               return_value='2026-06-10T10:00:00Z'):
            self.store.register_host_failure(
                uuidsentinel.segment, 'event-1', 'compute-1',
                uuidsentinel.notification)

        failures = self.store.list_recent_failures(
            uuidsentinel.segment, window_seconds=60,
            now='2026-06-10T10:00:30Z')
        state = self.store.get_fencing_state('compute-1')

        self.assertEqual(['compute-1'],
                         [failure['hostname'] for failure in failures])
        self.assertEqual(store_mod.FENCE_REQUIRED, state['state'])
        self.assertEqual(uuidsentinel.notification,
                         state['notification_uuid'])

    def test_transition_mark_fenced_and_assert_hosts_fenced(self):
        self.store.register_host_failure(
            uuidsentinel.segment, 'event-1', 'compute-1',
            uuidsentinel.notification)

        transitioned = self.store.transition_fencing_state(
            'compute-1', [store_mod.FENCE_REQUIRED], store_mod.FENCING)
        fenced = self.store.mark_fenced(
            'compute-1', 'event-1',
            {'verified_power_state': 'Off',
             'reset_target': '/redfish/v1/Systems/1/Actions/'
                             'ComputerSystem.Reset'})

        self.assertEqual(store_mod.FENCING, transitioned['state'])
        self.assertEqual(store_mod.FENCED, fenced['state'])
        self.assertEqual('Off', fenced['verified_power_state'])
        self.store.assert_hosts_fenced(['compute-1'])

    def test_assert_hosts_fenced_rejects_missing_or_failed_state(self):
        self.store.register_host_failure(
            uuidsentinel.segment, 'event-1', 'compute-1',
            uuidsentinel.notification)
        self.store.mark_fence_failed('compute-1', 'event-1', 'BMC timeout')

        self.assertRaises(exception.HostRecoveryFailureException,
                          self.store.assert_hosts_fenced, ['compute-1'])
        self.assertRaises(exception.HostRecoveryFailureException,
                          self.store.assert_hosts_fenced, ['compute-2'])

    def test_segment_and_host_locks_are_exclusive_until_released(self):
        segment_lock = self.store.acquire_segment_recovery_lock(
            uuidsentinel.segment, 'event-1', ttl=300)
        host_lock = self.store.acquire_host_fencing_lock(
            'compute-1', 'event-1', ttl=300)

        self.assertIsNone(self.store.acquire_segment_recovery_lock(
            uuidsentinel.segment, 'event-2', ttl=300))
        self.assertIsNone(self.store.acquire_host_fencing_lock(
            'compute-1', 'event-2', ttl=300))

        self.store.release_segment_recovery_lock(
            uuidsentinel.segment, segment_lock)
        self.store.release_host_fencing_lock('compute-1', host_lock)

        self.assertIsNotNone(self.store.acquire_segment_recovery_lock(
            uuidsentinel.segment, 'event-2', ttl=300))
        self.assertIsNotNone(self.store.acquire_host_fencing_lock(
            'compute-1', 'event-2', ttl=300))
