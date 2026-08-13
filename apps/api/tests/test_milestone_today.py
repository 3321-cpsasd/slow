from app.modules.learning.milestones import MilestoneService


def test_today_reason_uses_the_current_section_objective():
    service = MilestoneService(None, user_id="user_1", uid=lambda prefix: f"{prefix}_1")
    series = {
        "id": "series_1",
        "books": [
            {
                "title": "网络基础",
                "chapters": [
                    {
                        "id": "chapter_definition",
                        "title": "计算机网络是什么",
                        "objective": "解释计算机网络的定义和作用",
                        "sections": [
                            {
                                "id": "section_definition",
                                "title": "定义与核心作用",
                                "question": "计算机网络解决了什么问题？",
                                "objectives": ["用自己的话描述计算机网络的定义"],
                                "status": "available",
                            }
                        ],
                    },
                    {
                        "id": "chapter_layers",
                        "title": "网络分层",
                        "objective": "理解网络分层模型",
                        "sections": [],
                    },
                ],
            }
        ],
    }
    milestone = {
        "criteria": [
            {
                "chapterId": "chapter_definition",
                "statement": "准确描述 OSI 和 TCP/IP 分层模型",
                "completed": False,
            }
        ]
    }

    today = service._today(series, resume=None, milestone=milestone)

    assert today is not None
    assert today["sectionId"] == "section_definition"
    assert today["reason"] == "用自己的话描述计算机网络的定义"


def test_today_reason_extracts_statement_from_standard_content_objective():
    service = MilestoneService(None, user_id="user_1", uid=lambda prefix: f"{prefix}_1")
    series = {
        "id": "series_1",
        "books": [
            {
                "title": "网络基础",
                "chapters": [
                    {
                        "id": "chapter_definition",
                        "title": "计算机网络是什么",
                        "objective": "解释计算机网络的定义和作用",
                        "sections": [
                            {
                                "id": "section_definition",
                                "title": "定义与核心作用",
                                "question": "计算机网络解决了什么问题？",
                                "objectives": [
                                    {
                                        "statement": "用自己的话描述计算机网络的定义",
                                        "required": True,
                                        "baselineConceptKey": "network_definition",
                                        "baselineObjectiveKey": "explain_network_definition",
                                    }
                                ],
                                "status": "available",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    today = service._today(series, resume=None, milestone={"criteria": []})

    assert today is not None
    assert today["reason"] == "用自己的话描述计算机网络的定义"
    assert isinstance(today["reason"], str)


def test_today_reason_falls_back_when_section_objective_has_no_statement():
    service = MilestoneService(None, user_id="user_1", uid=lambda prefix: f"{prefix}_1")
    series = {
        "id": "series_1",
        "books": [
            {
                "title": "网络基础",
                "chapters": [
                    {
                        "id": "chapter_definition",
                        "title": "计算机网络是什么",
                        "objective": "解释计算机网络的定义和作用",
                        "sections": [
                            {
                                "id": "section_definition",
                                "title": "定义与核心作用",
                                "question": "计算机网络解决了什么问题？",
                                "objectives": [{"required": True}],
                                "status": "available",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    today = service._today(series, resume=None, milestone={"criteria": []})

    assert today is not None
    assert today["reason"] == "解释计算机网络的定义和作用"
