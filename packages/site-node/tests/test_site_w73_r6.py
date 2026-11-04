"""W73 外审 r6 的复现(原样搬进来):①还在等回执的手动回程被拒后,自动回程要补上;
②狗上的线高于 95% 时补能要补过它;③狗上的线只对同一份预算上下文算数。"""
# ruff: noqa: F401, F811, E501, I001
import asyncio
import json
from dataclasses import replace

import pytest
from test_site_w73_guard_duty import 站, _方案, _跑, _patrols, MAP
from test_site_w73_r4 import _待命, _狗上线, _回程
from test_site_w73_r5 import swap
from test_site_dispatcher import target
from nexwar_contract.messages import Event


async def test_dog_line_above_resume_target(站):
    t = 站
    s = _待命(t, 1100, home=False)
    _方案(t)
    t.dog.inject_battery(80.)
    await t.run(12)
    e = t.duty.plan()['entries'][0]
    info = t.duty._route(e['route'])
    print('BEFORE site/dog=', t.duty._lap_need(e, info, 'A'), _狗上线(t, info, e))
    await _跑(t, 12)
    print('LEARNED=', t.duty._dog_need, 'line=', t.duty.topup_line('A'), 'resume=', t.duty.resume_pct('A'))
    assert t.duty._dog_need
    t.dog.inject_battery(t.duty.resume_pct('A'))
    n = len(_patrols(t))
    await _跑(t, 10)
    print('AFTER resume charge line=', t.duty.topup_line('A'), 'resume=', t.duty.resume_pct('A'), 'patrols=', len(_patrols(t)))
    await s.close()
    assert len(_patrols(t)) > n, '补到 resume_pct 以后应能恢复值守'


async def test_dog_line_invalidated_when_standby_changes(站):
    t = 站
    s = _待命(t, 1200, home=False)
    _方案(t)
    t.dog.inject_battery(80.)
    await t.run(12)
    await _跑(t, 12)
    print('LEARNED OLD=', t.duty._dog_need)
    assert t.duty._dog_need
    s.set('A', 'default', map_id=MAP[0], map_version=MAP[1], x=0., y=0., yaw=0., default=True)
    t.dog.inject_battery(100.)
    await t.run(12)
    e = t.duty.plan()['entries'][0]
    info = t.duty._route(e['route'])
    print('NEW site/dog=', t.duty._lap_need(e, info, 'A'), _狗上线(t, info, e))
    n = len(_patrols(t))
    await _跑(t, 20)
    print('AFTER change patrols=', len(_patrols(t)), 'line=', t.duty.topup_line('A'))
    await s.close()
    assert len(_patrols(t)) > n, '换成短的可执行路线后不能仍按旧线锁住'


async def test_swap_boot_idle_and_freshness_gaps_release(站):
    t = 站
    s = _待命(t, 0)
    t.dog.inject_packs(None, 60.)
    await t.run(12)
    d = swap(t, s)
    await t.send(d.tick())
    assert not d.alerts.open
    # 先出现一次过期状态，然后恢复正常新状态。
    t.clock.advance(t.site.stale_ms / 1000 + 1)
    await t.send(d.tick())
    assert not d.alerts.open
    for _ in range(8):
        await t.run(10)
        await t.send(d.tick())
    print('NORMAL swap=', d.s, 'alerts=', d.alerts.open)
    await s.close()
    assert any(a['kind'] == 'swap_needed' for a in d.alerts.open)


@pytest.mark.parametrize('dedup_enabled', [True, False])
async def test_pending_manual_return_rejected_after_done_loses_return(站, monkeypatch, dedup_enabled):
    t = 站
    await t.run(12)
    s = _待命(t, 0)
    s.hold = None
    if not dedup_enabled:
        monkeypatch.setattr(s, '_already_back', lambda *a: False)
    held = []
    publish = t.broker._publish
    def delay_rejected_ack(msg, from_client):
        if '/ack' in msg.topic:
            data = json.loads(msg.payload)
            if data.get('result') == 'rejected' and data.get('reason') == 'busy':
                held.append((msg, from_client))
                return
        return publish(msg, from_client)
    monkeypatch.setattr(t.broker, '_publish', delay_rejected_ack)
    first = await t.send(t.site.goto('A', target(2.), .8, issued_by='alice', task_id='r6-work'))
    await t.run(12)
    pending = asyncio.create_task(s.return_to('A', issued_by='alice'))
    await t.broker.drain()
    for _ in range(15):
        await asyncio.sleep(0)
        await t.broker.drain()
    assert held and not pending.done()
    for _ in range(90):
        await t.run(1)
        if t.duty._terminal('A', first['task_id']):
            break
    for _ in range(20):
        await asyncio.sleep(0)
        await t.broker.drain()
    original_return = next(c for c in _回程(t) if c['issued_by'] == 'alice')
    delay_ms = t.clock() - original_return['issued_at']
    print('ACK DELAY ms=', delay_ms)
    assert delay_ms < t.site.ack_timeout_s * 1000, '窗口必须短于真实回执超时'
    print('AT DONE x=', t.dog.x, 'commands=', _回程(t), 'already=', s._already_back('A', first['task_id']))
    for msg, client in held:
        publish(msg, client)
    await t.broker.drain()
    await pending
    await t.run(100)
    print('AFTER rejected x=', t.dog.x, 'commands=', _回程(t))
    await s.close()
    assert any(c['ack_result'] == 'accepted' for c in _回程(t)), '未接收的手动回程不能吃掉正常完成后的自动回程'


# r6 #1:没回执、这个进程里也不在等的(站点重启前发的)不算回了
@pytest.mark.parametrize('ack, expected', [(None, ''), ('accepted', 'back'), ('duplicate', 'back'),
                                          ('timeout', ''), ('rejected', ''), ('expired', '')])
async def test_already_back_ack_matrix_and_insertion_order(站, ack, expected):
    t = 站
    s = _待命(t, 0)
    def add(cid, tid, robot='A', result='accepted', stamp=10):
        with t.db.tx() as tx:
            tx.execute('INSERT INTO commands(command_id,task_id,robot_id,kind,payload,issued_by,issued_at,ack_result) VALUES(?,?,?,?,?,?,?,?)',
                       (cid, tid, robot, 'goto', '{}', 'alice', stamp, result))
    add('early', 'standby-early', stamp=999)
    add('source', 'sched-source', stamp=100)
    add('other', 'standby-other', robot='B')
    assert s._already_back('A', 'sched-source') == ''
    add('late', 'standby-late', result=ack, stamp=1)
    assert s._already_back('A', 'sched-source') == expected
    await s.close()


async def test_need_event_serialization_and_success_clears_memory(站):
    t = 站
    s = _待命(t, 400, home=False)
    _方案(t)
    t.dog.inject_battery(40.)
    await t.run(12)
    await _跑(t, 12)
    need = t.duty._dog_need['A'][0]
    events = [e for e in t.site.recent_events('A', 500) if e['kind'] == 'task_failed']
    assert len(events) == 1
    assert events[0]['data']['need_pct'] == need
    e = Event('r6-event', 1, 'r6-boot', t.clock(), 'task_failed', events[0]['data'])
    assert Event.from_wire(json.loads(json.dumps(e.to_wire()))).data == e.data
    t.dog.inject_battery(t.duty.resume_pct('A'))
    await _跑(t, 5)
    assert len(_patrols(t)) == 2
    assert t.duty.topup_line('A') is None
    # 让真实代理跑完这次包含 400 米尾段的任务，验证成功后清缓存。
    await _跑(t, 440)
    print('AFTER success dog_need=', t.duty._dog_need)
    assert 'A' not in t.duty._dog_need
    await s.close()


async def test_lap_cache_distinguishes_subsets_and_orders(站):
    t = 站
    _方案(t)
    info = t.duty._route('night-guard')
    a, b, c, d = info['points']
    vals = [t.duty._lap_s(info, (0., 0.), pts) for pts in ([a,b], [c,d], [a,d,b])]
    print('SUBSETS=', vals, 'keys=', list(t.duty._lap_cache))
    assert vals[0] != vals[1] and vals[1] != vals[2]
    assert t.duty._lap_s(info, (0.,0.)) == t.duty._lap_s(info, (0.,0.), info['points'])


async def test_r6_2_满电也不够_直说_不去补能(站):
    """线 > 100%:补能没用 —— 不记「先补能」,等的原因直说预算不够;也不派。"""
    t = 站
    s = _待命(t, 9000, home=True)
    _方案(t)
    t.dog.inject_battery(100.)
    await t.run(12)
    e = t.duty.plan()['entries'][0]
    info = t.duty._route(e['route'])
    assert t.duty._lap_need(e, info, 'A')[0] > 100.0
    n = len(_patrols(t))
    await _跑(t, 10)
    why = t.duty._why_not('A', info, e)
    await s.close()
    assert '满电也不够' in why
    assert t.duty.topup_line('A') is None, '不该叫它去补能'
    assert len(_patrols(t)) == n
