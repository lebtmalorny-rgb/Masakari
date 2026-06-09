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

import eventlet

from oslo_log import log as logging

import masakari.conf
from masakari import coordination
from masakari import exception
from masakari.i18n import _


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)


class StartSlot(object):
    def __init__(self, notification_uuid, instance_uuid, dest_host, lease_id):
        self.notification_uuid = notification_uuid
        self.instance_uuid = instance_uuid
        self.dest_host = dest_host
        self.lease_id = lease_id

    @classmethod
    def from_record(cls, record):
        return cls(record['notification_uuid'], record['instance_uuid'],
                   record['dest_host'], record.get('etcd_lease_id'))


class EtcdStartLimiter(object):
    def __init__(self, context, state_store, owner, coordinator=None):
        self.context = context
        self.state_store = state_store
        self.owner = owner
        self.coordinator = coordinator or coordination.COORDINATOR
        self.max_parallel = (
            CONF.staged_recovery.max_parallel_starts_per_host)
        self.retry_interval = CONF.staged_recovery.slot_retry_interval
        self.ttl = CONF.staged_recovery.slot_lease_ttl
        self._validate_ttl()

    def _validate_ttl(self):
        minimum = (CONF.staged_recovery.start_timeout +
                   CONF.staged_recovery.batch_delay)
        if self.ttl < minimum:
            raise exception.InvalidInput(
                reason=_('[staged_recovery] slot_lease_ttl must be greater '
                         'than or equal to start_timeout + batch_delay'))

    def _lock_name(self, dest_host):
        return 'staged-start-lock-%s' % dest_host

    def acquire(self, notification_uuid, instance_uuid, dest_host):
        while True:
            lock = self.coordinator.get_lock(self._lock_name(dest_host))
            if lock is None:
                raise exception.MasakariException(
                    reason='[coordination] backend_url is required for '
                           'staged recovery start limiting')

            with lock:
                live = self.state_store.list_start_leases(dest_host)
                if len(live) < self.max_parallel:
                    record = self.state_store.create_start_lease(
                        notification_uuid=notification_uuid,
                        instance_uuid=instance_uuid,
                        dest_host=dest_host,
                        ttl=self.ttl)
                    if record is not None:
                        LOG.debug('Acquired staged start slot for instance '
                                  '%(instance)s on %(host)s',
                                  {'instance': instance_uuid,
                                   'host': dest_host})
                        return StartSlot.from_record(record)

            eventlet.sleep(self.retry_interval)

    def release(self, slot):
        self.state_store.release_start_lease(slot.dest_host,
                                             slot.instance_uuid)
