import pytest
from pydantic import ValidationError

from app.api.schemas import PlanCreate
from app.evaluation.runner import validated_evaluation_input


def plan_input(**overrides):
    values = {
        "shelf_id": "shelf_interaction_design",
        "topic": "交互设计",
        "role": "服装设计专业，正在转向交互设计",
        "experience": "具备设计表达经验，尚未系统学习用户研究",
        "purpose": "完成第一个交互设计作品集项目",
        "depth": "deep",
        "details": "",
    }
    values.update(overrides)
    return PlanCreate(**values)


def test_plan_accepts_freeform_learning_background():
    body = plan_input()

    assert body.role == "服装设计专业，正在转向交互设计"


def test_plan_rejects_blank_learning_background():
    with pytest.raises(ValidationError):
        plan_input(role="")


def test_evaluation_input_maps_background_and_preserves_custom_shelf():
    value = validated_evaluation_input(
        {
            "shelf": {
                "name": "交互设计",
                "domain": "设计学",
                "specialty": "交互设计转型",
                "tags": ["信息可视化", "作品集"],
            },
            "topic": "平面的信息可视化",
            "background": "大三，有服装设计经验，但是电脑小白",
            "experience": "0",
            "purpose": "积累作品集，申请研究生",
            "depth": "mastery",
        }
    )

    assert value["role"] == "大三，有服装设计经验，但是电脑小白"
    assert value["shelfId"] == "<created-for-evaluation>"
    assert value["shelf"]["domain"] == "设计学"


def test_evaluation_input_rejects_empty_custom_payload():
    with pytest.raises(ValidationError):
        validated_evaluation_input({})
