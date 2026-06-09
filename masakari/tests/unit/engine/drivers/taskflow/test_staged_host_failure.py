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
from masakari.engine.drivers.taskflow import staged_host_failure
from masakari.engine.drivers.taskflow import staged_state_etcd as store_mod
from masakari import exception
from masakari import objects
from masakari.objects import fields
from masakari.objects import vmove as vmove_obj
from masakari import test
from masakari.tests import uuidsentinel
from masakari.tests.unit.engine.drivers.taskflow import (
    test_staged_state_etcd)


CONF = conf.CONF


class FakeServer(object):
    def __init__(self, uuid, host, vm_state='active', task_state=None,
                 power_state=1, locked=False):
        self.id = uuid
        self.uuid = uuid
        self.name = 'fake-instance'
        self.locked = locked
        setattr(self, 'OS-EXT-SRV-ATTR:hypervisor_hostname', host)
        setattr(self, 'OS-EXT-STS:vm_state', vm_state)
        setattr(self, 'OS-EXT-STS:task_state', task_state)
        setattr(self, 'OS-EXT-STS:power_state', power_state)


class FakeNovaApi(object):
    def __init__(self):
        self.servers = {}
        self.evacuate_calls = []
        self.start_calls = []
        self.lock_calls = []
        self.unlock_calls = []

    def add_server(self, server):
        self.servers[server.id] = server
        return server

    def get_server(self, _context, uuid):
        return self.servers[uuid]

    def evacuate_instance_stopped(self, _context, uuid, target=None):
        self.evacuate_calls.append((uuid, target))
        server = self.servers[uuid]
        setattr(server, 'OS-EXT-SRV-ATTR:hypervisor_hostname',
                target or 'compute-2')
        setattr(server, 'OS-EXT-STS:vm_state', 'stopped')

    def start_server(self, _context, uuid):
        self.start_calls.append(uuid)
        setattr(self.servers[uuid], 'OS-EXT-STS:vm_state', 'active')

    def lock_server(self, _context, uuid):
        self.lock_calls.append(uuid)
        self.servers[uuid].locked = True

    def unlock_server(self, _context, uuid):
        self.unlock_calls.append(uuid)
        self.servers[uuid].locked = False

    def reset_instance_state(self, _context, uuid, status='error'):
        setattr(self.servers[uuid], 'OS-EXT-STS:vm_state', status)

    def enable_disable_service(self, *args, **kwargs):
        pass

    def get_aggregate_list(self, _context):
        return []


class FakeLimiter(object):
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, notification_uuid, instance_uuid, dest_host):
        slot = mock.Mock(notification_uuid=notification_uuid,
                         instance_uuid=instance_uuid,
                         dest_host=dest_host,
                         lease_id=1)
        self.acquired.append(slot)
        return slot

    def release(self, slot):
        self.released.append(slot)


class StagedHostFailureTestCase(test.TestCase):

    def setUp(self):
        super(StagedHostFailureTestCase, self).setUp()
        self.ctxt = context.get_admin_context()
        self.notification_uuid = uuidsentinel.notification
        self.source_host = 'compute-1'
        self.novaclient = FakeNovaApi()
        self.store = store_mod.EtcdStagedRecoveryStore(
            CONF, owner='engine-1',
            client=test_staged_state_etcd.FakeEtcdClient())
        self.override_config('enabled', True, group='staged_recovery')
        self.override_config('start_timeout', 60, group='staged_recovery')
        self.override_config('batch_delay', 0, group='staged_recovery')
        self.override_config('slot_lease_ttl', 60, group='staged_recovery')
        self.override_config('verify_interval', 0)
        self.override_config('wait_period_after_evacuation', 1)

    def _create_vmove(self, instance_uuid, status=fields.VMoveStatus.PENDING):
        vmove = vmove_obj.VMove(context=self.ctxt)
        vmove.instance_uuid = instance_uuid
        vmove.instance_name = 'fake-instance'
        vmove.notification_uuid = self.notification_uuid
        vmove.source_host = self.source_host
        vmove.status = status
        vmove.type = fields.VMoveType.EVACUATION
        vmove.create()
        return vmove

    def test_reconcile_creates_state_and_marks_evacuated_stopped(self):
        self._create_vmove(uuidsentinel.instance)
        self.novaclient.add_server(FakeServer(
            uuidsentinel.instance, 'compute-2', vm_state='stopped'))
        self.store.create_or_get_instance_state(
            self.notification_uuid, uuidsentinel.instance,
            {'step': store_mod.STEP_DISCOVERED,
             'original_vm_state': 'active',
             'source_host': self.source_host})

        task = staged_host_failure.ReconcileStagedRecoveryTask(
            self.ctxt, self.novaclient, state_store=self.store)
        task.execute(self.source_host, self.notification_uuid)

        state = self.store.get_instance_state(
            self.notification_uuid, uuidsentinel.instance)
        self.assertEqual(store_mod.STEP_EVACUATED_STOPPED, state['step'])
        self.assertEqual('compute-2', state['dest_host'])
        self.assertEqual('active', state['original_vm_state'])

    def test_evacuate_to_stopped_uses_staged_nova_method(self):
        self._create_vmove(uuidsentinel.instance)
        self.novaclient.add_server(FakeServer(
            uuidsentinel.instance, self.source_host, vm_state='active'))
        self.store.create_or_get_instance_state(
            self.notification_uuid, uuidsentinel.instance,
            {'step': store_mod.STEP_DISCOVERED,
             'original_vm_state': 'active',
             'source_host': self.source_host})

        task = staged_host_failure.EvacuateToStoppedTask(
            self.ctxt, self.novaclient, state_store=self.store,
            update_host_method=mock.Mock())
        task.execute(self.source_host, self.notification_uuid)

        self.assertEqual([(uuidsentinel.instance, None)],
                         self.novaclient.evacuate_calls)
        state = self.store.get_instance_state(
            self.notification_uuid, uuidsentinel.instance)
        self.assertEqual(store_mod.STEP_EVACUATED_STOPPED, state['step'])
        self.assertEqual('compute-2', state['dest_host'])
        vmove = objects.VMoveList.get_all_vmoves(
            self.ctxt, self.notification_uuid)[0]
        self.assertEqual(fields.VMoveStatus.SUCCEEDED, vmove.status)
        self.assertEqual('compute-2', vmove.dest_host)

    def test_batched_start_starts_only_originally_active_instances(self):
        self.novaclient.add_server(FakeServer(
            uuidsentinel.instance, 'compute-2', vm_state='stopped'))
        self.novaclient.add_server(FakeServer(
            uuidsentinel.instance_2, 'compute-2', vm_state='stopped'))
        self.store.create_or_get_instance_state(
            self.notification_uuid, uuidsentinel.instance,
            {'step': store_mod.STEP_EVACUATED_STOPPED,
             'original_vm_state': 'active',
             'dest_host': 'compute-2'})
        self.store.create_or_get_instance_state(
            self.notification_uuid, uuidsentinel.instance_2,
            {'step': store_mod.STEP_EVACUATED_STOPPED,
             'original_vm_state': 'stopped',
             'dest_host': 'compute-2'})
        limiter = FakeLimiter()

        task = staged_host_failure.BatchedStartInstancesTask(
            self.ctxt, self.novaclient, state_store=self.store,
            limiter=limiter)
        task.execute(self.notification_uuid)

        self.assertEqual([uuidsentinel.instance],
                         self.novaclient.start_calls)
        self.assertEqual(1, len(limiter.acquired))
        self.assertEqual(limiter.acquired, limiter.released)
        active_state = self.store.get_instance_state(
            self.notification_uuid, uuidsentinel.instance)
        ignored_state = self.store.get_instance_state(
            self.notification_uuid, uuidsentinel.instance_2)
        self.assertEqual(store_mod.STEP_ACTIVE, active_state['step'])
        self.assertEqual(store_mod.STEP_IGNORED, ignored_state['step'])

    def test_batched_start_raises_when_originally_active_fails(self):
        self.novaclient.add_server(FakeServer(
            uuidsentinel.instance, 'compute-2', vm_state='error'))
        self.store.create_or_get_instance_state(
            self.notification_uuid, uuidsentinel.instance,
            {'step': store_mod.STEP_EVACUATED_STOPPED,
             'original_vm_state': 'active',
             'dest_host': 'compute-2'})

        with mock.patch.object(self.novaclient, 'start_server',
                               side_effect=exception.MasakariException):
            task = staged_host_failure.BatchedStartInstancesTask(
                self.ctxt, self.novaclient, state_store=self.store,
                limiter=FakeLimiter())
            self.assertRaises(exception.StagedStartFailureException,
                              task.execute, self.notification_uuid)
