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
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
from webob import exc

from masakari.api.openstack import extensions
from masakari.api.openstack import wsgi
import masakari.conf
from masakari.conf import staged_recovery as staged_recovery_conf
from masakari import db
from masakari.engine.drivers.taskflow import staged_state_etcd as staged_state
from masakari import exception
from masakari.policies import admin_config as admin_config_policies


CONF = masakari.conf.CONF
ALIAS = 'admin-config'

SECRET_NAME_PARTS = ('password', 'secret', 'token', 'key')
RUNTIME_MUTABLE_OPTIONS = {
    'staged_recovery': {'max_parallel_starts_per_host'},
}

SUPPORTED_GROUPS = {'staged_recovery'}


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


def _loads(value):
    if not value:
        return None
    return jsonutils.loads(value)


def _dumps(value):
    return jsonutils.dumps(value, sort_keys=True)


def _draft_from_body(body):
    draft = (body or {}).get('draft') or {}
    if not isinstance(draft, dict):
        raise exc.HTTPBadRequest(explanation='Draft must be an object.')
    return draft


def _apply_from_body(body):
    apply = (body or {}).get('apply') or {}
    if not isinstance(apply, dict):
        raise exc.HTTPBadRequest(explanation='Apply must be an object.')
    return apply


def _format_datetime(value):
    return value.isoformat() + 'Z' if value else None


def _staged_recovery_opts_by_name():
    return {opt.name: opt for opt in staged_recovery_conf.staged_recovery_opts}


def _runtime_store():
    return staged_state.EtcdStagedRecoveryStore(CONF, owner='api')


def _iter_change_items(changes):
    if isinstance(changes, dict):
        for group, group_changes in changes.items():
            if not isinstance(group_changes, dict):
                yield {
                    'group': group,
                    'error': {
                        'code': 'invalid_group_value',
                        'group': group,
                        'message': 'Group changes must be an object.',
                    },
                }
                continue
            for option, value in group_changes.items():
                yield {
                    'group': group,
                    'option': option,
                    'value': value,
                }
        return

    if isinstance(changes, list):
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                yield {
                    'error': {
                        'code': 'invalid_change',
                        'index': index,
                        'message': 'Change must be an object.',
                    },
                }
                continue
            group = change.get('group')
            option = change.get('option')
            if not group or not option:
                yield {
                    'error': {
                        'code': 'invalid_change',
                        'index': index,
                        'message': 'Change must include group and option.',
                    },
                }
                continue
            yield {
                'file': change.get('file'),
                'group': group,
                'option': option,
                'value': change.get('value'),
            }
        return

    yield {
        'error': {
            'code': 'invalid_changes',
            'message': 'Draft changes must be an object or a list.',
        },
    }


def _validate_option_value(group, opt, value):
    if isinstance(opt, cfg.BoolOpt) and not isinstance(value, bool):
        return {
            'code': 'invalid_type',
            'group': group,
            'option': opt.name,
            'message': 'Value must be a boolean.',
        }
    if isinstance(opt, cfg.IntOpt):
        if isinstance(value, bool) or not isinstance(value, int):
            return {
                'code': 'invalid_type',
                'group': group,
                'option': opt.name,
                'message': 'Value must be an integer.',
            }
        if getattr(opt, 'min', None) is not None and value < opt.min:
            return {
                'code': 'below_minimum',
                'group': group,
                'option': opt.name,
                'message': 'Value must be greater than or equal to %s.' %
                           opt.min,
            }
    if isinstance(opt, cfg.StrOpt) and value is not None and not isinstance(
            value, str):
        return {
            'code': 'invalid_type',
            'group': group,
            'option': opt.name,
            'message': 'Value must be a string.',
        }
    if getattr(opt, 'choices', None) and value not in list(opt.choices):
        return {
            'code': 'invalid_choice',
            'group': group,
            'option': opt.name,
            'message': 'Value must be one of %s.' % list(opt.choices),
        }
    return None


class AdminConfigController(wsgi.Controller):
    """Admin config metadata for Horizon."""

    def __init__(self):
        self.drafts = AdminConfigDraftsController(self)
        self.apply_jobs = AdminConfigApplyJobsController()

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
            if opt.name == 'max_parallel_starts_per_host':
                try:
                    runtime_value, source = (
                        _runtime_store().
                        get_max_parallel_starts_per_host_with_source(value))
                except exception.MasakariException:
                    runtime_value, source = value, 'config'
                values[opt.name] = {
                    'value': runtime_value,
                    'source': source,
                }
            else:
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


class AdminConfigDraftsController(wsgi.Controller):
    """Config draft lifecycle without apply/rollback."""

    def __init__(self, config_controller):
        self.config_controller = config_controller

    def _view(self, draft):
        result = {
            'uuid': draft['uuid'],
            'name': draft['name'],
            'status': draft['status'],
            'changes': _loads(draft['values']) or {},
            'comment': draft['comment'],
        }
        validation = _loads(draft['validation'])
        if validation is not None:
            result['validation'] = validation
        plan = _loads(draft['plan'])
        if plan is not None:
            result['plan'] = plan
        return result

    def _get(self, context, draft_id):
        try:
            return db.admin_config_draft_get_by_uuid(context, draft_id)
        except exception.ConfigDraftNotFound as err:
            raise exc.HTTPNotFound(explanation=err.format_message())

    def _changes_from_body(self, body):
        draft = _draft_from_body(body)
        return draft.get('changes', draft.get('values', {}))

    def _validate_changes(self, changes):
        errors = []
        warnings = []
        opts = _staged_recovery_opts_by_name()

        for change in _iter_change_items(changes):
            error = change.get('error')
            if error:
                errors.append(error)
                continue

            group = change['group']
            option = change['option']
            value = change['value']
            if group not in SUPPORTED_GROUPS:
                errors.append({
                    'code': 'unknown_group',
                    'group': group,
                    'message': 'Unsupported config group.',
                })
                continue
            opt = opts.get(option)
            if opt is None:
                errors.append({
                    'code': 'unknown_option',
                    'group': group,
                    'option': option,
                    'message': 'Unsupported config option.',
                })
                continue
            error = _validate_option_value(group, opt, value)
            if error is not None:
                errors.append(error)
                continue
            if option not in RUNTIME_MUTABLE_OPTIONS[group]:
                warnings.append({
                    'code': 'reconfigure_required',
                    'group': group,
                    'option': option,
                    'message': 'Changing this option requires deploy '
                               'backend reconfiguration.',
                })

        return {
            'status': 'invalid' if errors else 'valid',
            'errors': errors,
            'warnings': warnings,
        }

    def _effective_value(self, group, option):
        if group != 'staged_recovery':
            return None
        try:
            return getattr(CONF.staged_recovery, option)
        except cfg.NoSuchOptError:
            return None

    def _public_value(self, option, value):
        return _masked(value) if _is_secret_name(option) else value

    def _build_diff(self, changes):
        diff = []
        opts = _staged_recovery_opts_by_name()
        for change in _iter_change_items(changes):
            error = change.get('error')
            if error:
                diff.append({'valid': False, 'error': error})
                continue
            group = change['group']
            option = change['option']
            if group not in SUPPORTED_GROUPS or option not in opts:
                code = ('unknown_group' if group not in SUPPORTED_GROUPS
                        else 'unknown_option')
                diff.append({
                    'group': group,
                    'option': option,
                    'valid': False,
                    'error': {
                        'code': code,
                        'message': 'Unsupported config change.',
                    },
                })
                continue
            proposed = change['value']
            current = self._effective_value(group, option)
            item = {
                'group': group,
                'option': option,
                'current': self._public_value(option, current),
                'proposed': self._public_value(option, proposed),
                'changed': current != proposed,
                'valid': True,
            }
            if change.get('file'):
                item['file'] = change['file']
            diff.append(item)
        return {'changes': diff}

    def _build_plan(self, changes):
        steps = []
        validation = self._validate_changes(changes)
        if validation['errors']:
            return {
                'status': 'invalid',
                'steps': steps,
                'errors': validation['errors'],
                'apply_supported': False,
            }
        for change in _iter_change_items(changes):
            group = change['group']
            option = change['option']
            runtime = option in RUNTIME_MUTABLE_OPTIONS.get(group, set())
            step = {
                'group': group,
                'option': option,
                'action': ('runtime_update' if runtime
                           else 'reconfigure_required'),
            }
            if change.get('file'):
                step['file'] = change['file']
            steps.append(step)
        runtime_only = bool(steps) and all(
            step['action'] == 'runtime_update' for step in steps)
        return {
            'status': 'planned',
            'steps': steps,
            'apply_supported': runtime_only,
        }

    def _runtime_apply_supported(self, plan):
        return (
            plan.get('status') == 'planned' and
            bool(plan.get('steps')) and
            all(step['action'] == 'runtime_update'
                for step in plan.get('steps', [])))

    def _apply_runtime_changes(self, changes):
        updates = []
        store = _runtime_store()
        for change in _iter_change_items(changes):
            if change.get('error'):
                continue
            if (change['group'] == 'staged_recovery' and
                    change['option'] == 'max_parallel_starts_per_host'):
                value = store.set_max_parallel_starts_per_host(
                    change['value'])
                updates.append({
                    'group': change['group'],
                    'option': change['option'],
                    'value': value,
                    'source': 'runtime',
                })
        return updates

    def _apply_result(self, changes, plan):
        if self._runtime_apply_supported(plan):
            updates = self._apply_runtime_changes(changes)
            return {
                'status': 'succeeded',
                'backend': 'runtime_etcd',
                'changed': bool(updates),
                'runtime_updates': updates,
                'message': 'Runtime configuration was applied to etcd.',
            }
        return {
            'status': 'succeeded',
            'backend': 'noop',
            'changed': False,
            'message': ('No-op apply backend recorded the plan without '
                        'changing configuration.'),
        }

    def _apply_failure_result(self, plan, message):
        backend = ('runtime_etcd' if self._runtime_apply_supported(plan)
                   else 'noop')
        return {
            'status': 'failed',
            'backend': backend,
            'changed': False,
            'message': message,
        }

    def _apply_failure_errors(self, plan, message):
        code = ('runtime_apply_failed' if self._runtime_apply_supported(plan)
                else 'apply_failed')
        return [{'code': code, 'message': message}]

    def _record_apply_failure(self, context, job_uuid, plan, message):
        db.admin_config_apply_job_update(context, job_uuid, {
            'status': 'failed',
            'result': _dumps(self._apply_failure_result(plan, message)),
            'errors': _dumps(self._apply_failure_errors(plan, message)),
        })

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.BAD_REQUEST))
    def index(self, req):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'index')
        drafts = db.admin_config_draft_get_all(
            context, sort_keys=['id'], sort_dirs=['desc'])
        return {'drafts': [self._view(draft) for draft in drafts]}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'detail')
        return {'draft': self._view(self._get(context, id))}

    @wsgi.response(HTTPStatus.CREATED)
    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.BAD_REQUEST))
    def create(self, req, body):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'create')
        draft_body = _draft_from_body(body)
        changes = self._changes_from_body(body)
        draft = db.admin_config_draft_create(context, {
            'uuid': uuidutils.generate_uuid(),
            'name': draft_body.get('name'),
            'status': 'draft',
            'values': _dumps(changes),
            'comment': draft_body.get('comment'),
            'validation': None,
            'plan': None,
        })
        return {'draft': self._view(draft)}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND,
                                 HTTPStatus.BAD_REQUEST))
    def update(self, req, id, body):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'update')
        self._get(context, id)
        draft_body = _draft_from_body(body)
        values = {}
        if 'name' in draft_body:
            values['name'] = draft_body['name']
        if 'comment' in draft_body:
            values['comment'] = draft_body['comment']
        if 'changes' in draft_body or 'values' in draft_body:
            values['values'] = _dumps(self._changes_from_body(body))
            values['status'] = 'draft'
            values['validation'] = None
            values['plan'] = None
        draft = db.admin_config_draft_update(context, id, values)
        return {'draft': self._view(draft)}

    @wsgi.response(HTTPStatus.NO_CONTENT)
    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def delete(self, req, id):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'delete')
        try:
            db.admin_config_draft_delete(context, id)
        except exception.ConfigDraftNotFound as err:
            raise exc.HTTPNotFound(explanation=err.format_message())

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def validate(self, req, draft_id, body=None):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'validate')
        draft = self._get(context, draft_id)
        validation = self._validate_changes(_loads(draft['values']) or {})
        updated = db.admin_config_draft_update(context, draft_id, {
            'status': validation['status'],
            'validation': _dumps(validation),
        })
        return {
            'draft': self._view(updated),
            'validation': validation,
        }

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def diff(self, req, draft_id, body=None):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'diff')
        draft = self._get(context, draft_id)
        return {'diff': self._build_diff(_loads(draft['values']) or {})}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def plan(self, req, draft_id, body=None):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'plan')
        draft = self._get(context, draft_id)
        changes = _loads(draft['values']) or {}
        plan = self._build_plan(changes)
        db.admin_config_draft_update(context, draft_id, {'plan': _dumps(plan)})
        return {'plan': plan}

    @wsgi.response(HTTPStatus.ACCEPTED)
    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND,
                                 HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT))
    def apply(self, req, draft_id, body=None):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_DRAFTS % 'apply')
        draft = self._get(context, draft_id)
        if draft['status'] in ('applying', 'applied'):
            raise exc.HTTPConflict(explanation='Draft is already applying or '
                                   'applied.')

        changes = _loads(draft['values']) or {}
        validation = self._validate_changes(changes)
        if validation['errors']:
            db.admin_config_draft_update(context, draft_id, {
                'status': validation['status'],
                'validation': _dumps(validation),
            })
            raise exc.HTTPBadRequest(explanation='Draft validation failed.')

        apply_body = _apply_from_body(body)
        strategy = apply_body.get('strategy') or 'noop'
        if not isinstance(strategy, str):
            raise exc.HTTPBadRequest(explanation='Apply strategy must be a '
                                     'string.')
        canary = apply_body.get('canary', False)
        if not isinstance(canary, bool):
            raise exc.HTTPBadRequest(explanation='Apply canary must be a '
                                     'boolean.')

        plan = self._build_plan(changes)
        job = db.admin_config_apply_job_create(context, {
            'uuid': uuidutils.generate_uuid(),
            'draft_uuid': draft['uuid'],
            'status': 'queued',
            'strategy': strategy,
            'canary': canary,
            'comment': apply_body.get('comment'),
            'plan': _dumps(plan),
            'result': None,
            'errors': None,
        })
        try:
            result = self._apply_result(changes, plan)
        except exception.MasakariException as err:
            message = err.format_message()
            self._record_apply_failure(context, job['uuid'], plan, message)
            raise exc.HTTPBadRequest(explanation=message)
        except Exception as err:
            self._record_apply_failure(context, job['uuid'], plan, str(err))
            raise

        job = db.admin_config_apply_job_update(context, job['uuid'], {
            'status': 'succeeded',
            'result': _dumps(result),
            'errors': _dumps([]),
        })
        db.admin_config_draft_update(context, draft_id, {
            'status': 'applied',
            'validation': _dumps(validation),
            'plan': _dumps(plan),
        })
        return {'apply_job': AdminConfigApplyJobsController.view(job)}


class AdminConfigApplyJobsController(wsgi.Controller):
    """Config apply job lifecycle for Horizon."""

    @staticmethod
    def view(job):
        result = {
            'id': job['uuid'],
            'draft_id': job['draft_uuid'],
            'status': job['status'],
            'strategy': job['strategy'],
            'canary': job['canary'],
            'comment': job['comment'],
            'created_at': _format_datetime(job['created_at']),
            'updated_at': _format_datetime(job['updated_at']),
            'errors': _loads(job['errors']) or [],
        }
        plan = _loads(job['plan'])
        if plan is not None:
            result['plan'] = plan
        apply_result = _loads(job['result'])
        if apply_result is not None:
            result['result'] = apply_result
        return result

    def _get(self, context, job_id):
        try:
            return db.admin_config_apply_job_get_by_uuid(context, job_id)
        except exception.ConfigApplyJobNotFound as err:
            raise exc.HTTPNotFound(explanation=err.format_message())

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.BAD_REQUEST))
    def index(self, req):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_APPLY_JOBS % 'index')
        filters = {}
        if 'status' in req.params:
            filters['status'] = req.params['status']
        if 'draft_id' in req.params:
            filters['draft_uuid'] = req.params['draft_id']
        jobs = db.admin_config_apply_job_get_all(
            context, filters=filters, sort_keys=['id'], sort_dirs=['desc'])
        return {'apply_jobs': [self.view(job) for job in jobs]}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND))
    def show(self, req, id):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_APPLY_JOBS % 'detail')
        return {'apply_job': self.view(self._get(context, id))}

    @extensions.expected_errors((HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND,
                                 HTTPStatus.CONFLICT))
    def rollback(self, req, job_id, body=None):
        context = req.environ['masakari.context']
        context.can(admin_config_policies.ADMIN_CONFIG_APPLY_JOBS % 'rollback')
        self._get(context, job_id)
        raise exc.HTTPConflict(explanation='Rollback is not supported by the '
                               'no-op apply backend.')


class AdminConfig(extensions.V1APIExtensionBase):
    """Admin config schema and effective values."""

    name = "AdminConfig"
    alias = ALIAS
    version = 1

    def get_resources(self):
        controller = AdminConfigController()
        return [
            extensions.ResourceExtension(ALIAS,
                                         controller,
                                         member_name='admin_config'),
            extensions.ResourceExtension(
                'admin-config-drafts',
                controller.drafts,
                member_name='admin_config_draft',
                custom_routes_fn=self.draft_custom_routes),
            extensions.ResourceExtension(
                'admin-config-apply-jobs',
                controller.apply_jobs,
                member_name='admin_config_apply_job',
                custom_routes_fn=self.apply_job_custom_routes),
        ]

    def get_controller_extensions(self):
        return []

    @staticmethod
    def draft_custom_routes(mapper, wsgi_resource):
        mapper.connect('admin-config-drafts-patch',
                       '/admin-config-drafts/{id}',
                       controller=wsgi_resource, action='update',
                       conditions={'method': ['PATCH']})
        mapper.connect('admin-config-drafts-validate',
                       '/admin-config-drafts/{draft_id}/validate',
                       controller=wsgi_resource, action='validate',
                       conditions={'method': ['POST']})
        mapper.connect('admin-config-drafts-diff',
                       '/admin-config-drafts/{draft_id}/diff',
                       controller=wsgi_resource, action='diff',
                       conditions={'method': ['GET']})
        mapper.connect('admin-config-drafts-plan',
                       '/admin-config-drafts/{draft_id}/plan',
                       controller=wsgi_resource, action='plan',
                       conditions={'method': ['POST']})
        mapper.connect('admin-config-drafts-apply',
                       '/admin-config-drafts/{draft_id}/apply',
                       controller=wsgi_resource, action='apply',
                       conditions={'method': ['POST']})

    @staticmethod
    def apply_job_custom_routes(mapper, wsgi_resource):
        mapper.connect('admin-config-apply-jobs-rollback',
                       '/admin-config-apply-jobs/{job_id}/rollback',
                       controller=wsgi_resource, action='rollback',
                       conditions={'method': ['POST']})
