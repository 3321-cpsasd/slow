"""Correct the fashion-to-ux demo learner profile.

Revision ID: 0023_correct_demo_persona
Revises: 0022_local_credentials
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_correct_demo_persona"
down_revision = "0022_local_credentials"
branch_labels = None
depends_on = None


USER_ID = "user_fashion_to_ux"
SHELF_ID = "shelf_fashion_to_ux"


def upgrade():
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE users SET name=:new_name "
            "WHERE id=:user_id AND name=:old_name"
        ),
        {
            "user_id": USER_ID,
            "old_name": "服装设计转交互设计",
            "new_name": "产品设计学习信息可视化",
        },
    )
    connection.execute(
        sa.text(
            "UPDATE shelves "
            "SET name=:new_name, specialty=:new_specialty, tags_json=:new_tags "
            "WHERE id=:shelf_id AND user_id=:user_id "
            "AND name=:old_name AND specialty=:old_specialty "
            "AND tags_json=:old_tags"
        ),
        {
            "shelf_id": SHELF_ID,
            "user_id": USER_ID,
            "old_name": "交互设计",
            "old_specialty": "交互设计转型",
            "old_tags": '["交互设计", "用户体验", "跨专业"]',
            "new_name": "信息可视化",
            "new_specialty": "产品设计与信息可视化",
            "new_tags": '["信息可视化", "产品设计", "数据表达"]',
        },
    )


def downgrade():
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE users SET name=:old_name "
            "WHERE id=:user_id AND name=:new_name"
        ),
        {
            "user_id": USER_ID,
            "old_name": "服装设计转交互设计",
            "new_name": "产品设计学习信息可视化",
        },
    )
    connection.execute(
        sa.text(
            "UPDATE shelves "
            "SET name=:old_name, specialty=:old_specialty, tags_json=:old_tags "
            "WHERE id=:shelf_id AND user_id=:user_id "
            "AND name=:new_name AND specialty=:new_specialty "
            "AND tags_json=:new_tags"
        ),
        {
            "shelf_id": SHELF_ID,
            "user_id": USER_ID,
            "old_name": "交互设计",
            "old_specialty": "交互设计转型",
            "old_tags": '["交互设计", "用户体验", "跨专业"]',
            "new_name": "信息可视化",
            "new_specialty": "产品设计与信息可视化",
            "new_tags": '["信息可视化", "产品设计", "数据表达"]',
        },
    )
