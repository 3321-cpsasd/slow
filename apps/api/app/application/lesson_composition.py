"""Server-owned lesson composition derived from frozen lesson inputs.

Composition changes how a lesson is explained, never what may be assessed.
The deterministic, versioned result is included in every generation audit.
"""

from __future__ import annotations

from typing import Any


LESSON_COMPOSITION_POLICY_VERSION = "lesson_composition_policy_v1"
LESSON_COMPOSITION_RESOLVER_VERSION = "contract_epistemic_resolver_v1"


_PROFILE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("normative_case_analysis", ("法律", "法条", "判例", "合规", "权利", "义务", "law", "legal", "regulation", "compliance")),
    ("historical_evidentiary", ("历史", "史料", "时代背景", "年代", "历史解释", "history", "historical", "chronology", "primary source")),
    ("textual_argumentative", ("文学", "文本细读", "作品", "修辞", "哲学", "论证", "前提", "反驳", "interpret", "argument", "philosophy", "literature")),
    ("social_empirical", ("社会", "经济", "组织", "制度", "心理", "治理", "公共政策", "群体", "市场", "调查", "social", "economic", "institution", "survey", "empirical", "organization")),
    ("formal_quantitative", ("证明", "定理", "公式", "方程", "推导", "计算", "概率", "矩阵", "函数", "proof", "theorem", "equation", "derive", "calculate")),
    ("technical_procedural", ("代码", "编程", "部署", "配置", "实现", "操作步骤", "调试", "算法", "code", "program", "deploy", "configure", "implement", "debug")),
    ("scientific_causal", ("实验", "变量", "细胞", "物理", "化学", "生物", "反应", "因果机制", "experiment", "variable", "physics", "chemistry", "biology")),
)


_PROFILE_POLICY: dict[str, dict[str, Any]] = {
    "generic_conceptual": {
        "knowledgeForm": "concept_or_system",
        "learningOperation": "explain_and_apply",
        "evidenceForms": ["conceptual_reasoning"],
        "recommendedRoles": ["mechanism", "example", "boundary", "practice"],
        "recommendedTeachingMoves": ["direct_explanation", "illustrate", "expose_boundary", "guided_practice"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": []},
    },
    "formal_quantitative": {
        "knowledgeForm": "formal_or_quantitative_model",
        "learningOperation": "derive_and_apply",
        "evidenceForms": ["logical_derivation", "worked_result"],
        "recommendedRoles": ["derivation", "worked_example", "counterexample", "practice"],
        "recommendedTeachingMoves": ["derive_stepwise", "demonstrate", "diagnose_error", "guided_practice"],
        "casePolicy": {"minimumDistinctCases": 1, "preferredKinds": ["worked_example"]},
    },
    "technical_procedural": {
        "knowledgeForm": "procedure_or_technical_system",
        "learningOperation": "execute_and_debug",
        "evidenceForms": ["worked_result", "observable_behavior"],
        "recommendedRoles": ["mechanism", "worked_example", "boundary", "practice"],
        "recommendedTeachingMoves": ["explain_mechanism", "demonstrate", "diagnose_error", "guided_practice"],
        "casePolicy": {"minimumDistinctCases": 1, "preferredKinds": ["worked_example"]},
    },
    "scientific_causal": {
        "knowledgeForm": "causal_process",
        "learningOperation": "explain_and_predict",
        "evidenceForms": ["experimental_observation", "causal_reasoning"],
        "recommendedRoles": ["mechanism", "evidence_analysis", "counterexample", "boundary"],
        "recommendedTeachingMoves": ["trace_causality", "interpret_evidence", "test_generalization", "expose_boundary"],
        "casePolicy": {"minimumDistinctCases": 1, "preferredKinds": ["empirical_case"]},
    },
    "historical_evidentiary": {
        "knowledgeForm": "historical_process",
        "learningOperation": "construct_evidence_based_explanation",
        "evidenceForms": ["primary_source", "historical_case"],
        "recommendedRoles": ["context", "primary_source", "evidence_analysis", "alternative_interpretation", "synthesis"],
        "recommendedTeachingMoves": ["situate_context", "interpret_evidence", "compare_explanations", "synthesize"],
        "casePolicy": {"minimumDistinctCases": 1, "preferredKinds": ["primary_source_case"]},
    },
    "textual_argumentative": {
        "knowledgeForm": "text_or_argument",
        "learningOperation": "interpret_and_evaluate",
        "evidenceForms": ["textual_evidence", "argument_structure"],
        "recommendedRoles": ["primary_source", "evidence_analysis", "alternative_interpretation", "counterargument", "synthesis"],
        "recommendedTeachingMoves": ["close_read", "reconstruct_argument", "compare_interpretations", "respond_to_objection"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["primary_source_case"]},
    },
    "social_empirical": {
        "knowledgeForm": "social_phenomenon_or_institution",
        "learningOperation": "compare_cases_and_explain",
        "evidenceForms": ["empirical_case", "research_evidence"],
        "recommendedRoles": ["mechanism", "empirical_case", "comparison", "evidence_analysis", "counterexample", "boundary", "transfer"],
        "recommendedTeachingMoves": ["explain_mechanism", "contrast", "interpret_evidence", "test_generalization", "transfer"],
        "casePolicy": {"minimumDistinctCases": 2, "preferredKinds": ["empirical_case", "counterexample"]},
    },
    "normative_case_analysis": {
        "knowledgeForm": "rule_or_norm",
        "learningOperation": "apply_rule_to_case",
        "evidenceForms": ["authoritative_rule", "case_application"],
        "recommendedRoles": ["primary_source", "worked_example", "boundary", "counterargument", "practice"],
        "recommendedTeachingMoves": ["state_rule", "apply_to_case", "expose_exception", "weigh_competing_reasons"],
        "casePolicy": {"minimumDistinctCases": 2, "preferredKinds": ["empirical_case", "counterexample"]},
    },
}


def resolve_lesson_composition_policy(
    *, section: dict[str, Any], targets: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return an auditable presentation policy without changing target identity."""

    corpus = " ".join(
        [str(section.get("title") or ""), str(section.get("question") or ""),
         *[str(item.get("objective") or "") for item in targets],
         *[str(item.get("dimension") or "") for item in targets],
         *[str(item.get("verificationPolicy") or "") for item in targets]]
    ).casefold()
    profile = "generic_conceptual"
    matched_signals: list[str] = []
    for candidate, signals in _PROFILE_RULES:
        hits = [signal for signal in signals if signal.casefold() in corpus]
        if hits:
            profile = candidate
            matched_signals = hits[:6]
            break
    target_count = max(1, len(targets))
    return {
        "schemaVersion": LESSON_COMPOSITION_POLICY_VERSION,
        "resolverVersion": LESSON_COMPOSITION_RESOLVER_VERSION,
        "profile": profile,
        "basis": "frozen_contract_deterministic_inference",
        "matchedSignals": matched_signals,
        **_PROFILE_POLICY[profile],
        "minimumBlocks": min(12, max(2, target_count + 1)),
        "maximumBlocks": 12,
    }
