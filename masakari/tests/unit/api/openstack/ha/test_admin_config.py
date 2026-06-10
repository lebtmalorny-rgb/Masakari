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

from http import HTTPStatus
from unittest import mock

from oslo_serialization import jsonutils
from webob import exc

from masakari.api.openstack.ha import admin_config
from masakari import test
from masakari.tests.unit.api.openstack import fakes
from masakari.tests import uuidsentinel


class AdminConfigTestCase(test.TestCase):

    def setUp(self):
        super(AdminConfigTestCase, self).setUp()
        self.controller = admin_config.AdminConfigController()
        self.req = fakes.HTTPRequest.blank('/v1/admin-config/schema',
                                           use_admin_context=True)

    @property
    def app(self):
        return fakes.wsgi_app_v1(
            fake_auth_context=fakes.FakeRequestContext(
                user_id=uuidsentinel.fake_user_id,
                project_id=uuidsentinel.fake_project_id,
                is_admin=True))

    def test_show_schema_returns_staged_recovery_options(self):
        result = self.controller.show(self.req, 'schema')

        groups = {group['name']: group for group in result['schema']['groups']}
        self.assertIn('staged_recovery', groups)
        option_names = [opt['name'] for opt in groups[
            'staged_recovery']['options']]
        self.assertIn('max_parallel_starts_per_host', option_names)
        self.assertIn('start_only_originally_active', option_names)

    def test_show_effective_masks_secret_like_values(self):
        self.override_config('max_parallel_starts_per_host', 5,
                             group='staged_recovery')
        self.override_config('etcd_key_file', '/etc/masakari/key.pem',
                             group='staged_recovery')

        result = self.controller.show(self.req, 'effective')

        staged = result['config']['staged_recovery']
        self.assertEqual(5, staged['max_parallel_starts_per_host'])
        self.assertEqual({'masked': True, 'configured': True},
                         staged['etcd_key_file'])

    def test_show_rejects_unknown_resource(self):
        self.assertRaises(exc.HTTPNotFound, self.controller.show, self.req,
                          'unknown')

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_schema_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config/schema',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('staged_recovery',
                      [group['name'] for group in body['schema']['groups']])

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_effective_route(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config/effective',
                                      use_admin_context=True)

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, response.status_code)
        body = jsonutils.loads(response.body)
        self.assertIn('staged_recovery', body['config'])

    def test_create_draft_stores_changes(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)

        result = self.controller.drafts.create(req, body={
            'draft': {
                'name': 'staged-start-limit',
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                },
                'comment': 'raise runtime start limit'
            }
        })

        self.assertEqual('staged-start-limit', result['draft']['name'])
        self.assertEqual('draft', result['draft']['status'])
        self.assertEqual(4, result['draft']['changes']['staged_recovery'][
            'max_parallel_starts_per_host'])

    def test_create_draft_rejects_non_object_draft_body(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)

        self.assertRaises(exc.HTTPBadRequest,
                          self.controller.drafts.create, req,
                          body={'draft': 'bad'})

    def test_validate_draft_marks_invalid_option(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'does_not_exist': 1
                    }
                }
            }
        })

        result = self.controller.drafts.validate(
            req, created['draft']['uuid'])

        self.assertEqual('invalid', result['validation']['status'])
        self.assertEqual('unknown_option',
                         result['validation']['errors'][0]['code'])

    def test_validate_draft_accepts_list_changes(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': [
                    {
                        'file': 'masakari.conf',
                        'group': 'staged_recovery',
                        'option': 'max_parallel_starts_per_host',
                        'value': 4,
                    }
                ]
            }
        })

        validation = self.controller.drafts.validate(
            req, created['draft']['uuid'])
        diff = self.controller.drafts.diff(req, created['draft']['uuid'])
        plan = self.controller.drafts.plan(req, created['draft']['uuid'])

        self.assertEqual('valid', validation['validation']['status'])
        self.assertEqual('masakari.conf',
                         diff['diff']['changes'][0]['file'])
        self.assertEqual('masakari.conf', plan['plan']['steps'][0]['file'])

    def test_validate_draft_reports_malformed_changes(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': 'not-a-change-set'
            }
        })

        validation = self.controller.drafts.validate(
            req, created['draft']['uuid'])
        diff = self.controller.drafts.diff(req, created['draft']['uuid'])
        plan = self.controller.drafts.plan(req, created['draft']['uuid'])

        self.assertEqual('invalid', validation['validation']['status'])
        self.assertEqual('invalid_changes',
                         validation['validation']['errors'][0]['code'])
        self.assertFalse(diff['diff']['changes'][0]['valid'])
        self.assertEqual('invalid', plan['plan']['status'])

    def test_update_draft_replaces_changes_and_clears_cached_results(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'does_not_exist': 1
                    }
                }
            }
        })
        self.controller.drafts.validate(req, created['draft']['uuid'])

        updated = self.controller.drafts.update(req, created['draft']['uuid'],
                                                body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                }
            }
        })

        self.assertEqual('draft', updated['draft']['status'])
        self.assertNotIn('validation', updated['draft'])
        self.assertEqual(4, updated['draft']['changes']['staged_recovery'][
            'max_parallel_starts_per_host'])

    def test_diff_draft_masks_secret_values(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        self.override_config('etcd_key_file', '/etc/masakari/old.pem',
                             group='staged_recovery')
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'etcd_key_file': '/etc/masakari/new.pem'
                    }
                }
            }
        })

        result = self.controller.drafts.diff(req, created['draft']['uuid'])

        change = result['diff']['changes'][0]
        self.assertEqual('etcd_key_file', change['option'])
        self.assertEqual({'masked': True, 'configured': True},
                         change['current'])
        self.assertEqual({'masked': True, 'configured': True},
                         change['proposed'])

    def test_plan_draft_marks_runtime_and_reconfigure_steps(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4,
                        'batch_delay': 20
                    }
                }
            }
        })

        result = self.controller.drafts.plan(req, created['draft']['uuid'])

        actions = {(step['group'], step['option']): step['action']
                   for step in result['plan']['steps']}
        self.assertEqual('runtime_update', actions[
            ('staged_recovery', 'max_parallel_starts_per_host')])
        self.assertEqual('reconfigure_required', actions[
            ('staged_recovery', 'batch_delay')])

    def test_apply_draft_creates_succeeded_noop_job(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                }
            }
        })

        result = self.controller.drafts.apply(req, created['draft']['uuid'],
                                              body={
            'apply': {
                'strategy': 'rolling',
                'canary': True,
                'comment': 'dry run from Horizon'
            }
        })

        job = result['apply_job']
        self.assertEqual(created['draft']['uuid'], job['draft_id'])
        self.assertEqual('succeeded', job['status'])
        self.assertEqual('rolling', job['strategy'])
        self.assertTrue(job['canary'])
        self.assertEqual('noop', job['result']['backend'])
        self.assertFalse(job['result']['changed'])

        stored = self.controller.apply_jobs.show(req, job['id'])
        self.assertEqual(job['id'], stored['apply_job']['id'])
        draft = self.controller.drafts.show(req, created['draft']['uuid'])
        self.assertEqual('applied', draft['draft']['status'])

    def test_apply_draft_rejects_invalid_changes(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'does_not_exist': 1
                    }
                }
            }
        })

        self.assertRaises(exc.HTTPBadRequest, self.controller.drafts.apply,
                          req, created['draft']['uuid'], body={'apply': {}})

    def test_apply_jobs_index_and_rollback_unsupported(self):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        created = self.controller.drafts.create(req, body={
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                }
            }
        })
        applied = self.controller.drafts.apply(
            req, created['draft']['uuid'], body={'apply': {}})

        index = self.controller.apply_jobs.index(req)

        self.assertEqual(1, len(index['apply_jobs']))
        self.assertEqual(applied['apply_job']['id'],
                         index['apply_jobs'][0]['id'])
        self.assertRaises(exc.HTTPConflict,
                          self.controller.apply_jobs.rollback, req,
                          applied['apply_job']['id'], body={'rollback': {}})

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_draft_routes(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        req.method = 'POST'
        req.headers['Content-Type'] = 'application/json'
        req.body = jsonutils.dump_as_bytes({
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                }
            }
        })

        response = req.get_response(self.app)

        self.assertEqual(HTTPStatus.CREATED, response.status_code)
        body = jsonutils.loads(response.body)
        draft_uuid = body['draft']['uuid']

        validate_req = fakes.HTTPRequest.blank(
            '/v1/admin-config-drafts/%s/validate' % draft_uuid,
            use_admin_context=True)
        validate_req.method = 'POST'
        validate_req.headers['Content-Type'] = 'application/json'
        validate_req.body = jsonutils.dump_as_bytes({})
        validate_response = validate_req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, validate_response.status_code)

    @mock.patch('masakari.ha.api.NotificationAPI')
    def test_apply_routes(self, mock_notification_api):
        req = fakes.HTTPRequest.blank('/v1/admin-config-drafts',
                                      use_admin_context=True)
        req.method = 'POST'
        req.headers['Content-Type'] = 'application/json'
        req.body = jsonutils.dump_as_bytes({
            'draft': {
                'changes': {
                    'staged_recovery': {
                        'max_parallel_starts_per_host': 4
                    }
                }
            }
        })
        response = req.get_response(self.app)
        draft_uuid = jsonutils.loads(response.body)['draft']['uuid']

        apply_req = fakes.HTTPRequest.blank(
            '/v1/admin-config-drafts/%s/apply' % draft_uuid,
            use_admin_context=True)
        apply_req.method = 'POST'
        apply_req.headers['Content-Type'] = 'application/json'
        apply_req.body = jsonutils.dump_as_bytes({
            'apply': {'strategy': 'rolling'}
        })
        apply_response = apply_req.get_response(self.app)

        self.assertEqual(HTTPStatus.ACCEPTED, apply_response.status_code)
        apply_body = jsonutils.loads(apply_response.body)
        job_id = apply_body['apply_job']['id']

        show_req = fakes.HTTPRequest.blank(
            '/v1/admin-config-apply-jobs/%s' % job_id,
            use_admin_context=True)
        show_response = show_req.get_response(self.app)

        self.assertEqual(HTTPStatus.OK, show_response.status_code)
        show_body = jsonutils.loads(show_response.body)
        self.assertEqual('succeeded', show_body['apply_job']['status'])
