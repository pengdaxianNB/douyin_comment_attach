import asyncio
from types import SimpleNamespace
from app.classifier import classify_comments
from app.db import DB


def comments(n):
    return [dict(id=str(i),uid='MS4wLjABAAAAcustomer_'+str(i),name='客户',text='咨询',comment_time='',video_id='v',video_title='',blogger='') for i in range(n)]


def test_bounded_dedup_and_failure(tmp_path):
    async def run():
        db=DB(tmp_path/'db.sqlite3'); data=comments(10)
        db.ingest(data[0],False,'已处理')
        active=peak=0; calls=[]; logs=[]
        async def classify(c):
            nonlocal active,peak
            active+=1; peak=max(peak,active); calls.append(c['id'])
            try:
                await asyncio.sleep(.001)
                if c['id']=='3': raise ValueError('超时')
                return True,'需求'
            finally: active-=1
        await classify_comments(db,SimpleNamespace(classify=classify),data+data,3,logs.append,lambda:None)
        assert peak==3 and len(calls)==9 and len(set(calls))==9
        assert len(db.rows('SELECT * FROM comments'))==9
        assert not db.rows('SELECT * FROM comments WHERE id=?',('3',))
        assert '失败 1 条' in logs[-1]
    asyncio.run(run())


def test_cancel_drains_workers(tmp_path):
    async def run():
        db=DB(tmp_path/'db.sqlite3'); active=0; started=asyncio.Event()
        async def classify(c):
            nonlocal active
            active+=1
            if active==3: started.set()
            try: await asyncio.Event().wait()
            finally: active-=1
        task=asyncio.create_task(classify_comments(db,SimpleNamespace(classify=classify),comments(10),3,lambda x:None,lambda:None))
        await started.wait(); task.cancel()
        result=await asyncio.gather(task,return_exceptions=True)
        assert isinstance(result[0],asyncio.CancelledError)
        assert active==0 and not db.rows('SELECT * FROM comments')
    asyncio.run(run())
