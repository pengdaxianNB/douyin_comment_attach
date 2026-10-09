import json

import pytest

from app.browser import search_response_payloads,search_result_videos


def framed(chunks):
    return b''.join(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n' for chunk in chunks)+b'0\r\n\r\n'


def test_medical_stream_reads_works_after_advice_and_ack_using_utf8_byte_lengths():
    packets=[{'status_code':0,'data':[{'type':83,'common_aladdin':{
                  'display':json.dumps({'title':'专家建议','content':'内容来自小荷医典'},ensure_ascii=False)}}]},
             {'time_cost':{'1st_screen_state':'hit_predict'},'ack':1},
             {'status_code':0,'data':[{'type':1,'aweme_info':{
                  'aweme_id':'1234567890123456789','desc':'实际作品'}}]}]
    body=framed([json.dumps(packet,ensure_ascii=False).encode() for packet in packets])

    assert search_response_payloads(body)==packets
    videos=[item for packet in search_response_payloads(body) for item in search_result_videos(packet)]
    assert [item['aweme_id'] for item in videos]==['1234567890123456789']


def test_chunk_boundaries_can_split_unicode_and_json_or_include_multiple_records():
    packets=[{'data':[{'aweme_info':{'aweme_id':'1234567890123456789','desc':'图文'}}]},
             {'data':[],'status_code':0}]
    text='\n'.join(json.dumps(packet,ensure_ascii=False) for packet in packets).encode()
    split=text.index('图'.encode())+1
    assert search_response_payloads(framed([text[:split],text[split:]]))==packets


def test_regular_json_body_still_works_with_unicode():
    packet={'status_code':0,'data':[{'aweme_info':{'aweme_id':'1234567890123456789','desc':'视频'}}]}
    assert search_response_payloads(json.dumps(packet,ensure_ascii=False))==[packet]


@pytest.mark.parametrize('body',[
    b'',b'<html>error</html>',b'2\r\n{}\r\n',b'3\r\n{}\r\n0\r\n\r\n',
    b'2\r\n{}\r\n0\r\n',framed([b'{}',b'{"data":']),
])
def test_malformed_or_truncated_stream_is_not_accepted_as_success(body):
    with pytest.raises(ValueError):
        search_response_payloads(body)
