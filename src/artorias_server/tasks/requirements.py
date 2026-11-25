import copy
import json
from artorias_shared.ids import uid
from .models import Criterion


def requirements_for(task):
    return {
        "goal": task["goal"],
        "constraints": json.loads(task["constraints"]),
        "criteria": json.loads(task["criteria"]),
    }


def initial_requirements(item):
    initial = item.initial_requirements
    criteria = {
        f"C{i}": {**c.model_dump(), "v": 1} for i, c in enumerate(initial.criteria, 1)
    }
    if not any(c["required"] for c in criteria.values()):
        criteria["signoff"] = {
            "v": 1,
            "text": "用户接受本次交付",
            "required": True,
            "verification": {"method": "user", "question": "是否接受本次交付？"},
        }
    return {
        "goal": initial.goal,
        "constraints": {
            f"K{i}": {"text": t, "v": 1} for i, t in enumerate(initial.constraints, 1)
        },
        "criteria": criteria,
    }


def apply_patch(current, patch, user_input):
    result = copy.deepcopy(current)
    aliases = {}
    for change in patch.operations:
        if change.evidence not in user_input:
            raise ValueError("需求变更缺少用户原文依据")
        op, field, key, value = change.op, change.field, change.id, change.value
        if field == "goal":
            if op != "replace" or key or not isinstance(value, str):
                raise ValueError("Invalid goal replacement")
            result[field] = value
            continue
        entries = result[field]
        if op == "remove":
            if key not in entries or value is not None:
                raise ValueError("Invalid removal")
            del entries[key]
            continue
        if field == "criteria" and isinstance(value, Criterion):
            definition = value.model_dump()
        elif field == "constraints" and isinstance(value, str):
            definition = {"text": value}
        else:
            raise ValueError("Invalid requirement type")
        if op == "add":
            if key or any(
                entry["text"] == definition["text"] for entry in entries.values()
            ):
                raise ValueError("Duplicate requirement or model-assigned ID")
            key = uid()
            if change.client_ref:
                if (
                    change.client_ref in aliases
                    or change.client_ref in current["criteria"]
                ):
                    raise ValueError("Duplicate client_ref")
                aliases[change.client_ref] = key
            version = 1
        else:
            if key not in entries:
                raise ValueError("Unknown requirement ID")
            version = entries[key]["v"]
            if entries[key] == dict(definition, v=version):
                continue
            version += 1
        entries[key] = dict(definition, v=version)
    if max(len(result["criteria"]), len(result["constraints"])) > 30:
        raise ValueError("有效要求超过上限")
    return result, aliases
