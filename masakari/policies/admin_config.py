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


ADMIN_CONFIG = 'os_masakari_api:admin-config:%s'
ADMIN_CONFIG_DRAFTS = 'os_masakari_api:admin-config-drafts:%s'
ADMIN_CONFIG_APPLY_JOBS = 'os_masakari_api:admin-config-apply-jobs:%s'

rules = [
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG % 'schema',
        check_str=base.RULE_ADMIN_API,
        description="Shows the Masakari admin config schema.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config/schema'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG % 'effective',
        check_str=base.RULE_ADMIN_API,
        description="Shows the masked effective Masakari admin config.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config/effective'
            }
        ]),
    policy.RuleDefault(
        name=ADMIN_CONFIG % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Admin config API extension to change the API.",
        ),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'index',
        check_str=base.RULE_ADMIN_API,
        description="Lists Masakari admin config drafts.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config-drafts'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'detail',
        check_str=base.RULE_ADMIN_API,
        description="Shows a Masakari admin config draft.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config-drafts/{draft_id}'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'create',
        check_str=base.RULE_ADMIN_API,
        description="Creates a Masakari admin config draft.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-config-drafts'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'update',
        check_str=base.RULE_ADMIN_API,
        description="Updates a Masakari admin config draft.",
        operations=[
            {
                'method': 'PATCH',
                'path': '/admin-config-drafts/{draft_id}'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'delete',
        check_str=base.RULE_ADMIN_API,
        description="Deletes a Masakari admin config draft.",
        operations=[
            {
                'method': 'DELETE',
                'path': '/admin-config-drafts/{draft_id}'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'validate',
        check_str=base.RULE_ADMIN_API,
        description="Validates a Masakari admin config draft.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-config-drafts/{draft_id}/validate'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'diff',
        check_str=base.RULE_ADMIN_API,
        description="Shows the diff for a Masakari admin config draft.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config-drafts/{draft_id}/diff'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'plan',
        check_str=base.RULE_ADMIN_API,
        description="Builds an apply plan for a Masakari admin config draft.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-config-drafts/{draft_id}/plan'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'apply',
        check_str=base.RULE_ADMIN_API,
        description="Starts a Masakari admin config draft apply job.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-config-drafts/{draft_id}/apply'
            }
        ]),
    policy.RuleDefault(
        name=ADMIN_CONFIG_DRAFTS % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Admin config drafts API extension to change the API.",
        ),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_APPLY_JOBS % 'index',
        check_str=base.RULE_ADMIN_API,
        description="Lists Masakari admin config apply jobs.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config-apply-jobs'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_APPLY_JOBS % 'detail',
        check_str=base.RULE_ADMIN_API,
        description="Shows a Masakari admin config apply job.",
        operations=[
            {
                'method': 'GET',
                'path': '/admin-config-apply-jobs/{job_id}'
            }
        ]),
    policy.DocumentedRuleDefault(
        name=ADMIN_CONFIG_APPLY_JOBS % 'rollback',
        check_str=base.RULE_ADMIN_API,
        description="Requests rollback for a Masakari admin config apply job.",
        operations=[
            {
                'method': 'POST',
                'path': '/admin-config-apply-jobs/{job_id}/rollback'
            }
        ]),
    policy.RuleDefault(
        name=ADMIN_CONFIG_APPLY_JOBS % 'discoverable',
        check_str=base.RULE_ADMIN_API,
        description="Admin config apply jobs API extension to change the API.",
        ),
]


def list_rules():
    return rules
