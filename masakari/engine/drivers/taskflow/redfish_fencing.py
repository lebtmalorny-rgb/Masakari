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

import dataclasses
import os
import time
from urllib import parse

import eventlet
from oslo_log import log as logging
import requests
import yaml

import masakari.conf
from masakari import exception


CONF = masakari.conf.CONF
LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class RedfishHostConfig(object):
    hostname: str
    address: str
    systems_uri: str
    username: str
    password: str
    scheme: str = 'https'
    port: int = 443
    base_uri: str = '/redfish/v1'
    auth_type: str = 'session'
    tls_verify: bool = True
    ca_cert: str = None
    expected_system_uuid: str = None
    expected_serial_number: str = None
    expected_asset_tag: str = None
    expected_manufacturer: str = None
    expected_model: str = None


def _load_yaml(path):
    if not path:
        raise exception.RedfishFencingException(
            message='[redfish_fencing] hosts_config_path is required')
    if not os.path.exists(path):
        raise exception.ConfigNotFound(path=path)
    with open(path, 'r') as stream:
        return yaml.safe_load(stream) or {}


def _load_password(hostname, values, conf):
    has_inline = values.get('password') is not None
    if has_inline and not conf.redfish_fencing.allow_insecure_inline_password:
        raise exception.RedfishFencingException(
            message='Inline Redfish password for host %s is disabled; use '
                    'password_file or set '
                    '[redfish_fencing] allow_insecure_inline_password' %
                    hostname)
    if has_inline:
        return str(values['password'])

    password_file = values.get('password_file')
    if password_file:
        with open(password_file, 'r') as stream:
            return stream.read().strip()

    if values.get('password_secret_ref'):
        raise exception.RedfishFencingException(
            message='password_secret_ref is not supported by the direct '
                    'Redfish fencing client; use password_file')

    raise exception.RedfishFencingException(
        message='Redfish password or password_file is required for host %s' %
                hostname)


def _required(values, hostname, name):
    value = values.get(name)
    if value in (None, ''):
        raise exception.RedfishFencingException(
            message='Redfish host %s is missing required option %s' %
                    (hostname, name))
    return value


def load_host_config(hostname, conf=CONF, hosts_file=None):
    mapping = _load_yaml(
        hosts_file or conf.redfish_fencing.hosts_config_path)
    defaults = mapping.get('defaults') or {}
    hosts = mapping.get('hosts') or {}
    if hostname not in hosts:
        raise exception.RedfishFencingException(
            message='No Redfish host mapping found for %s' % hostname)

    values = dict(defaults)
    values.update(hosts[hostname] or {})
    password = _load_password(hostname, values, conf)
    return RedfishHostConfig(
        hostname=hostname,
        address=_required(values, hostname, 'address'),
        systems_uri=_required(values, hostname, 'systems_uri'),
        username=_required(values, hostname, 'username'),
        password=password,
        scheme=values.get('scheme', 'https'),
        port=int(values.get('port', 443)),
        base_uri=values.get('base_uri', '/redfish/v1'),
        auth_type=values.get('auth_type', 'session'),
        tls_verify=bool(values.get('tls_verify', True)),
        ca_cert=values.get('ca_cert'),
        expected_system_uuid=values.get('expected_system_uuid'),
        expected_serial_number=values.get('expected_serial_number'),
        expected_asset_tag=values.get('expected_asset_tag'),
        expected_manufacturer=values.get('expected_manufacturer'),
        expected_model=values.get('expected_model'))


class RedfishFencingClient(object):
    def __init__(self, host_config, conf=CONF, session=None):
        self.host_config = host_config
        self.conf = conf
        self.session = session or requests.Session()
        self.session_location = None
        self._authenticated = False

    def _base_url(self):
        return '%s://%s:%s' % (
            self.host_config.scheme,
            self.host_config.address,
            self.host_config.port)

    def _url(self, uri):
        if uri.startswith('http://') or uri.startswith('https://'):
            return uri
        return parse.urljoin(self._base_url(), uri)

    def _timeout(self):
        return (
            self.conf.redfish_fencing.connect_timeout,
            self.conf.redfish_fencing.read_timeout)

    def _verify(self):
        if self.host_config.ca_cert:
            return self.host_config.ca_cert
        return self.host_config.tls_verify

    def _request(self, method, uri, **kwargs):
        kwargs.setdefault('timeout', self._timeout())
        kwargs.setdefault('verify', self._verify())
        kwargs.setdefault('headers', {'Accept': 'application/json'})
        response = getattr(self.session, method)(self._url(uri), **kwargs)
        response.raise_for_status()
        return response

    def _json(self, method, uri, **kwargs):
        return self._request(method, uri, **kwargs).json()

    def _authenticate(self):
        if self._authenticated:
            return

        self._request('get', self.host_config.base_uri)
        if self.host_config.auth_type == 'session':
            session_uri = '%s/SessionService/Sessions' % (
                self.host_config.base_uri.rstrip('/'))
            response = self._request(
                'post', session_uri,
                json={'UserName': self.host_config.username,
                      'Password': self.host_config.password},
                headers={'Accept': 'application/json',
                         'Content-Type': 'application/json'})
            token = response.headers.get('X-Auth-Token')
            if not token:
                raise exception.RedfishFencingException(
                    message='Redfish session response did not include '
                            'X-Auth-Token for host %s' %
                            self.host_config.hostname)
            self.session.headers.update({'X-Auth-Token': token})
            self.session_location = response.headers.get('Location')
        elif self.host_config.auth_type == 'basic':
            self.session.auth = (
                self.host_config.username, self.host_config.password)
        else:
            raise exception.RedfishFencingException(
                message='Unsupported Redfish auth_type %s for host %s' %
                        (self.host_config.auth_type,
                         self.host_config.hostname))
        self._authenticated = True

    def close(self):
        if self.session_location:
            try:
                self._request('delete', self.session_location)
            except Exception:
                LOG.debug('Failed to delete Redfish session for host %s',
                          self.host_config.hostname, exc_info=True)
            self.session_location = None

    def validate_system_identity(self, system):
        checks = (
            ('expected_system_uuid', 'UUID'),
            ('expected_serial_number', 'SerialNumber'),
            ('expected_asset_tag', 'AssetTag'),
            ('expected_manufacturer', 'Manufacturer'),
            ('expected_model', 'Model'),
        )
        for expected_attr, redfish_name in checks:
            expected = getattr(self.host_config, expected_attr)
            if expected is None:
                continue
            actual = system.get(redfish_name)
            if actual != expected:
                raise exception.RedfishIdentityMismatch(
                    message='Redfish %s mismatch for host %s: expected %s, '
                            'got %s' % (redfish_name,
                                        self.host_config.hostname,
                                        expected, actual))

    def _reset_action(self, system):
        action = (system.get('Actions') or {}).get('#ComputerSystem.Reset')
        action = action or {}
        reset_type = self.conf.redfish_fencing.reset_type
        allowed = action.get('ResetType@Redfish.AllowableValues')
        if allowed is not None and reset_type not in allowed:
            raise exception.RedfishFencingException(
                message='Redfish host %s does not allow reset type %s' %
                        (self.host_config.hostname, reset_type))
        return action.get('target') or '%s/Actions/ComputerSystem.Reset' % (
            self.host_config.systems_uri.rstrip('/'))

    def _read_system(self):
        system = self._json('get', self.host_config.systems_uri)
        self.validate_system_identity(system)
        return system

    def _post_forceoff(self, target):
        reset_type = self.conf.redfish_fencing.reset_type
        self._request(
            'post', target, json={'ResetType': reset_type},
            headers={'Accept': 'application/json',
                     'Content-Type': 'application/json'})

    def _wait_for_power_off(self):
        expected = self.conf.redfish_fencing.expected_power_state
        required_reads = self.conf.redfish_fencing.stable_power_state_reads
        deadline = time.time() + self.conf.redfish_fencing.power_off_timeout
        stable_reads = 0
        last_system = None

        while time.time() <= deadline:
            last_system = self._read_system()
            if last_system.get('PowerState') == expected:
                stable_reads += 1
                if stable_reads >= required_reads:
                    return {
                        'method': 'redfish',
                        'systems_uri': self.host_config.systems_uri,
                        'verified_power_state': expected,
                        'power_state_reads': stable_reads,
                    }
                eventlet.sleep(0)
                continue

            stable_reads = 0
            eventlet.sleep(self.conf.redfish_fencing.poll_interval)

        raise exception.RedfishFencingException(
            message='Timed out waiting for Redfish PowerState=%s on host %s; '
                    'last system payload was %s' %
                    (expected, self.host_config.hostname, last_system))

    def _fence_once(self):
        self._authenticate()
        system = self._read_system()
        target = self._reset_action(system)
        reset_type = self.conf.redfish_fencing.reset_type

        if (system.get('PowerState') !=
                self.conf.redfish_fencing.expected_power_state):
            self._post_forceoff(target)

        proof = self._wait_for_power_off()
        proof.update({
            'reset_target': target,
            'requested_reset_type': reset_type,
        })
        return proof

    def fence_off(self):
        attempts = self.conf.redfish_fencing.max_attempts
        for attempt in range(1, attempts + 1):
            try:
                return self._fence_once()
            except exception.MasakariException:
                raise
            except requests.exceptions.RequestException as exc:
                if attempt >= attempts:
                    raise exception.RedfishFencingException(message=str(exc))
                eventlet.sleep(self.conf.redfish_fencing.retry_interval)
        raise exception.RedfishFencingException(
            message='Redfish fencing attempts exhausted for host %s' %
                    self.host_config.hostname)
