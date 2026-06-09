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


staged_recovery_group = cfg.OptGroup(
    'staged_recovery',
    title='Staged recovery options',
    help='Options for staged VM start after host failure recovery.',
)

staged_recovery_opts = [
    cfg.BoolOpt(
        'enabled',
        default=False,
        help='Enable staged VM start workflow for host failure recovery.'),
    cfg.StrOpt(
        'state_backend',
        default='etcd',
        choices=['etcd'],
        help='Persistent state backend for staged recovery.'),
    cfg.StrOpt(
        'etcd_backend_url',
        default=None,
        help='etcd3 gateway URL for staged recovery state. If empty, reuse '
             '[coordination] backend_url when it uses etcd3+http(s).'),
    cfg.StrOpt(
        'etcd_prefix',
        default='/masakari/staged-recovery/v1',
        help='Key prefix for staged recovery state in etcd.'),
    cfg.IntOpt(
        'etcd_timeout',
        default=5,
        min=1,
        help='Timeout in seconds for etcd KV requests.'),
    cfg.StrOpt(
        'etcd_ca_cert',
        default=None,
        help='CA certificate for direct etcd KV client.'),
    cfg.StrOpt(
        'etcd_cert_file',
        default=None,
        help='Client certificate for direct etcd KV client.'),
    cfg.StrOpt(
        'etcd_key_file',
        default=None,
        help='Client private key for direct etcd KV client.'),
    cfg.IntOpt(
        'max_parallel_starts_per_host',
        default=2,
        min=1,
        help='Maximum number of simultaneously starting VMs per destination '
             'host.'),
    cfg.IntOpt(
        'start_timeout',
        default=900,
        min=60,
        help='Maximum time to wait until instance becomes ACTIVE.'),
    cfg.IntOpt(
        'batch_delay',
        default=30,
        min=0,
        help='Additional delay after VM becomes ACTIVE before releasing '
             'slot.'),
    cfg.IntOpt(
        'slot_lease_ttl',
        default=990,
        min=60,
        help='TTL for per-host start slot lease. It should cover '
             'start_timeout plus batch_delay and a safety margin.'),
    cfg.IntOpt(
        'slot_retry_interval',
        default=5,
        min=1,
        help='Delay between attempts to acquire a start slot.'),
    cfg.StrOpt(
        'nova_evacuate_microversion',
        default='2.95',
        help='Nova microversion used for evacuate-to-stopped.'),
    cfg.BoolOpt(
        'start_only_originally_active',
        default=True,
        help='Only start instances whose original vm_state was active.'),
    cfg.IntOpt(
        'reconcile_interval',
        default=60,
        min=10,
        help='Periodic interval for stale staged recovery reconciliation.'),
    cfg.IntOpt(
        'stale_recovery_timeout',
        default=300,
        min=60,
        help='Recovery is stale if updated_at is older than this timeout.'),
]


def register_opts(conf):
    conf.register_group(staged_recovery_group)
    conf.register_opts(staged_recovery_opts, group=staged_recovery_group)


def list_opts():
    return {
        staged_recovery_group.name: staged_recovery_opts,
    }
