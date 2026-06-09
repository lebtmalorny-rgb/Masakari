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
from masakari.engine.drivers.taskflow import staged_state_etcd as store_mod
from masakari import test
from masakari.tests import uuidsentinel


CONF = conf.CONF


class FakeLease(object):
    def __init__(self, client, lease_id, ttl):
        self.client = client
        self.id = lease_id
        self.ttl_value = ttl
        self.revoked = False
        self.refreshed = False

    def refresh(self):
        self.refreshed = True
        return self.ttl_value

    def revoke(self):
        self.revoked = True
        self.client.revoked_lease_ids.append(self.id)
        return True


class FakeEtcdClient(object):
    def __init__(self):
        self.data = {}
        self.lease_by_key = {}
        self.leases = {}
        self.next_lease_id = 100
        self.revoked_lease_ids = []

    def status(self):
        return {'version': 'fake'}

    def get(self, key, **kwargs):
        value = self.data.get(key)
        if value is None:
            return []
        if kwargs.get('metadata'):
            return [(value, mock.Mock(key=key.encode('utf-8')))]
        return [value]

    def get_prefix(self, key_prefix, **kwargs):
        result = []
        for key in sorted(self.data):
            if key.startswith(key_prefix):
                result.append(
                    (self.data[key], mock.Mock(key=key.encode('utf-8'))))
        return result

    def create(self, key, value, lease=None):
        if key in self.data:
            return False
        self.data[key] = value
        if lease is not None:
            self.lease_by_key[key] = lease.id
        return True

    def replace(self, key, initial_value, new_value):
        if self.data.get(key) != initial_value:
            return False
        self.data[key] = new_value
        return True

    def put(self, key, value, lease=None):
        self.data[key] = value
        if lease is not None:
            self.lease_by_key[key] = lease.id
        return True

    def delete(self, key, **kwargs):
        existed = key in self.data
        self.data.pop(key, None)
        self.lease_by_key.pop(key, None)
        return existed

    def lease(self, ttl=30, lease_id=None):
        if lease_id is None:
            lease_id = self.next_lease_id
            self.next_lease_id += 1
        lease = FakeLease(self, lease_id, ttl)
        self.leases[lease_id] = lease
        return lease


class EtcdStagedRecoveryStoreTestCase(test.NoDBTestCase):

    def setUp(self):
        super(EtcdStagedRecoveryStoreTestCase, self).setUp()
        self.client = FakeEtcdClient()
        self.store = store_mod.EtcdStagedRecoveryStore(
            CONF, owner='engine-1', client=self.client)

    def test_encode_key_part_escapes_path_separators(self):
        encoded = store_mod.encode_key_part('compute/1 with space')
        self.assertEqual('compute%2F1%20with%20space', encoded)
        self.assertEqual('compute/1 with space',
                         store_mod.decode_key_part(encoded))

    def test_create_or_get_instance_state_does_not_overwrite_existing(self):
        state = self.store.create_or_get_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'instance_name': 'vm1',
             'source_host': 'compute-1',
             'original_vm_state': 'active',
             'step': store_mod.STEP_DISCOVERED})

        existing = self.store.create_or_get_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'instance_name': 'vm1',
             'source_host': 'compute-1',
             'original_vm_state': 'stopped',
             'step': store_mod.STEP_FAILED})

        self.assertEqual(state, existing)
        self.assertEqual('active', existing['original_vm_state'])
        self.assertEqual(store_mod.STEP_DISCOVERED, existing['step'])

    def test_update_instance_state_uses_compare_and_replace(self):
        self.store.create_or_get_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'step': store_mod.STEP_DISCOVERED})

        updated = self.store.update_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'step': store_mod.STEP_EVACUATING, 'dest_host': 'compute-2'})

        self.assertEqual(store_mod.STEP_EVACUATING, updated['step'])
        self.assertEqual('compute-2', updated['dest_host'])

    def test_transition_instance_state_checks_expected_step(self):
        self.store.create_or_get_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'step': store_mod.STEP_DISCOVERED})

        skipped = self.store.transition_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            [store_mod.STEP_STARTING], store_mod.STEP_ACTIVE)
        transitioned = self.store.transition_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            [store_mod.STEP_DISCOVERED], store_mod.STEP_EVACUATING)

        self.assertIsNone(skipped)
        self.assertEqual(store_mod.STEP_EVACUATING, transitioned['step'])

    def test_list_stale_instance_states_filters_by_step_and_updated_at(self):
        with mock.patch.object(store_mod, 'utcnow_iso',
                               return_value='2026-06-09T12:00:00Z'):
            self.store.create_or_get_instance_state(
                uuidsentinel.notification, uuidsentinel.instance,
                {'step': store_mod.STEP_STARTING})
        self.store.update_instance_state(
            uuidsentinel.notification, uuidsentinel.instance,
            {'updated_at': '2026-06-09T11:50:00Z'})
        self.store.create_or_get_instance_state(
            uuidsentinel.notification, uuidsentinel.instance_2,
            {'step': store_mod.STEP_ACTIVE})

        stale = self.store.list_stale_instance_states(
            older_than_seconds=300, steps=[store_mod.STEP_STARTING],
            now='2026-06-09T12:00:00Z')

        self.assertEqual([uuidsentinel.instance],
                         [state['instance_uuid'] for state in stale])

    def test_create_start_lease_attaches_ttl_lease(self):
        lease = self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance, 'compute/2',
            ttl=60)

        self.assertEqual('compute/2', lease['dest_host'])
        self.assertEqual(100, lease['etcd_lease_id'])
        self.assertEqual(100, self.client.lease_by_key[
            self.store.start_lease_key('compute/2', uuidsentinel.instance)])

    def test_create_start_lease_revokes_unused_lease_on_conflict(self):
        self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance, 'compute-2',
            ttl=60)

        lease = self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance, 'compute-2',
            ttl=60)

        self.assertIsNone(lease)
        self.assertEqual([101], self.client.revoked_lease_ids)

    def test_release_start_lease_deletes_key_and_revokes_lease(self):
        self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance, 'compute-2',
            ttl=60)

        self.store.release_start_lease('compute-2', uuidsentinel.instance)

        self.assertEqual([], self.store.list_start_leases('compute-2'))
        self.assertEqual([100], self.client.revoked_lease_ids)
