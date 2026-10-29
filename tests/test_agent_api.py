import copy
import json
import urllib.error

import pytest

from evalanchor.agent_api import AgentAPIError, ResponsesClient, make_request, reservation_estimate, response_usage
from evalanchor.budget import BudgetLedger
from evalanchor.contracts import ContractError, read_json, write_json


def response(model='gpt-6-luna'):
    return {'id':'resp-fixture','model':model,'status':'completed','service_tier':'default',
        'output':[{'type':'function_call','call_id':'call-1','name':'get_order','arguments':'{"order_id":"O-1"}'}],
        'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,
            'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}


class FakeOpener:
    def __init__(self, body=None, error=None):self.body=body or response();self.error=error;self.calls=0;self.requests=[]
    def open(self, req, timeout):
        self.calls+=1;self.requests.append(req)
        if self.error:raise self.error
        return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return json.dumps(self.body).encode()


def generate(client, **changes):
    return client.generate(**{'request_key':'test:case:0','model':'gpt-6-luna','messages':[{'role':'user','content':'hello'}],'tools':[],**changes})


def test_paid_request_is_replayed_and_budget_settled_once(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    opener=FakeOpener()
    client=ResponsesClient(api_key='sensitive-test-value',ledger=ledger,cache_dir=tmp_path/'calls',opener=opener)
    first=generate(client);second=generate(client)
    assert opener.calls==1 and not first['replayed'] and second['replayed']
    assert ledger.snapshot()['known_spent_usd']==pytest.approx(.00002)
    assert b'sensitive-test-value' not in next((tmp_path/'calls').glob('*.json')).read_bytes()
    assert opener.requests[0].full_url=='https://api.openai.com/v1/responses'
    body=json.loads(opener.requests[0].data)
    assert body['store'] is False and body['parallel_tool_calls'] is False
    assert body['truncation']=='disabled' and body['reasoning']=={'effort':'none'}


def test_restarted_client_recovers_cached_response_without_network(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    first=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=FakeOpener())
    generate(first)
    offline=FakeOpener(error=RuntimeError('must not call'))
    second=ResponsesClient(api_key='test',ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1),cache_dir=tmp_path/'calls',opener=offline)
    assert generate(second)['replayed'] and offline.calls==0


def test_uncertain_http_outcome_keeps_reservation_and_never_retries(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    opener=FakeOpener(error=urllib.error.URLError('secret-like server detail'))
    client=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=opener)
    with pytest.raises(AgentAPIError,match='no automatic retry') as err:generate(client)
    assert 'secret-like' not in str(err.value)
    assert ledger.snapshot()['unknown_reserved_usd']>0
    with pytest.raises(AgentAPIError,match='refused'):generate(client)
    assert opener.calls==1


def test_replay_refuses_changed_input_or_cache_content(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1);opener=FakeOpener()
    client=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=opener)
    generate(client)
    with pytest.raises(ContractError,match='identity'):generate(client,messages=[{'role':'user','content':'different'}])
    path=next((tmp_path/'calls').glob('*.json'));saved=read_json(path);saved['response']['output']=[];write_json(path,saved)
    with pytest.raises(ContractError,match='content'):generate(client)
    assert opener.calls==1


def test_missing_price_fields_remain_unknown(tmp_path):
    native=response();del native['usage']['input_tokens_details']['cache_write_tokens']
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    client=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=FakeOpener(native))
    with pytest.raises(AgentAPIError,match='halted'):generate(client)
    assert ledger.snapshot()['unknown_reserved_usd']>0 and ledger.snapshot()['halted']
    with pytest.raises(AgentAPIError,match='halted'):generate(client)


@pytest.mark.parametrize('model',['gpt-6-luna','gpt-6.1-terra'])
def test_reservation_accounts_for_history_and_tools(model):
    small=make_request(model,[{'role':'user','content':'x'}],[])
    large=make_request(model,[{'role':'user','content':'任务'*300}], [{'long_schema':'x'*1000}])
    assert reservation_estimate(large)>reservation_estimate(small)
    assert response_usage(response(model),model,512)[1]<reservation_estimate(small)


@pytest.mark.parametrize('mutation',[
    lambda r:r.update(model='unpriced-alias'),
    lambda r:r.update(service_tier='fast'),
    lambda r:r['usage'].update(input_tokens=True),
    lambda r:r['usage']['input_tokens_details'].update(cached_tokens=101),
])
def test_unsupported_usage_cannot_be_counted_as_known_cost(mutation):
    body=response();mutation(body)
    assert response_usage(body,'gpt-6-luna',512)[1] is None


def test_no_truncation_and_unpriced_model_before_payment():
    with pytest.raises(ContractError):make_request('unpriced',[],[])
    with pytest.raises(ContractError,match='context allowance'):make_request('gpt-6-luna',[{'role':'user','content':'x'*120001}],[])


def test_server_bound_breach_is_costed_and_halts_future_requests(tmp_path):
    body=response();body['usage'].update(output_tokens=513,total_tokens=613)
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    opener=FakeOpener(body);client=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=opener)
    with pytest.raises(AgentAPIError,match='output_exceeded'):generate(client)
    assert ledger.snapshot()['halted'] and ledger.snapshot()['known_spent_usd']>0
    from evalanchor.budget import BudgetError
    with pytest.raises(BudgetError):generate(client,request_key='new-request')
    assert opener.calls==1


def test_future_conversation_mutation_cannot_rewrite_earlier_request(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.db',cap_usd=.1)
    client=ResponsesClient(api_key='test',ledger=ledger,cache_dir=tmp_path/'calls',opener=FakeOpener())
    messages=[{'role':'user','content':'original'}];tools=[]
    receipt=generate(client,messages=messages,tools=tools)
    messages[0]['content']='changed';messages.append({'role':'assistant','content':'next turn'})
    tools.append({'type':'function','name':'new'})
    assert receipt['request']['input']==[{'role':'user','content':'original'}]
    assert receipt['request']['tools']==[]
    assert receipt['request_hash']==__import__('evalanchor.contracts',fromlist=['fingerprint']).fingerprint(receipt['request'])
