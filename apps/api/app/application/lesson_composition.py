"""Server-owned lesson composition derived from frozen lesson inputs.

Composition changes how a lesson is explained, never what may be assessed.
The deterministic, versioned result is included in every generation audit.
"""

from __future__ import annotations

import re
from typing import Any


LESSON_COMPOSITION_POLICY_VERSION = "lesson_composition_policy_v1"
LESSON_COMPOSITION_RESOLVER_VERSION = "contract_epistemic_resolver_v3"


_FACTUAL_CASE_ROLES = {"empirical_case", "primary_source"}
_FACTUAL_CASE_KINDS = {"empirical_case", "primary_source_case"}
_FACTUAL_EVIDENCE_FORMS = {
    "authoritative_rule",
    "empirical_case",
    "historical_case",
    "primary_source",
    "research_evidence",
}


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
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["worked_example"]},
    },
    "technical_procedural": {
        "knowledgeForm": "procedure_or_technical_system",
        "learningOperation": "execute_and_debug",
        "evidenceForms": ["worked_result", "observable_behavior"],
        "recommendedRoles": ["mechanism", "worked_example", "boundary", "practice"],
        "recommendedTeachingMoves": ["explain_mechanism", "demonstrate", "diagnose_error", "guided_practice"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["worked_example"]},
    },
    "scientific_causal": {
        "knowledgeForm": "causal_process",
        "learningOperation": "explain_and_predict",
        "evidenceForms": ["experimental_observation", "causal_reasoning"],
        "recommendedRoles": ["mechanism", "evidence_analysis", "counterexample", "boundary"],
        "recommendedTeachingMoves": ["trace_causality", "interpret_evidence", "test_generalization", "expose_boundary"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["empirical_case"]},
    },
    "historical_evidentiary": {
        "knowledgeForm": "historical_process",
        "learningOperation": "construct_evidence_based_explanation",
        "evidenceForms": ["primary_source", "historical_case"],
        "recommendedRoles": ["context", "primary_source", "evidence_analysis", "alternative_interpretation", "synthesis"],
        "recommendedTeachingMoves": ["situate_context", "interpret_evidence", "compare_explanations", "synthesize"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["primary_source_case"]},
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
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["empirical_case", "counterexample"]},
    },
    "normative_case_analysis": {
        "knowledgeForm": "rule_or_norm",
        "learningOperation": "apply_rule_to_case",
        "evidenceForms": ["authoritative_rule", "case_application"],
        "recommendedRoles": ["primary_source", "worked_example", "boundary", "counterargument", "practice"],
        "recommendedTeachingMoves": ["state_rule", "apply_to_case", "expose_exception", "weigh_competing_reasons"],
        "casePolicy": {"minimumDistinctCases": 0, "preferredKinds": ["empirical_case", "counterexample"]},
    },
}


def resolve_lesson_composition_policy(
    *,
    section: dict[str, Any],
    targets: list[dict[str, Any]],
    knowledge_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable, non-blocking presentation preference.

    Pedagogical forms are advisory. Factual case forms are offered only when
    the frozen knowledge context contains claims that could support them.
    """

    weighted_fields = [
        (str(section.get("title") or ""), 1),
        (str(section.get("question") or ""), 2),
        *[(str(item.get("objective") or ""), 2) for item in targets],
        *[(str(item.get("dimension") or ""), 3) for item in targets],
        *[(str(item.get("verificationPolicy") or ""), 4) for item in targets],
    ]

    def contains_signal(text: str, signal: str) -> bool:
        folded_text = text.casefold()
        folded_signal = signal.casefold()
        if re.fullmatch(r"[a-z][a-z ]*", folded_signal):
            return bool(re.search(
                rf"(?<![a-z]){re.escape(folded_signal)}(?![a-z])",
                folded_text,
            ))
        return folded_signal in folded_text

    profile = "generic_conceptual"
    matched_signals: list[str] = []
    best_score = 0
    for candidate, signals in _PROFILE_RULES:
        candidate_hits: list[str] = []
        score = 0
        for signal in signals:
            hit_weights = [
                weight
                for text, weight in weighted_fields
                if contains_signal(text, signal)
            ]
            if hit_weights:
                candidate_hits.append(signal)
                score += max(hit_weights)
        if score > best_score:
            profile = candidate
            matched_signals = candidate_hits[:6]
            best_score = score
    target_count = max(1, len(targets))
    policy = {
        "schemaVersion": LESSON_COMPOSITION_POLICY_VERSION,
        "resolverVersion": LESSON_COMPOSITION_RESOLVER_VERSION,
        "profile": profile,
        "basis": "frozen_contract_deterministic_inference",
        "matchedSignals": matched_signals,
        **{
            **_PROFILE_POLICY[profile],
            "evidenceForms": list(_PROFILE_POLICY[profile]["evidenceForms"]),
            "recommendedRoles": list(_PROFILE_POLICY[profile]["recommendedRoles"]),
            "recommendedTeachingMoves": list(
                _PROFILE_POLICY[profile]["recommendedTeachingMoves"]
            ),
            "casePolicy": {
                **_PROFILE_POLICY[profile]["casePolicy"],
                # Case quantity is a writing preference, never a publication gate.
                "minimumDistinctCases": 0,
                "preferredKinds": list(
                    _PROFILE_POLICY[profile]["casePolicy"]["preferredKinds"]
                ),
            },
        },
        "minimumBlocks": min(12, max(2, target_count + 1)),
        "maximumBlocks": 12,
    }
    claims = list((knowledge_context or {}).get("claims") or [])
    has_bound_claims = any(item.get("claimVersionId") for item in claims)
    if not has_bound_claims:
        policy["recommendedRoles"] = [
            role
            for role in policy["recommendedRoles"]
            if role not in _FACTUAL_CASE_ROLES
        ]
        policy["casePolicy"]["preferredKinds"] = [
            kind
            for kind in policy["casePolicy"]["preferredKinds"]
            if kind not in _FACTUAL_CASE_KINDS
        ]
        policy["evidenceForms"] = [
            form
            for form in policy["evidenceForms"]
            if form not in _FACTUAL_EVIDENCE_FORMS
        ] or ["conceptual_reasoning"]
    return policy
