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


DIAGNOSTICS = 'os_masakari_api:diagnostics:%s'

rules = [
    policy.DocumentedRuleDefault(
        name=DIAGNOSTICS % 'index',
        check_str=base.RULE_ADMIN_API,
        description="Lists available Masakari diagnostics checks.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-diagnostics/checks'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=DIAGNOSTICS % 'run',
        check_str=base.RULE_ADMIN_API,
        description="Runs Masakari diagnostics checks.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-diagnostics/run'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=DIAGNOSTICS % 'detail',
        check_str=base.RULE_ADMIN_API,
        description="Shows a Masakari diagnostics job.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-diagnostics/jobs/{job_id}'
            }
        ]),
    policy.RuleDefault(
        name=DIAGNOSTICS % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Diagnostics API extension to change the API.",
        ),
]


def list_rules():
    return rules
