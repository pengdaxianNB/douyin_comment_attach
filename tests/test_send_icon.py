from app.browser import is_send_icon


def test_pink_composer_icon_only():
    editor=dict(right=1200,bottom=800)
    candidate=dict(rect=dict(x=1170,y=785,width=32,height=32),rgb=[254,44,85],enabled=True,clickable=True,has_icon=True)
    assert is_send_icon(candidate,editor)
    assert not is_send_icon(dict(candidate,enabled=False),editor)
    assert not is_send_icon(dict(candidate,rgb=[140,140,140]),editor)
    assert not is_send_icon(dict(candidate,rect=dict(x=1170,y=200,width=32,height=32)),editor)
    assert not is_send_icon(dict(candidate,rect=dict(x=1100,y=785,width=100,height=32)),editor)
    assert not is_send_icon(dict(candidate,has_icon=False),editor)
