from dataclasses import dataclass


LOCAL_DEMO_PASSWORD = "SlowDemo2026!"


@dataclass(frozen=True)
class LocalDemoPersona:
    user_id: str
    username: str
    display_name: str
    scenario: str
    shelf_name: str
    domain: str
    specialty: str
    tags: tuple[str, ...]


LOCAL_DEMO_PERSONAS = (
    LocalDemoPersona(
        user_id="user_cs_freshman",
        username="cs-freshman",
        display_name="计算机新生",
        scenario="计算机专业新生建立编程与计算机基础",
        shelf_name="计算机基础",
        domain="计算机科学",
        specialty="大学计算机入门",
        tags=("编程基础", "计算机导论"),
    ),
    LocalDemoPersona(
        user_id="user_finance_postgrad",
        username="finance-postgrad",
        display_name="金融考研生",
        scenario="金融专业学生准备研究生入学考试",
        shelf_name="金融考研",
        domain="金融学",
        specialty="研究生入学考试",
        tags=("金融学", "考研"),
    ),
    LocalDemoPersona(
        user_id="user_math_functional",
        username="math-functional",
        display_name="泛函期末生",
        scenario="数学系学生准备泛函分析期末考试",
        shelf_name="泛函分析",
        domain="数学",
        specialty="泛函分析期末",
        tags=("泛函分析", "期末复习"),
    ),
    LocalDemoPersona(
        user_id="user_dance_civil",
        username="dance-civil",
        display_name="舞蹈系考公生",
        scenario="舞蹈系学生跨专业准备公务员考试",
        shelf_name="公务员考试",
        domain="公共考试",
        specialty="跨专业考公",
        tags=("行测", "申论", "跨专业"),
    ),
)


LOCAL_DEMO_PERSONAS_BY_USER_ID = {
    persona.user_id: persona for persona in LOCAL_DEMO_PERSONAS
}
