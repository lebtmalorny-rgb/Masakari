# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Add admin config drafts

Revision ID: 9f33c2d21f7a
Revises: 8bdf5929c5a6
Create Date: 2026-06-10 12:20:00.000000
"""

from alembic import op
from oslo_db.sqlalchemy import types as oslo_db_types
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9f33c2d21f7a'
down_revision = '8bdf5929c5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_config_drafts',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'deleted',
            oslo_db_types.SoftDeleteInteger(),
            nullable=True,
        ),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('values', sa.Text(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('validation', sa.Text(), nullable=True),
        sa.Column('plan', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('uuid', name='uniq_admin_config_draft0uuid'),
    )
    op.create_index(
        'admin_config_drafts_status_idx',
        'admin_config_drafts',
        ['status'],
        unique=False,
    )
