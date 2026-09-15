import asyncio
import json
from unittest.mock import patch
import httpx
import pytest
from app.ai import AI,thinking_parameters


@pytest.mark.parametrize('effort',['low','high','max'])
def test_effort(effort):
    assert thinking_parameters({'thinking_mode':'enabled','thinking_effort':effort})=={'thinking':{'type':'enabled'},'reasoning_effort':effort}


def test_default_disabled_invalid():
    assert thinking_parameters({})=={}
    assert thinking_parameters({'thinking_mode':'disabled'})=={'thinking':{'type':'disabled'}}
    with pytest.raises(ValueError):thinking_parameters({'thinking_effort':'bad'})


def test_request_body():
    captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'}}]})
    client=httpx.AsyncClient
    with patch('app.ai.get_key',return_value='test'),patch('app.ai.httpx.AsyncClient',side_effect=lambda **kw:client(transport=httpx.MockTransport(handler),**kw)):
        ai=AI({'base_url':'https://api.deepseek.com','model':'deepseek-flash','thinking_mode':'enabled','thinking_effort':'max'})
        assert asyncio.run(ai.call('test',{}))=='ok'
    assert captured[0]['thinking']=={'type':'enabled'}
    assert captured[0]['reasoning_effort']=='max'
    assert 'temperature' not in captured[0]
