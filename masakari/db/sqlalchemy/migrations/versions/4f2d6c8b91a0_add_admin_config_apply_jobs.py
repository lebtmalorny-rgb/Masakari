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

"""Add admin config apply jobs

Revision ID: 4f2d6c8b91a0
Revises: 9f33c2d21f7a
Create Date: 2026-06-10 13:40:00.000000
"""

from alembic import op
from oslo_db.sqlalchemy import types as oslo_db_types
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4f2d6c8b91a0'
down_revision = '9f33c2d21f7a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_config_apply_jobs',
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
        sa.Column('draft_uuid', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('strategy', sa.String(length=32), nullable=False),
        sa.Column('canary', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('plan', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('errors', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('uuid', name='uniq_admin_config_apply_job0uuid'),
    )
    op.create_index(
        'admin_config_apply_jobs_draft_uuid_idx',
        'admin_config_apply_jobs',
        ['draft_uuid'],
        unique=False,
    )
    op.create_index(
        'admin_config_apply_jobs_status_idx',
        'admin_config_apply_jobs',
        ['status'],
        unique=False,
    )
