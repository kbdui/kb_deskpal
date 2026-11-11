"""只压缩宿主索引和重复包装，不把助手提议变成事实。"""

import json

import pytest

from deskpal.companion.understanding import parse_understanding
from deskpal.llm.context import history_view, references_view, role_aliases, role_view
from deskpal.llm.models import LLMResponse
from deskpal.memory.feedback import FeedbackValidationError, evaluate, feedback_view
from deskpal.planner.llm_planner import LLMPlanner
from deskpal.tools.models import ToolCall


def test_compact_role_keeps_meaning_and_reversible_aliases():
    role=dict(role_id='role-uuid',revision=7,character_card=dict(text='爱实践的园艺师',revision=2),
        learned_habits=[dict(id='habit-uuid',content='先看看实际条件',reason='多次交流',nature='inferred',
            revision=3,refs=[dict(kind='dialogue_turn',id='long-source-uuid',revision=1)])],
        current_emotion=dict(description_intensity='有些担心',reason_sources='花木暂未恢复',time_scope='这几天',
            uncertainty='等待观察',influence_weight=.7,updated_at=123))
    compact=role_view(role)
    assert compact['learned_habits'][0]['id']=='h1'
    assert role_aliases(role)=={'h1':'habit-uuid'}
    assert compact['current_emotion']['influence_weight']==.7
    assert 'uuid' not in json.dumps(compact)
    assert role['learned_habits'][0]['refs'][0]['id']=='long-source-uuid'


def test_historical_proposals_and_execution_failures_survive_projection():
    history=[dict(role='assistant',content='建议周五见面',source_ref={'id':'task:assistant'},
        tool_facts=[dict(kind='tool_fact',verified=True,fact={'outcome':'unknown','error':'network'})])]
    compact=history_view(history)[0]
    assert compact['source']=='history:0' and compact['confirmed'] is False
    assert compact['role'] == 'assistant'
    assert 'instruction' not in compact and 'externally_verified' not in compact
    assert compact['tool_facts'][0]['fact']['outcome']=='unknown'
    observations={'kind':'execution_observations','data':[{'outcome':'partial','content':'前半段已完成，后半段失败'}]}
    assert references_view([observations])==[observations]


@pytest.mark.parametrize('replacement', [[{}], [1], 'h1'])
def test_context_rejects_non_string_aliases_without_type_error(replacement):
    with pytest.raises(ValueError):
        parse_understanding({'entries':[{'replaces':replacement}]},sources={})


class Model:
    def __init__(self,body): self.body=body
    def chat(self,messages,*,tools=()): return LLMResponse(json.dumps(self.body))


def test_optional_no_change_groups_and_null_metadata_are_host_defaults():
    result=evaluate(Model({}),{'catalog':{}})
    assert result==dict(memories=[],reviews=[],habits=[],emotion=None,summary=None)
    body=dict(memories=[dict(content='喜欢园艺',destination='facts',claim_type='user_report',
        reason='明确表达',quote='喜欢园艺',sources=['user'])])
    result=evaluate(Model(body),{'catalog':{'user':{'content':'喜欢园艺'}}})
    assert result['memories'][0]['review_after'] is None
    assert result['memories'][0]['uncertainty']==''


def test_retain_review_uses_exact_original_and_frozen_revision():
    payload=dict(catalog={'memory0':{'content':'用户的实际原话'}},understandings=[dict(id='m1',revision=9,
        content='当前理解',destination='facts',claim_type='user_report',uncertainty='待观察',original_source_key='memory0')])
    result=evaluate(Model({'reviews':[dict(id='m1',decision='retain',reason='尚无新证据')]}),payload)
    row=result['reviews'][0]
    assert row['revision']==9 and row['content']=='当前理解' and row['quote']=='用户的实际原话'
    assert row['sources']==['memory0'] and row['uncertainty']=='待观察'


def test_omitted_retain_fields_preserve_original_quote_and_review_dates():
    payload=dict(catalog={'memory0':{'content':'原文背景'*3000+'用户的实际原话'}},understandings=[dict(id='m1',revision=9,
        content='当前理解',destination='facts',claim_type='user_report',uncertainty='待观察',original_source_key='memory0',
        quote='用户的实际原话',review_after='2026-11-04T12:00:00+08:00',valid_from='2026-11-01T12:00:00+08:00',valid_to=None)])
    result=evaluate(Model({'reviews':[dict(id='m1',decision='retain',reason='尚无新证据')]}),payload)
    row=result['reviews'][0]
    assert row['quote']=='用户的实际原话' and row['review_after']=='2026-11-04T12:00:00+08:00'
    assert row['valid_from']=='2026-11-01T12:00:00+08:00'
    assert 'quote' not in feedback_view(payload)['understandings'][0]


def test_feedback_has_one_copy_of_history_and_keeps_attribution():
    payload=dict(catalog={'history0':dict(content='建议周五见面',role='assistant',origin='assistant_output',
        available=True,refs=[{'id':'source-uuid'}],use_policy='cloud_allowed')},
        published_history=[{'content':'建议周五见面'}],current_exchange={'request':'重复正文'})
    compact=feedback_view(payload)
    assert json.dumps(compact,ensure_ascii=False).count('建议周五见面')==1
    assert compact['catalog']['history0']['origin']=='assistant_output'
    assert compact['published_history']==[{'source':'history0'}]
    assert payload['catalog']['history0']['refs']==[{'id':'source-uuid'}]


def test_invalid_time_has_specific_field_without_echoing_text():
    model=Model({'emotion':dict(description_intensity='担忧',reason_sources='用户提到难题',time_scope='今天',
        uncertainty='未决',sources=['user'],expires_at='2026-11-03')})
    with pytest.raises(FeedbackValidationError) as caught:
        evaluate(model,{'catalog':{'user':{'content':'难题'}}})
    assert caught.value.code=='time_invalid' and caught.value.field=='emotion.expires_at'
    assert '2026' not in str(caught.value)


def test_context_metadata_with_clarification_keeps_awaiting_input_action():
    class NativeModel:
        def chat(self,messages,*,tools=()):
            return LLMResponse('{"action":"clarify","reply":"你指哪件事？"}',
                (ToolCall('turn_context',{'entries':[]},'meta'),))
    result=LLMPlanner(NativeModel()).plan_context('帮忙处理',references=[
        {'kind':'role_context','data':{'character_card':{'text':'园艺师'}}},
        {'kind':'context_clock','now':'2026-11-03T12:00:00+08:00'}],llm=NativeModel())
    assert result.action=='clarify' and result.reply=='你指哪件事？'
    assert result.context=={'entries':[]} and result.steps==()
