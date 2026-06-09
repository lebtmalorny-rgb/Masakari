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
import json
from urllib import parse

import eventlet

from oslo_log import log as logging

import masakari.conf
from masakari import exception


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_CAS_RETRIES = 8

STEP_DISCOVERED = 'DISCOVERED'
STEP_EVACUATING = 'EVACUATING'
STEP_EVACUATED_STOPPED = 'EVACUATED_STOPPED'
STEP_WAITING_START_SLOT = 'WAITING_START_SLOT'
STEP_STARTING = 'STARTING'
STEP_ACTIVE = 'ACTIVE'
STEP_FAILED = 'FAILED'
STEP_IGNORED = 'IGNORED'

FINAL_STEPS = (STEP_ACTIVE, STEP_FAILED, STEP_IGNORED)

CONFIG_MAX_PARALLEL_STARTS_PER_HOST = 'max_parallel_starts_per_host'


class ConcurrentStateUpdate(exception.MasakariException):
    msg_fmt = "Concurrent staged recovery state update for %(key)s."


class StateNotFound(exception.MasakariException):
    msg_fmt = "Staged recovery state %(key)s was not found."


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def utcnow_iso():
    return utcnow().replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse_iso(value):
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    return datetime.datetime.fromisoformat(value)


def encode_key_part(value):
    return parse.quote(str(value), safe='')


def decode_key_part(value):
    return parse.unquote(value)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode(
        'utf-8')


def loads(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return json.loads(value)


def parse_etcd_backend_url(conf):
    url = (conf.staged_recovery.etcd_backend_url or
           conf.coordination.backend_url)
    if not url:
        raise exception.MasakariException(
            reason='[staged_recovery] etcd_backend_url or [coordination] '
                   'backend_url is required for staged recovery')

    if url.startswith('etcd3+'):
        url = url[len('etcd3+'):]

    parsed = parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise exception.MasakariException(
            reason='staged recovery etcd backend must use etcd3+http(s) '
                   'or http(s)')

    api_path = parsed.path or None
    return {
        'host': parsed.hostname,
        'port': parsed.port or 2379,
        'protocol': parsed.scheme,
        'api_path': api_path,
    }


class EtcdStagedRecoveryStore(object):
    def __init__(self, conf, owner, client=None):
        self.conf = conf
        self.owner = owner
        self.prefix = conf.staged_recovery.etcd_prefix.rstrip('/')
        self.client = client or self._make_client()

    def _make_client(self):
        import etcd3gw

        endpoint = parse_etcd_backend_url(self.conf)
        return etcd3gw.client(
            host=endpoint['host'],
            port=endpoint['port'],
            protocol=endpoint['protocol'],
            ca_cert=self.conf.staged_recovery.etcd_ca_cert,
            cert_key=self.conf.staged_recovery.etcd_key_file,
            cert_cert=self.conf.staged_recovery.etcd_cert_file,
            timeout=self.conf.staged_recovery.etcd_timeout,
            api_path=endpoint['api_path'])

    def assert_available(self):
        try:
            self.client.status()
        except Exception as exc:
            raise exception.MasakariException(
                reason='staged recovery etcd backend is unavailable: %s' %
                       exc)

    def instance_key(self, notification_uuid, instance_uuid):
        return '%s/notifications/%s/instances/%s' % (
            self.prefix,
            encode_key_part(notification_uuid),
            encode_key_part(instance_uuid))

    def notification_instances_prefix(self, notification_uuid):
        return '%s/notifications/%s/instances/' % (
            self.prefix, encode_key_part(notification_uuid))

    def start_lease_key(self, dest_host, instance_uuid):
        return '%s/start-leases/%s/%s' % (
            self.prefix,
            encode_key_part(dest_host),
            encode_key_part(instance_uuid))

    def start_lease_prefix(self, dest_host):
        return '%s/start-leases/%s/' % (
            self.prefix, encode_key_part(dest_host))

    def runtime_config_key(self, name):
        return '%s/config/%s' % (self.prefix, encode_key_part(name))

    def _get_raw(self, key):
        result = self.client.get(key)
        if not result:
            return None
        first = result[0]
        if isinstance(first, tuple):
            return first[0]
        return first

    def _list_values(self, prefix):
        return [loads(value) for value, _metadata in
                self.client.get_prefix(prefix)]

    def _validate_positive_int(self, value, name):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise exception.InvalidInput(
                reason='%s must be an integer' % name)
        if value < 1:
            raise exception.InvalidInput(
                reason='%s must be >= 1' % name)
        return value

    def _base_state(self, notification_uuid, instance_uuid, values):
        now = utcnow_iso()
        state = {
            'schema_version': SCHEMA_VERSION,
            'notification_uuid': str(notification_uuid),
            'instance_uuid': str(instance_uuid),
            'owner': self.owner,
            'created_at': now,
            'updated_at': now,
            'step': STEP_DISCOVERED,
            'attempt': 0,
            'last_error': None,
        }
        state.update(values)
        state.setdefault('notification_uuid', str(notification_uuid))
        state.setdefault('instance_uuid', str(instance_uuid))
        return state

    def create_or_get_instance_state(self, notification_uuid, instance_uuid,
                                     values):
        key = self.instance_key(notification_uuid, instance_uuid)
        state = self._base_state(notification_uuid, instance_uuid, values)
        if self.client.create(key, dumps(state)):
            return state
        raw = self._get_raw(key)
        if raw is None:
            raise StateNotFound(key=key)
        return loads(raw)

    def get_runtime_config(self, name):
        raw = self._get_raw(self.runtime_config_key(name))
        if raw is None:
            return None
        return loads(raw)

    def set_runtime_config(self, name, value):
        record = {
            'schema_version': SCHEMA_VERSION,
            'name': name,
            'value': value,
            'owner': self.owner,
            'updated_at': utcnow_iso(),
        }
        self.client.put(self.runtime_config_key(name), dumps(record))
        return record

    def clear_runtime_config(self, name):
        return self.client.delete(self.runtime_config_key(name))

    def get_max_parallel_starts_per_host(self, default):
        record = self.get_runtime_config(
            CONFIG_MAX_PARALLEL_STARTS_PER_HOST)
        if record is None:
            return self._validate_positive_int(
                default, CONFIG_MAX_PARALLEL_STARTS_PER_HOST)
        return self._validate_positive_int(
            record.get('value'), CONFIG_MAX_PARALLEL_STARTS_PER_HOST)

    def set_max_parallel_starts_per_host(self, value):
        value = self._validate_positive_int(
            value, CONFIG_MAX_PARALLEL_STARTS_PER_HOST)
        self.set_runtime_config(CONFIG_MAX_PARALLEL_STARTS_PER_HOST, value)
        return value

    def clear_max_parallel_starts_per_host(self):
        return self.clear_runtime_config(
            CONFIG_MAX_PARALLEL_STARTS_PER_HOST)

    def get_instance_state(self, notification_uuid, instance_uuid):
        raw = self._get_raw(self.instance_key(notification_uuid,
                                             instance_uuid))
        return loads(raw) if raw is not None else None

    def list_instance_states(self, notification_uuid):
        return self._list_values(
            self.notification_instances_prefix(notification_uuid))

    def list_stale_instance_states(self, older_than_seconds, steps, now=None):
        if now is None:
            now_dt = utcnow()
        elif isinstance(now, str):
            now_dt = _parse_iso(now)
        else:
            now_dt = now

        states = self._list_values('%s/notifications/' % self.prefix)
        stale = []
        for state in states:
            if state.get('step') not in steps:
                continue
            updated_at = state.get('updated_at')
            if not updated_at:
                continue
            age = (now_dt - _parse_iso(updated_at)).total_seconds()
            if age >= older_than_seconds:
                stale.append(state)
        return stale

    def update_instance_state(self, notification_uuid, instance_uuid, patch):
        key = self.instance_key(notification_uuid, instance_uuid)
        for _attempt in range(MAX_CAS_RETRIES):
            current_raw = self._get_raw(key)
            if current_raw is None:
                raise StateNotFound(key=key)

            current = loads(current_raw)
            new = dict(current)
            new.update(patch)
            new['owner'] = self.owner
            new['updated_at'] = patch.get('updated_at', utcnow_iso())

            if self.client.replace(key, current_raw, dumps(new)):
                return new
            eventlet.sleep(0)

        raise ConcurrentStateUpdate(key=key)

    def transition_instance_state(self, notification_uuid, instance_uuid,
                                  expected_steps, new_step, patch=None):
        key = self.instance_key(notification_uuid, instance_uuid)
        patch = patch or {}
        for _attempt in range(MAX_CAS_RETRIES):
            current_raw = self._get_raw(key)
            if current_raw is None:
                raise StateNotFound(key=key)

            current = loads(current_raw)
            if current.get('step') not in expected_steps:
                return None

            new = dict(current)
            new.update(patch)
            new['step'] = new_step
            new['owner'] = self.owner
            new['updated_at'] = patch.get('updated_at', utcnow_iso())

            if self.client.replace(key, current_raw, dumps(new)):
                return new
            eventlet.sleep(0)

        raise ConcurrentStateUpdate(key=key)

    def mark_failed(self, notification_uuid, instance_uuid, error):
        return self.update_instance_state(
            notification_uuid, instance_uuid,
            {'step': STEP_FAILED, 'last_error': str(error)})

    def list_start_leases(self, dest_host):
        return self._list_values(self.start_lease_prefix(dest_host))

    def create_start_lease(self, notification_uuid, instance_uuid, dest_host,
                           ttl):
        key = self.start_lease_key(dest_host, instance_uuid)
        lease = self.client.lease(ttl=ttl)
        now = utcnow()
        record = {
            'schema_version': SCHEMA_VERSION,
            'notification_uuid': str(notification_uuid),
            'instance_uuid': str(instance_uuid),
            'dest_host': dest_host,
            'owner': self.owner,
            'etcd_lease_id': lease.id,
            'created_at': now.replace(microsecond=0).isoformat().replace(
                '+00:00', 'Z'),
            'expires_at': (now + datetime.timedelta(seconds=ttl)).replace(
                microsecond=0).isoformat().replace('+00:00', 'Z'),
        }
        if self.client.create(key, dumps(record), lease=lease):
            return record
        try:
            lease.revoke()
        except Exception:
            LOG.debug('Failed to revoke unused staged start lease %s',
                      lease.id, exc_info=True)
        return None

    def _lease_by_id(self, lease_id):
        if hasattr(self.client, 'leases') and lease_id in self.client.leases:
            return self.client.leases[lease_id]
        try:
            from etcd3gw.lease import Lease
        except ImportError:
            return None
        return Lease(lease_id, self.client)

    def refresh_start_lease(self, lease_record):
        lease_id = lease_record.get('etcd_lease_id')
        lease = self._lease_by_id(lease_id)
        if lease is None:
            return False
        return lease.refresh() >= 0

    def release_start_lease(self, dest_host, instance_uuid):
        key = self.start_lease_key(dest_host, instance_uuid)
        raw = self._get_raw(key)
        lease_id = loads(raw).get('etcd_lease_id') if raw else None
        self.client.delete(key)
        if lease_id is not None:
            lease = self._lease_by_id(lease_id)
            if lease is not None:
                try:
                    lease.revoke()
                except Exception:
                    LOG.debug('Failed to revoke staged start lease %s',
                              lease_id, exc_info=True)
