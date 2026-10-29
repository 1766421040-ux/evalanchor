"""Actual World replay with explicit scripted test doubles, never model scores."""
import copy
import json

import pytest

from evalanchor.agent_api import make_request, response_usage
from evalanchor.agent_runner import run_episode, audit_episode, run_experiment, audit_experiment, default_configs
from evalanchor.contracts import ContractError, fingerprint
from evalanchor.orderops import build_cases, benchmark_suite, scripted_oracle


class FixtureLedger:
    def snapshot(self):return {'known_spent_usd':0,'kind':'fixture_no_network'}


class ScriptedClient:
    kind='scripted_fixture'
    def __init__(self,cases):self.cases={c['id']:c for c in cases};self.calls=0;self.ledger=FixtureLedger()
    def generate(self,*,request_key,model,messages,tools,max_output_tokens):
        self.calls+=1
        _,_,case_id,_,turn=request_key.split(':');turn=int(turn)
        action=scripted_oracle(self.cases[case_id])[turn]
        args=action.get('arguments',action.get('args'))
        native={'id':f'fixture-{fingerprint(request_key)}','status':'completed','model':model,'service_tier':'default',
            'output':[{'type':'function_call','call_id':f'call-{turn}','name':action['name'],'arguments':json.dumps(args)}],
            'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}
        request=make_request(model,messages,tools,max_output_tokens)
        usage,cost=response_usage(native,model,max_output_tokens)
        return {'request_key':request_key,'request':copy.deepcopy(request),'request_hash':fingerprint(request),'response':native,'response_hash':fingerprint(native),'usage':usage,'estimated_cost_usd':cost,'latency_ms':0,'replayed':False}


@pytest.mark.parametrize('case',build_cases(),ids=lambda c:c['id'])
def test_all_state_goals_are_reachable_and_replayable(case):
    cfg=default_configs()[0]
    r=run_episode(case,cfg,0,client=ScriptedClient([case]),experiment_id='fixture',max_turns=8)
    assert r['grade']['task_success'] is True
    assert audit_episode(r,case,cfg)['verified']


def test_complete_matrix_resume_skips_all_model_calls_and_audits(tmp_path):
    benchmark=benchmark_suite();cases=[c for c in benchmark['cases'] if c['split']=='dev'][:2]
    cfgs=default_configs()[:2];client=ScriptedClient(cases)
    result=run_experiment(benchmark,cfgs,tmp_path/'fixture',client=client,case_ids=[c['id'] for c in cases],split='dev',repeats=2)
    assert len(result['records'])==8 and result['provenance']['kind']=='scripted_fixture'
    count=client.calls
    resumed=run_experiment(benchmark,cfgs,tmp_path/'fixture',client=client,case_ids=[c['id'] for c in cases],split='dev',repeats=2,resume=True)
    assert client.calls==count and audit_experiment(resumed)['verified']
    from evalanchor.agent_analysis import summarize_experiment
    analysis=summarize_experiment(resumed)
    assert analysis


@pytest.mark.parametrize('kind',['score','final','state','tool','request','status'])
def test_resigned_tampering_cannot_bypass_replay(kind):
    case=build_cases('dev')[0];cfg=default_configs()[0]
    r=run_episode(case,cfg,0,client=ScriptedClient([case]),experiment_id='fixture')
    if kind=='score':r['grade']['task_success']=False
    elif kind=='final':r['final']['facts_json']='{}'
    elif kind=='state':r['final_state']['forged']=True
    elif kind=='tool':r['steps'][0]['tool_calls'][0]['result']={'ok':False}
    elif kind=='request':r['steps'][0]['request']['input'][0]['content']='changed';r['steps'][0]['request_hash']=fingerprint(r['steps'][0]['request'])
    elif kind=='status':r['status']='no_finish'
    r['fingerprint']=fingerprint({k:v for k,v in r.items() if k!='fingerprint'})
    with pytest.raises(ContractError):audit_episode(r,case,cfg)


def test_matrix_missing_cell_is_not_a_smaller_successful_run(tmp_path):
    benchmark=benchmark_suite();case=build_cases('dev')[0]
    r=run_experiment(benchmark,default_configs()[:1],tmp_path/'fixture',client=ScriptedClient([case]),case_ids=[case['id']],split='dev')
    r['records']=[]
    with pytest.raises(ContractError,match='missing matrix'):audit_experiment(r)


def test_seeded_schedule_preserves_complete_blocks_and_reproducibility():
    from evalanchor.agent_runner import scheduled_jobs
    cases=build_cases('dev');configs=default_configs()
    def keys(seed):return [(c['id'],f['id'],r) for c,f,r in scheduled_jobs(cases,configs,2,seed)]
    a=keys(20261029)
    assert a==keys(20261029) and a!=keys(20261030)
    assert set(a)==set(keys(None)) and len(a)==len(set(a))==192
    for i in range(0,len(a),4):
        block=a[i:i+4]
        assert len({(c,r) for c,_,r in block})==1
        assert {cfg for _,cfg,_ in block}=={c['id'] for c in configs}
