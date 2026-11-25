from langchain_core.messages import AIMessage
from artorias_server.tasks.models import Criterion, FinishStep, Item, RequirementPatch
from artorias_server.tasks.verification import resolve


def user_rule(text):
    return Criterion(text=text, verification={"method": "user", "question": text})


def plan_item(key, goal, constraints=(), criteria=(), relation="new", **fields):
    spec = dict(goal=goal, constraints=list(constraints))
    initial = None
    if relation in ("new", "followup"):
        initial = dict(
            goal=goal,
            constraints=list(constraints),
            criteria=[user_rule(t) for t in criteria],
        )
    else:
        fields.setdefault("task_patch", RequirementPatch(expected_revision=0))
    return Item(
        key=key,
        step_spec=spec,
        initial_requirements=initial,
        relation=relation,
        **fields,
    )


def submission(text):
    return FinishStep(
        step_outcome="completed",
        final_text=text,
        delivery_summary=text[:100],
        milestones=[dict(key="delivery", text=text[:100], evidence_refs=["response"])],
    )


def deliver(repo, run, step, text):
    result = submission(text)
    evidence, *_ = resolve(repo.store, run, step, result)
    return repo.deliver(run, step, result, evidence, [])


def finish_message(text):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "finish-" + text[:8],
                "name": "finish_step",
                "args": submission(text).model_dump(),
            }
        ],
    )
