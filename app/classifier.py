import asyncio


async def classify_comments(db, ai, comments, concurrency, log, changed):
    """Bounded worker pool; failed/cancelled comments remain eligible next round."""
    pending = []
    seen = set()
    for comment in comments:
        ident = comment['id']
        if ident in seen:
            continue
        seen.add(ident)
        if not db.rows('SELECT id FROM comments WHERE id=?', (ident,)):
            pending.append(comment)
    if not pending:
        return
    limit = min(20, max(1, int(concurrency)))
    log(f'AI 评论并发开始：待处理 {len(pending)} 条，并发上限 {limit}')
    items = iter(pending)
    success = failed = 0

    async def worker():
        nonlocal success, failed
        for comment in items:
            try:
                target, reason = await ai.classify(comment)
            except Exception as exc:
                failed += 1
                log(f'评论 {comment["id"]} AI 识别失败，下轮重试：{exc}')
                continue
            # Database failures propagate, rather than being misreported as AI failures.
            db.ingest(comment, target, reason)
            success += 1
            changed()

    tasks = [asyncio.create_task(worker()) for _ in range(min(limit,len(pending)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        log(f'AI 评论并发结束：成功 {success} 条，失败 {failed} 条，未完成 {len(pending)-success-failed} 条')
