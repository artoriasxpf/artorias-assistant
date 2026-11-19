import json
import pytest
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.repository import TaskRepository
from artorias_server.tasks.models import Plan, Item, RequirementPatch, RequirementOperation
from artorias_server.tasks.context import ContextBuilder
from artorias_server.tasks.results import result_tool


def setup(tmp_path):
    store = Store(tmp_path/'app.db')
    sid = store.session()
    store.submit(sid, '写报告，使用英语，保留来源', 'one')
    run = store.claim()
    repo = TaskRepository(store)
    repo.install(run, Plan(kind='tasks', items=[Item(key='a', goal='报告', constraints=['英语', '来源'])]), [])
    step = store.query('SELECT * FROM task_steps')[0]
    repo.deliver(run, step, '任务一成果')
    repo.deliver(run, step, '重复交付不能覆盖')
    store.finish(run, 'completed', '其他任务混合输出不应带入')
    return store, repo, sid, step


def change(store, repo, sid, task_id, scope='persistent'):
    store.submit(sid, '改为中文', 'two')
    run = store.claim()
    candidates = repo.candidates(sid, '报告')
    item = Item(key='b', goal='更新报告', relation='refine', task_id=task_id,
                task_patch=RequirementPatch(scope=scope, expected_revision=0, operations=[
                    RequirementOperation(op='replace', field='constraints', id='constraint_1', value='中文', evidence='改为中文')]))
    plan = Plan(kind='tasks', items=[item])
    repo.install(run, plan, candidates)
    return run, plan, candidates


@pytest.mark.parametrize('scope', ['persistent', 'execution'])
def test_patches_snapshots_and_idempotency(tmp_path, scope):
    store, repo, sid, first = setup(tmp_path)
    run, plan, candidates = change(store, repo, sid, first['task'], scope)
    repo.install(run, plan, candidates)
    task = store.query('SELECT * FROM business_tasks')[0]
    assert task['execution_count'] == 1
    assert task['revision'] == (1 if scope == 'persistent' else 0)
    assert json.loads(task['requirements'])['constraints']['constraint_1'] == ('中文' if scope == 'persistent' else '英语')
    step = store.query('SELECT * FROM task_steps WHERE run=?', (run['id'],))[0]
    assert json.loads(step['baseline_requirements'])['constraints']['constraint_1'] == '英语'
    body = ContextBuilder(repo).build(run, step)
    assert body['effective_requirements']['constraints'] == {'constraint_1': '中文', 'constraint_2': '来源'}
    assert '其他任务混合输出' not in json.dumps(body, ensure_ascii=False)
    assert any(m['role'] == 'task_result' for m in body['history'])
    # Execution failure does not roll back an already accepted permanent change.
    store.finish(run, 'failed')
    assert store.query('SELECT revision FROM business_tasks')[0]['revision'] == task['revision']
    assert result_tool(store,sid,first['task']).invoke({'run_id':first['run'],'step_key':'a'})['content'] == '任务一成果'
    with pytest.raises(ValueError):
        result_tool(store,store.session(),first['task']).invoke({'run_id':first['run'],'step_key':'a'})


def test_stale_and_invalid_patch_atomicity(tmp_path):
    store, repo, sid, first = setup(tmp_path)
    run, plan, candidates = change(store, repo, sid, first['task'])
    store.finish(run, 'completed')
    store.submit(sid, '改为中文', 'three')
    next_run = store.claim()
    with pytest.raises(ValueError, match='revision conflict'):
        repo.install(next_run, plan, candidates)
    assert not store.query('SELECT * FROM task_plans WHERE run=?', (next_run['id'],))
    fresh = repo.candidates(sid, '报告')
    plan.items[0].task_patch.expected_revision = 1
    plan.items[0].task_patch.operations[0].evidence = '用户没有说过的话'
    with pytest.raises(ValueError): repo.install(next_run, plan, fresh)
    assert not store.query('SELECT * FROM task_steps WHERE run=?', (next_run['id'],))


def test_remove_preserves_unmentioned(tmp_path):
    from artorias_server.tasks.requirements import apply_patch
    source={'goal':'g','constraints':{'a':'x','b':'y'},'criteria':{}}
    patch=RequirementPatch(operations=[RequirementOperation(op='remove',field='constraints',id='a',evidence='删除x')])
    assert apply_patch(source,patch,'删除x')['constraints']=={'b':'y'}
    assert source['constraints']=={'a':'x','b':'y'}
