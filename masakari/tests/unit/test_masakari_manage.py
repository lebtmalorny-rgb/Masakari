# Copyright 2017 NTT DATA
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

import sys
from unittest import mock

from masakari.cmd import manage
from masakari import context
from masakari.db import api as db_api
from masakari import test


class DBCommandsTestCase(test.TestCase):

    def setUp(self):
        super(DBCommandsTestCase, self).setUp()
        self.commands = manage.DbCommands()
        self.context = context.get_admin_context()
        sys.argv = ['masakari-manage']

    @mock.patch.object(db_api, 'purge_deleted_rows')
    @mock.patch.object(context, 'get_admin_context')
    def test_purge_command(self, mock_context, mock_db_purge):
        mock_context.return_value = self.context
        self.commands.purge(0, 100)
        mock_db_purge.assert_called_once_with(self.context, 0, 100)

    def test_purge_negative_age_in_days(self):
        ex = self.assertRaises(SystemExit, self.commands.purge, -1, 100)
        self.assertEqual("Must supply a non-negative value for age.", ex.code)

    def test_purge_invalid_age_in_days(self):
        ex = self.assertRaises(SystemExit, self.commands.purge, "test", 100)
        self.assertEqual("Invalid value for age, test", ex.code)

    def test_purge_command_exceeded_age_in_days(self):
        ex = self.assertRaises(SystemExit, self.commands.purge, 1000000, 50)
        self.assertEqual("Maximal age is count of days since epoch.", ex.code)

    def test_purge_invalid_max_rows(self):
        ex = self.assertRaises(SystemExit, self.commands.purge, 0, 0)
        self.assertEqual("Must supply value greater than 0 for max_rows.",
                         ex.code)

    def test_purge_negative_max_rows(self):
        ex = self.assertRaises(SystemExit, self.commands.purge, 0, -5)
        self.assertEqual("Invalid input received: max_rows must be >= -1",
                         ex.code)

    @mock.patch.object(db_api, 'purge_deleted_rows')
    @mock.patch.object(context, 'get_admin_context')
    def test_purge_max_rows(self, mock_context, mock_db_purge):
        mock_context.return_value = self.context
        value = (2 ** 31) - 1
        self.commands.purge(age_in_days=1, max_rows=value)
        mock_db_purge.assert_called_once_with(self.context, 1, value)

    def test_purge_command_exceeded_maximum_rows(self):
        # value(2 ** 31) is greater than max_rows(2147483647) by 1.
        value = 2 ** 31
        ex = self.assertRaises(SystemExit, self.commands.purge, age_in_days=1,
                               max_rows=value)
        expected = "Invalid input received: max_rows must be <= 2147483647"
        self.assertEqual(expected, ex.code)


class StagedRecoveryCommandsTestCase(test.TestCase):

    def setUp(self):
        super(StagedRecoveryCommandsTestCase, self).setUp()
        self.commands = manage.StagedRecoveryCommands()

    @mock.patch('builtins.print')
    @mock.patch('masakari.cmd.manage.staged_state.EtcdStagedRecoveryStore')
    def test_get_start_limit_prints_effective_limit(self, mock_store_cls,
                                                    mock_print):
        store = mock_store_cls.return_value
        store.get_max_parallel_starts_per_host.return_value = 3

        self.commands.get_start_limit()

        store.get_max_parallel_starts_per_host.assert_called_once_with(
            default=2)
        mock_print.assert_called_once_with('3')

    @mock.patch('builtins.print')
    @mock.patch('masakari.cmd.manage.staged_state.EtcdStagedRecoveryStore')
    def test_set_start_limit_updates_runtime_limit(self, mock_store_cls,
                                                   mock_print):
        store = mock_store_cls.return_value
        store.set_max_parallel_starts_per_host.return_value = 4

        self.commands.set_start_limit(4)

        store.set_max_parallel_starts_per_host.assert_called_once_with(4)
        mock_print.assert_called_once_with('4')

    @mock.patch('builtins.print')
    @mock.patch('masakari.cmd.manage.staged_state.EtcdStagedRecoveryStore')
    def test_clear_start_limit_removes_runtime_limit(self, mock_store_cls,
                                                     mock_print):
        self.commands.clear_start_limit()

        store = mock_store_cls.return_value
        store.clear_max_parallel_starts_per_host.assert_called_once_with()
        mock_print.assert_called_once_with(
            'Runtime staged recovery start limit cleared.')

    def test_staged_recovery_category_is_registered(self):
        self.assertIs(manage.StagedRecoveryCommands,
                      manage.CATEGORIES['staged_recovery'])
