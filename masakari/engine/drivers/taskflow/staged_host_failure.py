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

import socket

import eventlet
from eventlet import greenpool

from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import timeutils

import masakari.conf
from masakari.engine.drivers.taskflow import base
from masakari.engine.drivers.taskflow import staged_limiter
from masakari.engine.drivers.taskflow import staged_state_etcd as state
from masakari import exception
from masakari import objects
from masakari.objects import fields


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)
SHUTDOWN = 4


def _owner_id():
    return '%s-%s' % (socket.gethostname(), id(object()))


def _server_host(server):
    return getattr(server, 'OS-EXT-SRV-ATTR:hypervisor_hostname')


def _vm_state(server):
    return getattr(server, 'OS-EXT-STS:vm_state', None)


def _task_state(server):
    return getattr(server, 'OS-EXT-STS:task_state', None)


def _power_state(server):
    return getattr(server, 'OS-EXT-STS:power_state', None)


def _is_active(server):
    return _vm_state(server) == 'active'


def _is_stopped(server):
    return _vm_state(server) in ('stopped', 'shutoff')


def _update_vmove(vmove, status=None, start_time=None, end_time=None,
                  dest_host=None, message=None):
    if status:
        vmove.status = status
    if start_time:
        vmove.start_time = start_time
    if end_time:
        vmove.end_time = end_time
    if dest_host:
        vmove.dest_host = dest_host
    if message:
        vmove.message = message
    vmove.save()


class StagedHostFailureTask(base.MasakariTask):
    def __init__(self, context, novaclient, **kwargs):
        self.state_store = kwargs.pop('state_store', None)
        self.owner = kwargs.pop('owner', _owner_id())
        super(StagedHostFailureTask, self).__init__(
            context, novaclient, **kwargs)

    def _store(self):
        if self.state_store is None:
            self.state_store = state.EtcdStagedRecoveryStore(
                CONF, owner=self.owner)
        return self.state_store

    def _assert_enabled(self):
        if not CONF.staged_recovery.enabled:
            raise exception.MasakariException(
                reason='[staged_recovery] enabled must be true when staged '
                       'host recovery tasks are configured')
        self._store().assert_available()


class ReconcileStagedRecoveryTask(StagedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['host_name', 'notification_uuid']
        super(ReconcileStagedRecoveryTask, self).__init__(
            context, novaclient, **kwargs)

    def _initial_state_values(self, vmove, server):
        return {
            'instance_name': vmove.instance_name,
            'source_host': vmove.source_host,
            'dest_host': vmove.dest_host,
            'original_vm_state': _vm_state(server),
            'original_task_state': _task_state(server),
            'original_power_state': _power_state(server),
            'current_vm_state': _vm_state(server),
            'step': state.STEP_DISCOVERED,
        }

    def _reconcile_state_from_server(self, vmove, server, current_state):
        patch = {
            'current_vm_state': _vm_state(server),
            'current_task_state': _task_state(server),
            'current_power_state': _power_state(server),
        }
        server_host = _server_host(server)
        if server_host and server_host != vmove.source_host:
            patch['dest_host'] = server_host
            if _is_active(server):
                patch['step'] = state.STEP_ACTIVE
            elif _is_stopped(server):
                if (CONF.staged_recovery.start_only_originally_active and
                        current_state.get('original_vm_state') != 'active'):
                    patch['step'] = state.STEP_IGNORED
                else:
                    patch['step'] = state.STEP_EVACUATED_STOPPED
        if len(patch) > 3 or current_state.get('current_vm_state') != (
                patch['current_vm_state']):
            return self._store().update_instance_state(
                vmove.notification_uuid, vmove.instance_uuid, patch)
        return current_state

    def execute(self, host_name, notification_uuid):
        self._assert_enabled()
        vmoves = objects.VMoveList.get_all_vmoves(
            self.context, notification_uuid)

        for vmove in vmoves:
            server = self.novaclient.get_server(
                self.context, vmove.instance_uuid)
            current = self._store().create_or_get_instance_state(
                notification_uuid, vmove.instance_uuid,
                self._initial_state_values(vmove, server))
            self._reconcile_state_from_server(vmove, server, current)


class EvacuateToStoppedTask(StagedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['host_name', 'notification_uuid']
        self.update_host_method = kwargs.pop('update_host_method', None)
        super(EvacuateToStoppedTask, self).__init__(
            context, novaclient, **kwargs)

    def _get_target_vmoves(self, notification_uuid):
        pending = objects.VMoveList.get_all_vmoves(
            self.context, notification_uuid,
            status=fields.VMoveStatus.PENDING)
        ongoing = objects.VMoveList.get_all_vmoves(
            self.context, notification_uuid,
            status=fields.VMoveStatus.ONGOING)
        by_instance = {}
        for vmove in list(pending) + list(ongoing):
            by_instance[vmove.instance_uuid] = vmove
        return list(by_instance.values())

    def _wait_for_evacuation_confirmation(self, vmove):
        def _wait():
            server = self.novaclient.get_server(
                self.context, vmove.instance_uuid)
            if _server_host(server) != vmove.source_host and (
                    _is_stopped(server) or _is_active(server)):
                raise loopingcall.LoopingCallDone(server)
            if _vm_state(server) == 'error':
                raise exception.InstanceEvacuateFailed(
                    instance_uuid=vmove.instance_uuid)

        server = self.novaclient.get_server(self.context, vmove.instance_uuid)
        if _server_host(server) != vmove.source_host and (
                _is_stopped(server) or _is_active(server)):
            return server

        timer = loopingcall.FixedIntervalWithTimeoutLoopingCall(_wait)
        try:
            return timer.start(
                interval=max(CONF.verify_interval, 0.01),
                timeout=CONF.wait_period_after_evacuation).wait()
        finally:
            timer.stop()

    def _prepare_instance_for_evacuate(self, instance):
        vm_state = _vm_state(instance)
        task_state = _task_state(instance)
        if vm_state not in ['active', 'error', 'stopped']:
            self.novaclient.reset_instance_state(self.context, instance.id)
            instance = self.novaclient.get_server(self.context, instance.id)
            if vm_state == 'resized' and _power_state(instance) != SHUTDOWN:
                return instance
        elif task_state is not None:
            self.novaclient.reset_instance_state(self.context, instance.id)
            instance = self.novaclient.get_server(self.context, instance.id)
        return instance

    def _evacuate_and_confirm(self, vmove, reserved_host=None):
        instance = self.novaclient.get_server(
            self.context, vmove.instance_uuid)
        instance_already_locked = getattr(instance, 'locked', False)

        if not instance_already_locked:
            self.novaclient.lock_server(self.context, instance.id)

        try:
            current = self._store().create_or_get_instance_state(
                vmove.notification_uuid, vmove.instance_uuid,
                {'instance_name': vmove.instance_name,
                 'source_host': vmove.source_host,
                 'original_vm_state': _vm_state(instance),
                 'original_task_state': _task_state(instance),
                 'original_power_state': _power_state(instance),
                 'current_vm_state': _vm_state(instance),
                 'step': state.STEP_DISCOVERED})
            if current.get('step') in (state.STEP_EVACUATED_STOPPED,
                                       state.STEP_ACTIVE,
                                       state.STEP_IGNORED):
                return

            instance = self._prepare_instance_for_evacuate(instance)
            self._store().transition_instance_state(
                vmove.notification_uuid, vmove.instance_uuid,
                [state.STEP_DISCOVERED, state.STEP_EVACUATING],
                state.STEP_EVACUATING)
            _update_vmove(vmove, status=fields.VMoveStatus.ONGOING,
                          start_time=timeutils.utcnow())

            self.novaclient.evacuate_instance_stopped(
                self.context, instance.id, target=reserved_host)

            server = self._wait_for_evacuation_confirmation(vmove)
            dest_host = _server_host(server)
            _update_vmove(vmove, status=fields.VMoveStatus.SUCCEEDED,
                          dest_host=dest_host)
            self._store().update_instance_state(
                vmove.notification_uuid, vmove.instance_uuid,
                {'step': (state.STEP_ACTIVE if _is_active(server) else
                          state.STEP_EVACUATED_STOPPED),
                 'dest_host': dest_host,
                 'current_vm_state': _vm_state(server)})
        except Exception as exc:
            LOG.warning('Failed staged evacuation for instance %(uuid)s: '
                        '%(error)s',
                        {'uuid': vmove.instance_uuid, 'error': exc})
            _update_vmove(vmove, status=fields.VMoveStatus.FAILED,
                          message=str(exc))
            self._store().mark_failed(vmove.notification_uuid,
                                      vmove.instance_uuid, exc)
        finally:
            _update_vmove(vmove, end_time=timeutils.utcnow())
            if not instance_already_locked:
                self.novaclient.unlock_server(self.context, instance.id)

    def execute(self, host_name, notification_uuid, reserved_host=None):
        self._assert_enabled()
        all_vmoves = self._get_target_vmoves(notification_uuid)

        if reserved_host:
            if CONF.host_failure.add_reserved_host_to_aggregate:
                aggregates = self.novaclient.get_aggregate_list(self.context)
                for aggregate in aggregates:
                    if host_name in aggregate.hosts:
                        try:
                            self.novaclient.add_host_to_aggregate(
                                self.context, reserved_host, aggregate)
                        except exception.Conflict:
                            LOG.info("Host '%(host)s' is already in "
                                     "aggregate '%(aggregate)s'.",
                                     {'host': reserved_host,
                                      'aggregate': aggregate.name})
            self.novaclient.enable_disable_service(
                self.context, reserved_host, enable=True)
            if self.update_host_method:
                self.update_host_method(self.context, reserved_host)

        thread_pool = greenpool.GreenPool(
            CONF.host_failure_recovery_threads)
        for vmove in all_vmoves:
            thread_pool.spawn_n(self._evacuate_and_confirm, vmove,
                                reserved_host)
        thread_pool.waitall()

        all_vmoves = objects.VMoveList.get_all_vmoves(
            self.context, notification_uuid)
        failed_vmoves = [i.instance_uuid for i in all_vmoves
                         if i.status == fields.VMoveStatus.FAILED]
        if failed_vmoves:
            msg = ("Failed to evacuate instances '%s' from host '%s'" %
                   (','.join(failed_vmoves), host_name))
            raise exception.HostRecoveryFailureException(message=msg)


class BatchedStartInstancesTask(StagedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs['requires'] = ['notification_uuid']
        self.limiter = kwargs.pop('limiter', None)
        super(BatchedStartInstancesTask, self).__init__(
            context, novaclient, **kwargs)

    def _limiter(self):
        if self.limiter is None:
            self.limiter = staged_limiter.EtcdStartLimiter(
                self.context, self._store(), self.owner)
        return self.limiter

    def _wait_for_start_result(self, instance_uuid):
        def _wait():
            server = self.novaclient.get_server(self.context, instance_uuid)
            if _is_active(server) or _vm_state(server) == 'error':
                raise loopingcall.LoopingCallDone(server)

        server = self.novaclient.get_server(self.context, instance_uuid)
        if _is_active(server) or _vm_state(server) == 'error':
            return server

        timer = loopingcall.FixedIntervalWithTimeoutLoopingCall(_wait)
        try:
            return timer.start(
                interval=max(CONF.verify_interval, 0.01),
                timeout=CONF.staged_recovery.start_timeout).wait()
        finally:
            timer.stop()

    def _start_one(self, instance_state):
        notification_uuid = instance_state['notification_uuid']
        instance_uuid = instance_state['instance_uuid']
        dest_host = instance_state['dest_host']
        slot = None
        try:
            self._store().transition_instance_state(
                notification_uuid, instance_uuid,
                [state.STEP_EVACUATED_STOPPED,
                 state.STEP_WAITING_START_SLOT],
                state.STEP_WAITING_START_SLOT)
            slot = self._limiter().acquire(notification_uuid, instance_uuid,
                                           dest_host)
            self._store().transition_instance_state(
                notification_uuid, instance_uuid,
                [state.STEP_WAITING_START_SLOT], state.STEP_STARTING)
            self.novaclient.start_server(self.context, instance_uuid)
            server = self._wait_for_start_result(instance_uuid)
            if _is_active(server):
                self._store().update_instance_state(
                    notification_uuid, instance_uuid,
                    {'step': state.STEP_ACTIVE,
                     'current_vm_state': _vm_state(server)})
                return True

            self._store().mark_failed(
                notification_uuid, instance_uuid,
                'Nova reported instance ERROR during staged start')
            return False
        except loopingcall.LoopingCallTimeOut as exc:
            self._store().mark_failed(notification_uuid, instance_uuid, exc)
            return False
        except Exception as exc:
            self._store().mark_failed(notification_uuid, instance_uuid, exc)
            return False
        finally:
            if slot is not None:
                try:
                    if CONF.staged_recovery.batch_delay:
                        eventlet.sleep(CONF.staged_recovery.batch_delay)
                    self._limiter().release(slot)
                except Exception:
                    LOG.warning('Failed to release staged start slot for '
                                'instance %s', instance_uuid, exc_info=True)

    def execute(self, notification_uuid):
        self._assert_enabled()
        instance_states = self._store().list_instance_states(
            notification_uuid)
        candidates = []
        for instance_state in instance_states:
            if instance_state.get('step') != state.STEP_EVACUATED_STOPPED:
                continue
            if (CONF.staged_recovery.start_only_originally_active and
                    instance_state.get('original_vm_state') != 'active'):
                self._store().update_instance_state(
                    instance_state['notification_uuid'],
                    instance_state['instance_uuid'],
                    {'step': state.STEP_IGNORED})
                continue
            candidates.append(instance_state)

        pool_size = max(CONF.host_failure_recovery_threads,
                        CONF.staged_recovery.max_parallel_starts_per_host)
        thread_pool = greenpool.GreenPool(pool_size)
        for instance_state in candidates:
            thread_pool.spawn_n(self._start_one, instance_state)
        thread_pool.waitall()

        all_states = self._store().list_instance_states(notification_uuid)
        failed = [
            s['instance_uuid'] for s in all_states
            if (s.get('original_vm_state') == 'active' and
                s.get('step') == state.STEP_FAILED)]
        if failed:
            raise exception.StagedStartFailureException(
                message='Failed to start staged recovery instances: %s' %
                        ','.join(failed))
