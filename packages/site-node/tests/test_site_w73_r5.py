# 站 是从值守测试借来的 pytest 夹具,各测试按参数名用它
# ruff: noqa: F401, F811, E501, E701, E702, E128, I001
"""W73 外审 r5 的复现(原样搬自外审的 /tmp/w73-r5/test_r5_findings.py,断言按「缺陷已修」写)。"""
import asyncio
import json
from types import SimpleNamespace as NS
from dataclasses import replace

from test_site_w73_guard_duty import 站, _方案, _跑, _patrols, MAP
from test_site_w73_r4 import _待命, _狗上线, _回程
from nexwar_site.battery_swap import SwapDesk


def swap(t, s):
    d = SwapDesk(t.site, now_ms=t.clock, need=lambda _:35., eta_min=lambda _:None,
                 has_charger=lambda _:False, home=s.swap_point)
    d.standby, d.alerts = s, t.duty.alerts
    return d


async def test_missing_offline_before_first_halt_must_not_invite(站):
    t = 站
    await t.run(12)
    s = _待命(t, 30)
    await t.send(s.return_to('A', issued_by='site:swap', charge=True))
    await t.run(25)
    d = swap(t, s)
    t.dog.inject_packs(None, 60.)
    await t.run(12)
    assert d.missing('A') == 1 and t.duty._running('A')
    t.broker.disconnect('dogA')
    await t.broker.drain()
    x = t.dog.x
    await t.send(d.tick())
    await t.run(40)
    asked = [a for a in d.alerts.open if a['kind']=='swap_needed']
    print('offline before first tick', d.s, 'asked=', asked, 'moved=', t.dog.x-x)
    assert not asked, '首次停车 tick 前断线也不能把未知运动当停车'


async def test_filtered_short_lap_not_blocked_by_original_route(站):
    t = 站
    _待命(t, 0)
    points = [{'name':n,'x':x,'y':0.,'yaw':0.} for n,x in [('A',2.),('B',4.),('C',800.)]]
    t.store.put_route('night-guard', {'map_id':MAP[0],'map_version':MAP[1], 'kind':'guard',
        'waypoints':points,'base_revision':1}, by='alice')
    t.areas.set(*MAP,'西区',[[799.,-2.],[801.,-2.],[801.,2.],[799.,2.]],by='alice')
    t.arming.set_zone_home('西区', False)
    t.arming.set_mode('home', by='olga')
    _方案(t)
    t.dog.inject_battery(80.)
    await t.run(12)
    e=t.duty.plan()['entries'][0]; info=t.duty._route(e['route'])
    pts=t.duty._lap_points(info,['东区'],None,False,False)
    print('filtered', [p['name'] for p in pts], 'original line=',t.duty._lap_need(e,info,'A'),
          'actual line=',_狗上线(t,info,e,pts))
    await _跑(t,8)
    print('patrols=',len(_patrols(t)), 'topup=',t.duty.topup_line('A'))
    assert _patrols(t), '实际短路线够电时不得先用原路线预算挡住'


async def test_unknown_home_low_energy_must_request_topup(站):
    t=站
    _待命(t,400,home=False)
    _方案(t)
    t.dog.inject_battery(40.)
    await t.run(12)
    e=t.duty.plan()['entries'][0];info=t.duty._route(e['route'])
    print('unknown home line=',t.duty._lap_need(e,info,'A'), 'dog=',_狗上线(t,info,e))
    await _跑(t,130)
    failures=[dict(r) for r in t.db.query("SELECT kind,data FROM events WHERE kind='task_failed'")]
    print('failed=',len(failures),'topup=',t.duty.topup_line('A'), 'patrols=',len(_patrols(t)))
    assert t.duty.topup_line('A') is not None, '未知原点不能退化为永远预飞失败且不补能'
    # 修法:以狗上的线为准 —— 拒了一次就记下狗上的出发线(51%),不再一直重派;补能要补过它
    assert t.duty.topup_line('A') >= 50.9
    assert len(failures) == 1, '狗上说电量不够出发,只拒一次,之后按它的线先补能'
    assert t.duty.resume_pct('A') >= t.duty.topup_line('A') + 10 - 1e-6
    assert not t.duty.alerts.has_open('A', 'guard_stuck'), '电量不够不是路被堵'


async def test_forecast_excludes_offline_candidate(站):
    t=站
    s=_待命(t,0)
    t.reg.enroll('B',fingerprint='sha256:b',issued_at=t.clock()-1,expires_at=t.clock()+10**10)
    s.set('B','default',map_id=MAP[0],map_version=MAP[1],x=800.,y=0.,yaw=0.,default=True)
    t.site.clients['B']=NS(status=NS(online=False))
    _方案(t, robot='')
    await t.run(12)
    e=t.duty.plan()['entries'][0]; info=t.duty._route(e['route'])
    chosen=t.duty._pick(e,info,set())
    f=t.duty.forecast()
    expected=t.duty._lap_s(info,(0.,0.))
    print('chosen=',chosen,'forecast=',f,'eligible lap=',expected)
    assert chosen[0]=='A'
    assert f['entries'][0]['lap_s']==expected, '不能把不在线的机器人当能挑的最远候选'


async def test_swap_terminal_old_status_should_release_halt(站):
    t=站
    await t.run(12)
    s=_待命(t,30)
    await t.send(s.return_to('A',issued_by='site:swap',charge=True))
    await t.run(25)
    t.dog.inject_packs(None,60.)
    await t.run(12)
    d=swap(t,s)
    old=t.site.clients['A'].status
    await t.send(d.tick())
    await t.run(15)
    assert d._terminal('A',old.task.task_id)
    current=t.site.clients['A'].status
    t.site.clients['A'].status=old
    await t.send(d.tick())
    print('terminal with old task snapshot=',d.s)
    t.site.clients['A'].status=current
    await t.send(d.tick())
    assert 'halt_tid' not in d.s['A']
    assert any(a['kind']=='swap_needed' for a in d.alerts.open)


async def test_standdown_foreign_terminal_status_must_still_return(站):
    from test_site_dispatcher import target
    t=站
    _待命(t,0)
    _方案(t)
    await _跑(t,3)
    await t.send(t.site.goto('A',target(1.),.8,issued_by='incident',priority=80,task_id='incident-r5'))
    await t.run(10)
    old=t.site.clients['A'].status
    assert old.task.task_id=='incident-r5'
    await t.run(100)
    assert t.duty._terminal('A','incident-r5')
    t.site.clients['A'].status=old
    for z in ['东区','西区']:t.arming.set_zone_home(z,False)
    t.arming.set_mode('home',by='olga')
    await t.send(t.duty.tick())
    print('foreign already terminal run=',t.duty.status()['runs'],'return=',_回程(t))
    await _跑(t,15)
    assert _回程(t), '别的任务已结束,旧快照不能当成已经接手而丢掉回程'


async def test_standdown_and_late_done_must_not_dispatch_twice(站, monkeypatch):
    t=站
    s=_待命(t,0)
    _方案(t)
    await _跑(t,8)
    tid=t.duty.status()['runs'][0]['task_id']
    held=[]
    publish=t.broker._publish
    def delay_done(msg,from_client):
        if '/event' in msg.topic:
            data=json.loads(msg.payload)
            if data.get('kind')=='task_done' and data.get('data',{}).get('task_id')==tid:
                held.append((msg,from_client))
                return
        return publish(msg,from_client)
    monkeypatch.setattr(t.broker,'_publish',delay_done)
    await t.run(600)
    print('held=',len(held),'running=',t.duty._running('A'), 'terminal=',t.duty._terminal('A',tid))
    assert held and not t.duty._running('A') and t.duty._terminal('A',tid) is None
    for z in ['东区','西区']:t.arming.set_zone_home(z,False)
    t.arming.set_mode('home',by='olga')
    await t.send(t.duty.tick())
    await t.run(20)
    before=[c for c in t.site.commands('A',500) if c['task_id'].startswith('standby-')]
    for msg,client in held:publish(msg,client)
    await t.broker.drain()
    await t.run(40)
    after=[c for c in t.site.commands('A',500) if c['task_id'].startswith('standby-')]
    print('standdown late done returns before=',before,'after=',after,'x=',t.dog.x)
    await s.close()
    assert len(after)==len(before), '收队已派回程,晚到的同一任务完成事件不能再派一次'


async def test_swap_pending_reconnect_empty_waits_and_releases(站):
    t=站
    await t.run(12)
    s=_待命(t,30)
    await t.send(s.return_to('A',issued_by='site:swap',charge=True))
    await t.run(25)
    t.dog.inject_packs(None,60.)
    await t.run(12)
    d=swap(t,s)
    async def reject(*a,**kw): return {'ack':{'result':'rejected'}}
    t.site.abort=reject
    await t.send(d.tick())
    offline=replace(t.site.clients['A'].status,online=False,task=None)
    t.site.clients['A'].status=offline
    await t.send(d.tick())
    t.site.clients['A'].status=replace(offline,online=True)
    await t.send(d.tick())
    assert d.s['A'].get('halt_tid') and not d.alerts.open
    t.clock.advance(5.1)
    await t.send(d.tick())
    assert 'halt_tid' not in d.s['A'] and any(a['kind']=='swap_needed' for a in d.alerts.open)


async def test_guard_and_swap_stop_do_not_restart_endlessly(站):
    t=站
    s=_待命(t,0)
    d=swap(t,s)
    t.duty.swap=d
    _方案(t)
    await _跑(t,3)
    t.dog.inject_packs(None,60.)
    await t.run(12)
    for z in ['东区','西区']:t.arming.set_zone_home(z,False)
    t.arming.set_mode('home',by='olga')
    for _ in range(20):
        await t.send(t.duty.tick())
        await t.send(d.tick())
        await t.run(10)
    print('guard+swap runs=',t.duty.status()['runs'],'swap=',d.s,'returns=',len(_回程(t)))
    assert not t.duty.status()['runs'] and not t.duty._running('A')
    assert len(_回程(t))<=1 and 'halt_tid' not in d.s['A']
    assert any(a['kind']=='swap_needed' for a in d.alerts.open)


async def test_reclaim_is_bounded_and_plain_manual_loss_not_reclaimed(站,monkeypatch):
    t=站
    await t.run(12)
    calls=[]
    async def denied():
        calls.append(t.clock())
        raise RuntimeError('Controlled denial of service (another master exists)')
    monkeypatch.setattr(t.dog,'acquire_control',denied)
    await t.dog.release_control()
    await t.run(100)
    assert not calls, '没有 TCP 断线的人工接管不进入重拿流程'
    await t.dog.close()
    await t.dog.emergency_stop(True)
    await t.run(500)
    n=len(calls)
    await t.run(100)
    h=await t.dog.health()
    print('bounded reclaim attempts=',n,'left=',t.agent._reclaim_left,'health=',h)
    assert n==7 and len(calls)==n and h.estop and not h.control
