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

import datetime
from urllib import parse

import eventlet
from oslo_log import log as logging

import masakari.conf
from masakari.engine.drivers.taskflow import staged_state_etcd as staged
from masakari import exception


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_CAS_RETRIES = 8

FENCE_REQUIRED = 'FENCE_REQUIRED'
FENCING = 'FENCING'
FENCED = 'FENCED'
FENCE_FAILED = 'FENCE_FAILED'


def utcnow_iso():
    return staged.utcnow_iso()


def _parse_iso(value):
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    return datetime.datetime.fromisoformat(value)


def _parse_backend_url(conf):
    url = (conf.redfish_fencing.etcd_backend_url or
           conf.staged_recovery.etcd_backend_url or
           conf.coordination.backend_url)
    if not url:
        raise exception.MasakariException(
            reason='[redfish_fencing] etcd_backend_url, '
                   '[staged_recovery] etcd_backend_url, or [coordination] '
                   'backend_url is required for Redfish fencing')
    if url.startswith('etcd3+'):
        url = url[len('etcd3+'):]
    parsed = parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise exception.MasakariException(
            reason='Redfish fencing etcd backend must use etcd3+http(s) '
                   'or http(s)')
    return {
        'host': parsed.hostname,
        'port': parsed.port or 2379,
        'protocol': parsed.scheme,
        'api_path': parsed.path or None,
    }


class EtcdFencingStore(object):
    def __init__(self, conf, owner, client=None):
        self.conf = conf
        self.owner = owner
        self.prefix = conf.redfish_fencing.etcd_prefix.rstrip('/')
        self.client = client or self._make_client()

    def _make_client(self):
        import etcd3gw

        endpoint = _parse_backend_url(self.conf)
        return etcd3gw.client(
            host=endpoint['host'],
            port=endpoint['port'],
            protocol=endpoint['protocol'],
            ca_cert=self.conf.staged_recovery.etcd_ca_cert,
            cert_key=self.conf.staged_recovery.etcd_key_file,
            cert_cert=self.conf.staged_recovery.etcd_cert_file,
            timeout=self.conf.redfish_fencing.etcd_timeout,
            api_path=endpoint['api_path'])

    def assert_available(self):
        try:
            self.client.status()
        except Exception as exc:
            raise exception.MasakariException(
                reason='Redfish fencing etcd backend is unavailable: %s' %
                       exc)

    def failure_key(self, segment_uuid, event_id, hostname):
        return '%s/failures/%s/%s/%s' % (
            self.prefix,
            staged.encode_key_part(segment_uuid),
            staged.encode_key_part(event_id),
            staged.encode_key_part(hostname))

    def failures_prefix(self, segment_uuid):
        return '%s/failures/%s/' % (
            self.prefix, staged.encode_key_part(segment_uuid))

    def host_state_key(self, hostname):
        return '%s/hosts/%s' % (
            self.prefix, staged.encode_key_part(hostname))

    def segment_lock_key(self, segment_uuid):
        return '%s/locks/segments/%s' % (
            self.prefix, staged.encode_key_part(segment_uuid))

    def host_lock_key(self, hostname):
        return '%s/locks/hosts/%s' % (
            self.prefix, staged.encode_key_part(hostname))

    def _get_raw(self, key):
        result = self.client.get(key)
        if not result:
            return None
        first = result[0]
        if isinstance(first, tuple):
            return first[0]
        return first

    def _put_state(self, hostname, state):
        self.client.put(self.host_state_key(hostname), staged.dumps(state))
        return state

    def _base_state(self, segment_uuid, event_id, hostname,
                    notification_uuid):
        now = utcnow_iso()
        return {
            'schema_version': SCHEMA_VERSION,
            'segment_uuid': str(segment_uuid),
            'event_id': str(event_id),
            'hostname': hostname,
            'notification_uuid': str(notification_uuid),
            'owner': self.owner,
            'state': FENCE_REQUIRED,
            'created_at': now,
            'updated_at': now,
            'last_error': None,
        }

    def register_host_failure(self, segment_uuid, event_id, hostname,
                              notification_uuid):
        now = utcnow_iso()
        failure = {
            'schema_version': SCHEMA_VERSION,
            'segment_uuid': str(segment_uuid),
            'event_id': str(event_id),
            'hostname': hostname,
            'notification_uuid': str(notification_uuid),
            'owner': self.owner,
            'created_at': now,
        }
        self.client.put(
            self.failure_key(segment_uuid, event_id, hostname),
            staged.dumps(failure))

        state = self.get_fencing_state(hostname)
        if state is None or state.get('state') in (FENCED, FENCE_FAILED):
            state = self._base_state(
                segment_uuid, event_id, hostname, notification_uuid)
        else:
            state = dict(state)
            state.update({
                'segment_uuid': str(segment_uuid),
                'event_id': str(event_id),
                'notification_uuid': str(notification_uuid),
                'state': FENCE_REQUIRED,
                'owner': self.owner,
                'updated_at': now,
                'last_error': None,
            })
        return self._put_state(hostname, state)

    def list_recent_failures(self, segment_uuid, window_seconds,
                             now=None, event_id=None):
        if now is None:
            now_dt = staged.utcnow()
        elif isinstance(now, str):
            now_dt = _parse_iso(now)
        else:
            now_dt = now

        failures = []
        for raw, _metadata in self.client.get_prefix(
                self.failures_prefix(segment_uuid)):
            failure = staged.loads(raw)
            if event_id is not None and str(failure.get('event_id')) != str(
                    event_id):
                continue
            if window_seconds > 0:
                created_at = failure.get('created_at')
                if not created_at:
                    continue
                age = (now_dt - _parse_iso(created_at)).total_seconds()
                if age > window_seconds:
                    continue
            failures.append(failure)
        failures.sort(key=lambda failure: failure.get('hostname', ''))
        return failures

    def get_fencing_state(self, hostname):
        raw = self._get_raw(self.host_state_key(hostname))
        return staged.loads(raw) if raw is not None else None

    def transition_fencing_state(self, hostname, expected_states, new_state,
                                 patch=None):
        key = self.host_state_key(hostname)
        patch = patch or {}
        for _attempt in range(MAX_CAS_RETRIES):
            current_raw = self._get_raw(key)
            if current_raw is None:
                return None
            current = staged.loads(current_raw)
            if current.get('state') not in expected_states:
                return None
            new = dict(current)
            new.update(patch)
            new['state'] = new_state
            new['owner'] = self.owner
            new['updated_at'] = patch.get('updated_at', utcnow_iso())
            if self.client.replace(key, current_raw, staged.dumps(new)):
                return new
            eventlet.sleep(0)
        raise staged.ConcurrentStateUpdate(key=key)

    def update_fencing_state(self, hostname, patch):
        key = self.host_state_key(hostname)
        for _attempt in range(MAX_CAS_RETRIES):
            current_raw = self._get_raw(key)
            if current_raw is None:
                raise exception.HostRecoveryFailureException(
                    message='Fencing state for host %s was not found' %
                            hostname)
            current = staged.loads(current_raw)
            new = dict(current)
            new.update(patch)
            new['owner'] = self.owner
            new['updated_at'] = patch.get('updated_at', utcnow_iso())
            if self.client.replace(key, current_raw, staged.dumps(new)):
                return new
            eventlet.sleep(0)
        raise staged.ConcurrentStateUpdate(key=key)

    def mark_fenced(self, hostname, event_id, proof):
        patch = dict(proof)
        patch.update({
            'event_id': str(event_id),
            'state': FENCED,
            'last_error': None,
        })
        return self.update_fencing_state(hostname, patch)

    def mark_fence_failed(self, hostname, event_id, error):
        return self.update_fencing_state(
            hostname, {
                'event_id': str(event_id),
                'state': FENCE_FAILED,
                'last_error': str(error),
            })

    def assert_hosts_fenced(self, hostnames):
        missing = []
        failed = []
        for hostname in hostnames:
            state = self.get_fencing_state(hostname)
            if state is None:
                missing.append(hostname)
                continue
            if state.get('state') != FENCED:
                failed.append('%s:%s' % (
                    hostname, state.get('state', 'UNKNOWN')))
                continue
            if state.get('verified_power_state') != 'Off':
                failed.append('%s:PowerState=%s' % (
                    hostname, state.get('verified_power_state')))

        if missing or failed:
            raise exception.HostRecoveryFailureException(
                message='Redfish fencing proof is missing or invalid; '
                        'missing=%s failed=%s' %
                        (','.join(missing), ','.join(failed)))

    def _acquire_lock(self, key, lock_type, name, event_id, ttl):
        lease = self.client.lease(ttl=ttl)
        record = {
            'schema_version': SCHEMA_VERSION,
            'lock_type': lock_type,
            'name': str(name),
            'event_id': str(event_id),
            'owner': self.owner,
            'etcd_lease_id': lease.id,
            'created_at': utcnow_iso(),
        }
        if self.client.create(key, staged.dumps(record), lease=lease):
            return record
        try:
            lease.revoke()
        except Exception:
            LOG.debug('Failed to revoke unused fencing lock lease %s',
                      lease.id, exc_info=True)
        return None

    def acquire_segment_recovery_lock(self, segment_uuid, event_id, ttl):
        return self._acquire_lock(
            self.segment_lock_key(segment_uuid), 'segment', segment_uuid,
            event_id, ttl)

    def acquire_host_fencing_lock(self, hostname, event_id, ttl):
        return self._acquire_lock(
            self.host_lock_key(hostname), 'host', hostname, event_id, ttl)

    def _lease_by_id(self, lease_id):
        if hasattr(self.client, 'leases') and lease_id in self.client.leases:
            return self.client.leases[lease_id]
        try:
            from etcd3gw.lease import Lease
        except ImportError:
            return None
        return Lease(lease_id, self.client)

    def _release_lock(self, key, lock_record):
        lease_id = lock_record.get('etcd_lease_id') if lock_record else None
        self.client.delete(key)
        if lease_id is not None:
            lease = self._lease_by_id(lease_id)
            if lease is not None:
                try:
                    lease.revoke()
                except Exception:
                    LOG.debug('Failed to revoke fencing lock lease %s',
                              lease_id, exc_info=True)

    def release_segment_recovery_lock(self, segment_uuid, lock_record):
        self._release_lock(self.segment_lock_key(segment_uuid), lock_record)

    def release_host_fencing_lock(self, hostname, lock_record):
        self._release_lock(self.host_lock_key(hostname), lock_record)
