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


ADMIN_OVERVIEW = 'os_masakari_api:admin-overview:%s'

rules = [
    policy.DocumentedRuleDefault(
        name=ADMIN_OVERVIEW % 'index',
        check_str=base.RULE_ADMIN_API,
        description="Shows the Masakari admin overview summary.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-overview'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_OVERVIEW % 'health',
        check_str=base.RULE_ADMIN_API,
        description="Shows the Masakari admin health summary.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-overview/health'
            }
        ]),
    policy.RuleDefault(
        name=ADMIN_OVERVIEW % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Admin overview API extension to change the API.",
        ),
]


def list_rules():
    return rules
