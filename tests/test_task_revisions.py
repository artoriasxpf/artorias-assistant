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
    assert json.loads(task['constraints'])['constraint_1'] == ('中文' if scope == 'persistent' else '英语')
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


def test_context_recalls_five_by_delivery_time_without_keyword_ranking(tmp_path):
    store,repo,sid,first=setup(tmp_path)
    task_id=first['task']
    with store.tx() as conn:
        conn.execute("UPDATE task_steps SET updated_at='2025-01-01T00:00:00.000Z'")
        # A later-inserted Step can be delivered earlier than a preceding Step.
        for day in [7,2,5,4,3,6]:
            conn.execute("""
                INSERT INTO task_steps(run,key,task,scope,status,result,updated_at)
                VALUES(?,'history',?,'{}','delivered',?,?)
            """, (f'run-{day}',task_id,'matching word' if day==2 else 'other',f'2025-01-{day:02}T00:00:00.000Z'))

    store.submit(sid,'matching word', 'next')
    run=store.claim()
    repo.install(run,Plan(kind='tasks',items=[
        Item(key='current',goal='matching word',relation='continue',task_id=task_id),
    ]),repo.candidates(sid,'matching word'))
    step=store.query('SELECT * FROM task_steps WHERE run=?',(run['id'],))[0]
    body=ContextBuilder(repo).build(run,step)
    recalled=[entry['result_ref']['run_id'] for entry in body['history'] if entry['role']=='task_result']
    assert recalled==['run-7','run-6','run-5','run-4','run-3']


def test_task_summary_is_latest_full_delivery_and_progress_is_index(tmp_path):
    store,repo,sid,first=setup(tmp_path)
    run,_,_=change(store,repo,sid,first['task'])
    step=store.query('SELECT * FROM task_steps WHERE run=?',(run['id'],))[0]
    result='最新交付正文' * 1000 + '结尾仍保留'
    repo.deliver(run,step,result)
    repo.deliver(run,step,'duplicate')
    task=store.query('SELECT * FROM business_tasks')[0]
    assert task['summary']==result
    progress=json.loads(task['progress'])
    assert [entry['result_ref'] for entry in progress]==[
        {'run':first['run'],'key':'a'}, {'run':run['id'],'key':'b'},
    ]
    assert all(set(entry)=={'run_id','step_key','source_message_id','status','delivered_at','result_ref'} for entry in progress)
    assert progress[-1]['delivered_at']==store.query(
        'SELECT updated_at FROM task_steps WHERE run=?',(run['id'],),
    )[0]['updated_at']


def test_followup_can_read_full_direct_source_only(tmp_path):
    store,repo,sid,first=setup(tmp_path)
    summary='目录摘要\n'+'关键细节\n'*1500
    with store.tx() as conn:
        conn.execute('UPDATE task_steps SET result=? WHERE run=? AND key=?',(summary,first['run'],first['key']))
    store.submit(sid,'把上面摘要保存到原目录','followup')
    run=store.claim()
    repo.install(run,Plan(kind='tasks',items=[
        Item(key='save',goal='保存上一轮摘要',relation='followup',task_id=first['task']),
    ]),repo.candidates(sid,'保存摘要'))
    step=store.query('SELECT * FROM task_steps WHERE run=?',(run['id'],))[0]
    assert step['task']!=first['task']
    source,=ContextBuilder(repo).build(run,step)['source_results']
    assert source=={'result_ref':{'run_id':first['run'],'step_key':first['key']},
                    'total_chars':len(summary),'content':summary[:4000],'has_more':True}
    read=result_tool(store,sid,step['task']).invoke
    page=read(source['result_ref'])
    tail=read({**source['result_ref'],'offset':page['next_offset'],'limit':8000})
    assert page['content']+tail['content']==summary and not tail['has_more']
    with pytest.raises(ValueError):
        result_tool(store,store.session(),step['task']).invoke(source['result_ref'])
    repo.deliver(run,step,'followup output')
    with pytest.raises(ValueError):
        result_tool(store,sid,first['task']).invoke({'run_id':run['id'],'step_key':step['key']})
