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

"""Admin config API extension."""

from http import HTTPStatus

from oslo_config import cfg
from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
import masakari.conf
from masakari.conf import staged_recovery as staged_recovery_conf
from masakari.policies import admin_config as admin_config_policies


CONF = masakari.conf.CONF
ALIAS = 'admin-config'

SECRET_NAME_PARTS = ('password', 'secret', 'token', 'key')
RUNTIME_MUTABLE_OPTIONS = {
    'staged_recovery': {'max_parallel_starts_per_host'},
}


def _option_type(opt):
    if isinstance(opt, cfg.BoolOpt):
        return 'bool'
    if isinstance(opt, cfg.IntOpt):
        return 'int'
    if isinstance(opt, cfg.StrOpt):
        return 'string'
    return opt.__class__.__name__


def _is_secret_name(name):
    return any(part in name for part in SECRET_NAME_PARTS)


def _masked(value):
    return {
        'masked': True,
        'configured': value is not None and value != '',
    }


class AdminConfigController(wsgi.Controller):
    """Read-only admin config metadata for Horizon."""

    def _staged_recovery_schema(self):
        options = []
        runtime_mutable = RUNTIME_MUTABLE_OPTIONS['staged_recovery']
        for opt in staged_recovery_conf.staged_recovery_opts:
            item = {
                'name': opt.name,
                'type': _option_type(opt),
                'default': opt.default,
                'mutable': opt.name in runtime_mutable,
                'deploy_stage': ('runtime' if opt.name in runtime_mutable
                                 else 'reconfigure'),
                'secret': _is_secret_name(opt.name),
                'help': opt.help,
            }
            if getattr(opt, 'choices', None):
                item['choices'] = list(opt.choices)
            if getattr(opt, 'min', None) is not None:
                item['minimum'] = opt.min
            options.append(item)

        return {'name': 'staged_recovery', 'options': options}

    def _staged_recovery_effective(self):
        values = {}
        for opt in staged_recovery_conf.staged_recovery_opts:
            value = getattr(CONF.staged_recovery, opt.name)
            values[opt.name] = _masked(value) if _is_secret_name(
                opt.name) else value
        return values

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']

        if id == 'schema':
            context.can(admin_config_policies.ADMIN_CONFIG % 'schema')
            return {'schema': {'groups': [self._staged_recovery_schema()]}}

        if id == 'effective':
            context.can(admin_config_policies.ADMIN_CONFIG % 'effective')
            group = req.params.get('group')
            if group and group != 'staged_recovery':
                raise exc.HTTPNotFound()
            return {'config': {
                'staged_recovery': self._staged_recovery_effective()}}

        raise exc.HTTPNotFound()


class AdminConfig(extensions.V1APIExtensionBase):
    """Admin config schema and effective values."""

    name = "AdminConfig"
    alias = ALIAS
    version = 1

    def get_resources(self):
        return [
            extensions.ResourceExtension(ALIAS,
                                         AdminConfigController(),
                                         member_name='admin_config')
        ]

    def get_controller_extensions(self):
        return []
