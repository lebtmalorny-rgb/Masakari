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

from oslo_config import cfg


redfish_fencing_group = cfg.OptGroup(
    'redfish_fencing',
    title='Redfish fencing options',
    help='Options for fencing failed compute hosts before evacuation.',
)

redfish_fencing_opts = [
    cfg.BoolOpt(
        'enabled',
        default=False,
        help='Enable Redfish fencing tasks for host failure recovery.'),
    cfg.StrOpt(
        'hosts_config_path',
        default='/etc/masakari/redfish-fencing-hosts.yaml',
        help='YAML mapping from Nova compute host names to Redfish BMC '
             'endpoints.'),
    cfg.BoolOpt(
        'allow_insecure_inline_password',
        default=False,
        help='Allow BMC passwords to be stored inline in the hosts mapping. '
             'Use only for lab or test environments.'),
    cfg.StrOpt(
        'etcd_backend_url',
        default=None,
        help='etcd3 gateway URL for Redfish fencing state. If empty, reuse '
             '[staged_recovery] etcd_backend_url or [coordination] '
             'backend_url.'),
    cfg.StrOpt(
        'etcd_prefix',
        default='/masakari/redfish-fencing/v1',
        help='Key prefix for Redfish fencing proof and locks in etcd.'),
    cfg.IntOpt(
        'etcd_timeout',
        default=5,
        min=1,
        help='Timeout in seconds for etcd KV requests.'),
    cfg.StrOpt(
        'reset_type',
        default='ForceOff',
        choices=['ForceOff'],
        help='Redfish reset type used for split-brain fencing.'),
    cfg.StrOpt(
        'expected_power_state',
        default='Off',
        choices=['Off'],
        help='Power state required before evacuation may continue.'),
    cfg.IntOpt(
        'power_off_timeout',
        default=180,
        min=1,
        help='Maximum time to wait for Redfish PowerState=Off.'),
    cfg.IntOpt(
        'poll_interval',
        default=5,
        min=1,
        help='Delay between Redfish power-state polling attempts.'),
    cfg.IntOpt(
        'stable_power_state_reads',
        default=2,
        min=1,
        help='Number of consecutive PowerState=Off reads required.'),
    cfg.IntOpt(
        'connect_timeout',
        default=5,
        min=1,
        help='Redfish TCP/TLS connect timeout.'),
    cfg.IntOpt(
        'read_timeout',
        default=30,
        min=1,
        help='Redfish HTTP read timeout.'),
    cfg.IntOpt(
        'max_attempts',
        default=3,
        min=1,
        help='Maximum Redfish fencing attempts per host.'),
    cfg.IntOpt(
        'retry_interval',
        default=5,
        min=0,
        help='Delay between Redfish fencing attempts.'),
    cfg.IntOpt(
        'lock_ttl',
        default=300,
        min=1,
        help='TTL for segment and host fencing locks in etcd.'),
    cfg.IntOpt(
        'multi_host_batch_window',
        default=30,
        min=0,
        help='Seconds to group host failure events in the same segment. '
             'Zero groups only events with the same event id.'),
    cfg.IntOpt(
        'max_auto_fence_hosts_per_segment',
        default=1,
        min=1,
        help='Maximum failed hosts that may be fenced automatically for one '
             'segment recovery event.'),
    cfg.IntOpt(
        'min_surviving_compute_hosts',
        default=1,
        min=0,
        help='Minimum non-failed compute hosts that must remain in the '
             'segment before automatic fencing is allowed.'),
]


def register_opts(conf):
    conf.register_group(redfish_fencing_group)
    conf.register_opts(redfish_fencing_opts, group=redfish_fencing_group)


def list_opts():
    return {
        redfish_fencing_group.name: redfish_fencing_opts,
    }
