"""Deterministic requirement reducer. LLM proposes semantics; code validates changes."""
import copy
import json


def requirements_for(task):
    return {
        'goal': task['goal'], 'constraints': json.loads(task['constraints']),
        'criteria': json.loads(task['criterias']),
    }


def initial_requirements(item):
    return {'goal': item.goal,
            'constraints': {f'constraint_{i+1}': v for i, v in enumerate(item.constraints)},
            'criteria': {f'criterion_{i+1}': v for i, v in enumerate(item.criteria)}}


def apply_patch(current, patch, user_input):
    result = copy.deepcopy(current)
    for change in patch.operations:
        if not change.evidence or change.evidence not in user_input:
            raise ValueError('需求变更必须引用本次用户输入的原文依据')
        if change.field == 'goal':
            if change.op != 'replace' or not change.value or change.id is not None:
                raise ValueError('goal 仅支持 replace，且不能带约束 ID')
            result['goal'] = change.value
            continue
        entries = result[change.field]
        if not change.id:
            raise ValueError('约束和验收标准必须有稳定 ID')
        if change.op == 'add':
            if change.id in entries or not change.value:
                raise ValueError('新增 ID 已存在或值为空')
            entries[change.id] = change.value
        elif change.op == 'replace':
            if change.id not in entries or not change.value:
                raise ValueError('替换 ID 不存在或值为空')
            entries[change.id] = change.value
        else:
            if change.id not in entries or change.value is not None:
                raise ValueError('删除 ID 不存在或包含新值')
            del entries[change.id]
    if len(result['constraints']) > 30 or len(result['criteria']) > 30:
        raise ValueError('有效约束或验收标准超过上限')
    return result
