"""给旧的行为测试替身补齐新结束协议；协议严格性由 planner 单元测试独立检查。"""
from dataclasses import replace
import json


class PlanningDecisionProvider:
    def __init__(self, model):
        self.model = model

    def __getattr__(self, name):
        return getattr(self.model, name)

    def chat(self, messages, *, tools=()):
        result = self.model.chat(messages, tools=tools)
        if messages[0].content.startswith('[PLANNING]') and not result.tool_calls:
            value = json.loads(result.content) if result.content.lstrip().startswith('{') else dict(action='answer', reply=result.content)
            value.setdefault('organize_memory', value['action'] == 'answer')
            return replace(result, content=json.dumps(value, ensure_ascii=False))
        return result
