# Copyright (C) 2018 NTT DATA
# All Rights Reserved.
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


import itertools

from masakari.policies import admin_config
from masakari.policies import admin_overview
from masakari.policies import audit
from masakari.policies import base
from masakari.policies import diagnostics
from masakari.policies import extension_info
from masakari.policies import hosts
from masakari.policies import notifications
from masakari.policies import recovery_workflows
from masakari.policies import segments
from masakari.policies import staged_recovery
from masakari.policies import versions
from masakari.policies import vmoves


def list_rules():
    return itertools.chain(
        admin_config.list_rules(),
        admin_overview.list_rules(),
        audit.list_rules(),
        base.list_rules(),
        diagnostics.list_rules(),
        extension_info.list_rules(),
        hosts.list_rules(),
        notifications.list_rules(),
        recovery_workflows.list_rules(),
        segments.list_rules(),
        staged_recovery.list_rules(),
        versions.list_rules(),
        vmoves.list_rules()
    )
