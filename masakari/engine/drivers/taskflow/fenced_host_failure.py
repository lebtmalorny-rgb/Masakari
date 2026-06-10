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

from oslo_log import log as logging

import masakari.conf
from masakari.engine.drivers.taskflow import base
from masakari.engine.drivers.taskflow import fencing_state_etcd as state
from masakari.engine.drivers.taskflow import redfish_fencing
from masakari import exception
from masakari import objects


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)


def _owner_id():
    return '%s-%s' % (socket.gethostname(), id(object()))


def _host_name(host):
    value = getattr(host, 'name', None)
    if isinstance(value, str):
        return value
    value = getattr(host, '_mock_name', None)
    if isinstance(value, str):
        return value
    return str(host)


class FencedHostFailureTask(base.MasakariTask):
    def __init__(self, context, novaclient, **kwargs):
        self.fencing_store = kwargs.pop('fencing_store', None)
        self.owner = kwargs.pop('owner', _owner_id())
        super(FencedHostFailureTask, self).__init__(
            context, novaclient, **kwargs)

    def _store(self):
        if self.fencing_store is None:
            self.fencing_store = state.EtcdFencingStore(
                CONF, owner=self.owner)
        return self.fencing_store

    def _assert_enabled(self):
        if not CONF.redfish_fencing.enabled:
            raise exception.HostRecoveryFailureException(
                message='[redfish_fencing] enabled must be true when '
                        'fencing tasks are configured')
        self._store().assert_available()


class CollectFailureSetTask(FencedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs.setdefault('requires', ['segment_uuid', 'event_id',
                                      'host_name', 'notification_uuid'])
        kwargs.setdefault('provides', 'failure_set')
        self.host_config_loader = kwargs.pop(
            'host_config_loader', redfish_fencing.load_host_config)
        self.host_list_method = kwargs.pop('host_list_method', None)
        super(CollectFailureSetTask, self).__init__(
            context, novaclient, **kwargs)

    def _segment_hosts(self, segment_uuid):
        if self.host_list_method is not None:
            hosts = self.host_list_method()
        else:
            hosts = objects.HostList.get_all(
                self.context,
                filters={'failover_segment_id': segment_uuid,
                         'reserved': False})
        return [_host_name(host) for host in hosts]

    def execute(self, segment_uuid, event_id, host_name,
                notification_uuid=None):
        self._assert_enabled()
        notification_uuid = notification_uuid or event_id
        self._store().register_host_failure(
            segment_uuid, event_id, host_name, notification_uuid)

        event_filter = event_id if (
            CONF.redfish_fencing.multi_host_batch_window == 0) else None
        failures = self._store().list_recent_failures(
            segment_uuid, CONF.redfish_fencing.multi_host_batch_window,
            event_id=event_filter)
        failure_set = sorted({failure['hostname'] for failure in failures})
        if host_name not in failure_set:
            failure_set.append(host_name)
            failure_set.sort()

        max_hosts = CONF.redfish_fencing.max_auto_fence_hosts_per_segment
        if len(failure_set) > max_hosts:
            raise exception.HostRecoveryFailureException(
                message='Refusing to Redfish-fence %d hosts in segment %s; '
                        'limit is %d' %
                        (len(failure_set), segment_uuid, max_hosts))

        for hostname in failure_set:
            self.host_config_loader(hostname, CONF)

        segment_hosts = self._segment_hosts(segment_uuid)
        surviving = len(set(segment_hosts) - set(failure_set))
        if surviving < CONF.redfish_fencing.min_surviving_compute_hosts:
            raise exception.HostRecoveryFailureException(
                message='Refusing Redfish fencing for segment %s; only %d '
                        'surviving compute hosts remain' %
                        (segment_uuid, surviving))
        return failure_set


class DisableFailureSetTask(FencedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs.setdefault('requires', ['failure_set'])
        super(DisableFailureSetTask, self).__init__(
            context, novaclient, **kwargs)

    def execute(self, failure_set):
        for hostname in failure_set:
            self.novaclient.enable_disable_service(
                self.context, hostname, enable=False,
                reason=CONF.host_failure.service_disable_reason)
        return failure_set


class RedfishFenceFailureSetTask(FencedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs.setdefault('requires', ['failure_set', 'event_id',
                                      'segment_uuid', 'notification_uuid'])
        self.client_cls = kwargs.pop(
            'client_cls', redfish_fencing.RedfishFencingClient)
        self.host_config_loader = kwargs.pop(
            'host_config_loader', redfish_fencing.load_host_config)
        super(RedfishFenceFailureSetTask, self).__init__(
            context, novaclient, **kwargs)

    def _fence_one(self, hostname, event_id):
        host_lock = self._store().acquire_host_fencing_lock(
            hostname, event_id, ttl=CONF.redfish_fencing.lock_ttl)
        if host_lock is None:
            raise exception.HostRecoveryFailureException(
                message='Redfish fencing lock is already held for host %s' %
                        hostname)

        client = None
        try:
            transitioned = self._store().transition_fencing_state(
                hostname, [state.FENCE_REQUIRED, state.FENCING],
                state.FENCING)
            if transitioned is None:
                current = self._store().get_fencing_state(hostname)
                if current and current.get('state') == state.FENCED:
                    return
                raise exception.HostRecoveryFailureException(
                    message='Host %s is not ready for Redfish fencing' %
                            hostname)

            host_config = self.host_config_loader(hostname, CONF)
            client = self.client_cls(host_config, CONF)
            proof = client.fence_off()
            self._store().mark_fenced(hostname, event_id, proof)
        except Exception as exc:
            try:
                self._store().mark_fence_failed(hostname, event_id, exc)
            except Exception:
                LOG.debug('Failed to persist Redfish fencing failure for %s',
                          hostname, exc_info=True)
            raise
        finally:
            if client is not None:
                client.close()
            self._store().release_host_fencing_lock(hostname, host_lock)

    def execute(self, failure_set, event_id, segment_uuid,
                notification_uuid=None):
        self._assert_enabled()
        segment_lock = self._store().acquire_segment_recovery_lock(
            segment_uuid, event_id, ttl=CONF.redfish_fencing.lock_ttl)
        if segment_lock is None:
            raise exception.HostRecoveryFailureException(
                message='Redfish segment recovery lock is already held for '
                        'segment %s' % segment_uuid)
        try:
            for hostname in failure_set:
                self._fence_one(hostname, event_id)
            self._store().assert_hosts_fenced(failure_set)
            return failure_set
        finally:
            self._store().release_segment_recovery_lock(
                segment_uuid, segment_lock)


class AssertFailureSetFencedTask(FencedHostFailureTask):
    def __init__(self, context, novaclient, **kwargs):
        kwargs.setdefault('requires', ['failure_set'])
        super(AssertFailureSetFencedTask, self).__init__(
            context, novaclient, **kwargs)

    def execute(self, failure_set):
        self._assert_enabled()
        self._store().assert_hosts_fenced(failure_set)
        return failure_set
