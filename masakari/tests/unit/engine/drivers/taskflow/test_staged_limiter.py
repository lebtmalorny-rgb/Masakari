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
from masakari.engine.drivers.taskflow import staged_limiter
from masakari.engine.drivers.taskflow import staged_state_etcd as store_mod
from masakari import exception
from masakari import test
from masakari.tests import uuidsentinel
from masakari.tests.unit.engine.drivers.taskflow import (
    test_staged_state_etcd)


CONF = conf.CONF
_UNSET = object()


class FakeLock(object):
    def __call__(self, blocking=True):
        self.blocking = blocking
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class FakeCoordinator(object):
    def __init__(self, lock):
        self.lock = lock
        self.lock_names = []

    def get_lock(self, name):
        self.lock_names.append(name)
        return self.lock


class EtcdStartLimiterTestCase(test.NoDBTestCase):

    def setUp(self):
        super(EtcdStartLimiterTestCase, self).setUp()
        self.ctxt = context.get_admin_context()
        self.client = test_staged_state_etcd.FakeEtcdClient()
        self.store = store_mod.EtcdStagedRecoveryStore(
            CONF, owner='engine-1', client=self.client)
        self.override_config('max_parallel_starts_per_host', 1,
                             group='staged_recovery')
        self.override_config('start_timeout', 60, group='staged_recovery')
        self.override_config('batch_delay', 0, group='staged_recovery')
        self.override_config('slot_lease_ttl', 60, group='staged_recovery')
        self.override_config('slot_retry_interval', 1,
                             group='staged_recovery')

    def _limiter(self, lock=_UNSET):
        if lock is _UNSET:
            lock = FakeLock()
        return staged_limiter.EtcdStartLimiter(
            self.ctxt, self.store, owner='engine-1',
            coordinator=FakeCoordinator(lock))

    def test_acquire_creates_lease_when_host_has_capacity(self):
        limiter = self._limiter()

        slot = limiter.acquire(
            uuidsentinel.notification, uuidsentinel.instance, 'compute-2')

        self.assertEqual('compute-2', slot.dest_host)
        self.assertEqual(uuidsentinel.instance, slot.instance_uuid)
        self.assertEqual(1, len(self.store.list_start_leases('compute-2')))

    def test_acquire_retries_until_existing_lease_is_released(self):
        self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance_2, 'compute-2',
            ttl=60)
        limiter = self._limiter()

        def fake_sleep(_seconds):
            self.store.release_start_lease(
                'compute-2', uuidsentinel.instance_2)

        with mock.patch.object(staged_limiter.eventlet, 'sleep',
                               side_effect=fake_sleep) as mock_sleep:
            slot = limiter.acquire(
                uuidsentinel.notification, uuidsentinel.instance,
                'compute-2')

        self.assertEqual(1, mock_sleep.call_count)
        self.assertEqual(uuidsentinel.instance, slot.instance_uuid)

    def test_acquire_fails_closed_when_coordination_lock_unavailable(self):
        limiter = self._limiter(lock=None)

        self.assertRaises(
            exception.MasakariException,
            limiter.acquire, uuidsentinel.notification, uuidsentinel.instance,
            'compute-2')

    def test_acquire_reads_runtime_limit_after_limiter_creation(self):
        limiter = self._limiter()
        self.store.create_start_lease(
            uuidsentinel.notification, uuidsentinel.instance_2, 'compute-2',
            ttl=60)

        self.store.set_max_parallel_starts_per_host(2)

        with mock.patch.object(staged_limiter.eventlet, 'sleep') as sleep:
            slot = limiter.acquire(
                uuidsentinel.notification, uuidsentinel.instance,
                'compute-2')

        self.assertEqual(uuidsentinel.instance, slot.instance_uuid)
        self.assertFalse(sleep.called)

    def test_init_rejects_ttl_shorter_than_start_window(self):
        self.override_config('start_timeout', 90, group='staged_recovery')
        self.override_config('batch_delay', 10, group='staged_recovery')
        self.override_config('slot_lease_ttl', 60, group='staged_recovery')

        self.assertRaises(exception.InvalidInput, self._limiter)

    def test_release_removes_start_lease(self):
        limiter = self._limiter()
        slot = limiter.acquire(
            uuidsentinel.notification, uuidsentinel.instance, 'compute-2')

        limiter.release(slot)

        self.assertEqual([], self.store.list_start_leases('compute-2'))
