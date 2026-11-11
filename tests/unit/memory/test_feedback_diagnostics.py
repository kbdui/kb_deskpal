"""反馈失败的结构诊断不包含模型正文；保留现有接受/拒绝边界。"""
import json
from types import SimpleNamespace

import pytest

from deskpal.llm.diagnostics import sanitize
from deskpal.llm.models import LLMResponse
from deskpal.memory.feedback import FeedbackValidationError, evaluate


def payload():
    return {'catalog': {'memory0': {'content': '合成原话', 'origin': 'user_message'}},
            'understandings': [{'id': 'synthetic', 'revision': 1, 'content': '合成理解',
                'destination': 'facts', 'claim_type': 'user_report', 'uncertainty': '',
                'original_source_key': 'memory0', 'quote': '合成原话'}]}


def evaluate_result(result):
    provider = SimpleNamespace(chat=lambda *args, **kwargs: LLMResponse(json.dumps(result)))
    return evaluate(provider, payload())


def test_minimal_adoption_inherits_existing_content_and_evidence():
    row = evaluate_result({'reviews': [{'id': 'synthetic', 'decision': 'adopt', 'reason': '有实际依据'}]})['reviews'][0]
    assert row['content'] == '合成理解' and row['quote'] == '合成原话'
    assert row['sources'] == ['memory0'] and row['revision'] == 1
    assert row['destination'] == 'facts' and row['claim_type'] == 'user_report'


def test_adoption_that_changes_content_still_requires_complete_change_and_evidence():
    with pytest.raises(FeedbackValidationError) as caught:
        evaluate_result({'reviews': [{'id': 'synthetic', 'decision': 'adopt', 'reason': '保密正文', 'content': '改变后的理解'}]})
    error = caught.value
    assert error.field == 'reviews[0]'
    assert error.diagnostics['processing_missing_fields'] == ['claim_type', 'destination', 'quote', 'sources']
    assert error.diagnostics['processing_extra_fields'] == []
    assert error.diagnostics['processing_decision'] == 'adopt'
    assert '保密正文' not in json.dumps(error.diagnostics, ensure_ascii=False)


@pytest.mark.parametrize('overrides', [{'subject':'another-person'}, {'destination':'habit'},
                                      {'claim_type':'observed_event'}, {'temporal':True},
                                      {'valid_from':'2026-11-01T00:00:00+08:00'},
                                      {'valid_to':'2026-11-01T00:00:00+08:00'}])
def test_adoption_cannot_change_attribution_or_time_by_silently_inheriting(overrides):
    with pytest.raises(FeedbackValidationError):
        evaluate_result({'reviews': [{'id': 'synthetic', 'decision': 'adopt', 'reason': '变化', **overrides}]})


def test_correction_prompt_contains_safe_exact_field_difference_and_rule():
    messages = []
    provider = SimpleNamespace(chat=lambda sent, **kwargs: (messages.extend(sent), LLMResponse('{}'))[1])
    evaluate(provider, payload(), correction=True, correction_code='fields_invalid', correction_field='reviews[0]',
        correction_diagnostics={'processing_missing_fields':['quote','sources'], 'processing_extra_fields':['status'],
            'processing_rule':'field_set', 'processing_expected_type':'object', 'prompt':'不应传递的正文'})
    system = messages[0].content
    assert '"processing_missing_fields":["quote","sources"]' in system
    assert '"processing_extra_fields":["status"]' in system
    assert '不应传递' not in system
    messages.clear()
    evaluate(provider, payload(), correction=True, correction_code='proposal_invalid', correction_field='feedback',
             correction_diagnostics={'processing_rule':'role_delivery'})
    assert 'buffer_id' in messages[0].content and '同时输出 habits' in messages[0].content


def test_minimal_retention_remains_valid_and_input_only_fields_are_reported():
    row = {'id': 'synthetic', 'decision': 'retain', 'reason': '仍有意义'}
    assert evaluate_result({'reviews': [row]})['reviews'][0]['content'] == '合成理解'
    with pytest.raises(FeedbackValidationError) as caught:
        evaluate_result({'reviews': [{**row, 'status': '保密内容', '任意私密字段': '保密内容'}]})
    diagnostics = caught.value.diagnostics
    assert diagnostics['processing_extra_count'] == 2
    assert diagnostics['processing_extra_fields'] == ['status', 'unknown']
    assert '保密' not in json.dumps(diagnostics, ensure_ascii=False)


@pytest.mark.parametrize('value,actual', [(None, 'null'), (True, 'boolean'), ([], 'array'), ('保密内容', 'string')])
def test_review_type_failure_has_expected_and_actual_type(value, actual):
    with pytest.raises(FeedbackValidationError) as caught:
        evaluate_result({'reviews': [value]})
    assert caught.value.diagnostics['processing_stage'] == 'normalize'
    assert caught.value.diagnostics['processing_actual_type'] == actual
    assert caught.value.diagnostics['processing_expected_type'] == 'object'


def test_new_diagnostic_fields_are_bounded_and_filtered():
    clean = sanitize({'processing_missing_fields': ['quote', 'secret-key', '秘密'],
        'processing_extra_fields': ['status'] * 100, 'processing_stage': 'private-value',
        'processing_actual_type': 'private-value', 'learning_kind': 'review',
        'learning_attempt': 2, 'processing_actual_bytes': -1, 'learning_correction': True})
    assert clean == {'processing_missing_fields': ['quote', 'unknown'], 'processing_extra_fields': ['status'],
                     'learning_kind': 'review', 'learning_attempt': 2}
