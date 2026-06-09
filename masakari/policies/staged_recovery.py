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

from oslo_policy import policy

from masakari.policies import base


STAGED_RECOVERY = 'os_masakari_api:staged-recovery:%s'

rules = [
    policy.DocumentedRuleDefault(
        name=STAGED_RECOVERY % 'start_limit:show',
        check_str=base.RULE_ADMIN_API,
        description="Shows the staged recovery runtime start limit.",
        operations=[
            {
                'method': 'GET',
                'path': '/staged-recovery/start-limit'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=STAGED_RECOVERY % 'start_limit:update',
        check_str=base.RULE_ADMIN_API,
        description="Updates the staged recovery runtime start limit.",
        operations=[
            {
                'method': 'PUT',
                'path': '/staged-recovery/start-limit'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=STAGED_RECOVERY % 'start_limit:delete',
        check_str=base.RULE_ADMIN_API,
        description="Clears the staged recovery runtime start limit.",
        operations=[
            {
                'method': 'DELETE',
                'path': '/staged-recovery/start-limit'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=STAGED_RECOVERY % 'instances:index',
        check_str=base.RULE_ADMIN_API,
        description="Lists staged recovery instance states.",
        operations=[
            {
                'method': 'GET',
                'path': '/staged-recovery/instances'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=STAGED_RECOVERY % 'leases:index',
        check_str=base.RULE_ADMIN_API,
        description="Lists staged recovery start leases.",
        operations=[
            {
                'method': 'GET',
                'path': '/staged-recovery/leases'
            }
        ]),
    policy.RuleDefault(
        name=STAGED_RECOVERY % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Staged recovery API extension to change the API.",
        ),
]


def list_rules():
    return rules
