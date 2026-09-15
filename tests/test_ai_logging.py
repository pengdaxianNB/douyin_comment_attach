import asyncio
from unittest.mock import patch
import httpx
import pytest
from app.ai import AI


def run_request(body, status=200):
    logs=[]
    transport=httpx.MockTransport(lambda request:httpx.Response(status,json=body))
    real_client=httpx.AsyncClient
    with patch('app.ai.get_key',return_value='secret-test-key'), patch('app.ai.httpx.AsyncClient',side_effect=lambda **kw:real_client(transport=transport,**kw)):
        ai=AI({'base_url':'https://example.com/v1','model':'test','filter_prompt':'筛选'},logs.append)
        try:
            result=asyncio.run(ai.classify({'text':'想咨询','video_title':'介绍'}))
        except ValueError:
            result=None
    return result,'\n'.join(logs)


def test_success_logs():
    result,logs=run_request({'choices':[{'message':{'content':'{"target": true, "reason": "咨询需求"}'}}],'usage':{'total_tokens':32}})
    assert result==(True,'咨询需求')
    assert all(s in logs for s in ['开始','成功','客户语义筛选','想咨询','咨询需求','32','耗时='])


def test_http_error_redacts_credentials():
    result,logs=run_request({'error':'secret-test-key'},401)
    assert result is None
    assert '401' in logs and '失败' in logs and 'secret-test-key' not in logs


def test_invalid_result_not_success():
    result,logs=run_request({'choices':[{'message':{'content':'not json'}}]})
    assert result is None
    assert '失败' in logs and '成功' not in logs
