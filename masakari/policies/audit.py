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


AUDIT = 'os_masakari_api:audit:%s'

rules = [
    policy.DocumentedRuleDefault(
        name=AUDIT % 'index',
        check_str=base.RULE_ADMIN_API,
        description="Lists Masakari admin audit events.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-audit/events'
            }
        ]),
    policy.RuleDefault(
        name=AUDIT % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Audit API extension to change the API.",
        ),
]


def list_rules():
    return rules
