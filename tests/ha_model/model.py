"""站点热备的控制权模型(docs/热备与HA设计.md,RC 加固 H11)。

**这不是实现**,是把设计稿里的规矩写成能跑的最小模型,好让「不变式」和脑裂场景能当测试钉住。
实现的时候,同一批场景要在仿真狗上原样再做一遍。

三样东西:
- :class:`Robot`:记见过的最大任期(落盘的那一份、内存里的那一份分开,好演「没落盘就重启」);
  收命令的规矩;声光 45 秒自己关;**狗上在跑的运动任务**(保持距离:不靠站点续,到自己的时限才停,
  时限封顶 :data:`STANDOFF_MAX_S`)—— RC 加固外审 I3:原来的模型只有声光,证明不了「狗原地等」。
- :class:`Site`:一台站点主机。``leader`` / ``standby`` / ``deposed`` 三个状态;任期只在升主时加一。
  狗在跑、自己库里没有记录的运动任务:不续、不撤,这台狗算占用到任务结束(决策 62);人下的叫停照发。
- :class:`World`:谁连得到谁、时间、两台主机之间的数据复制。
"""

from __future__ import annotations

from dataclasses import dataclass, field

KEEP_S = 45                     # 声光每次开只管这么久(W22)
STANDOFF_MAX_S = 900            # 保持距离最长跑这么久(W25,契约里封顶)
#: 人下的、任何时候都照发的命令(叫停);别的命令不发给占用着的狗。
HUMAN_OVERRIDE = ("stop",)


@dataclass(frozen=True)
class Command:
    site_epoch: int
    leader_id: str
    kind: str                   # "move" / "stop" / "outputs_on" / "standoff"
    command_id: str
    owner: str = ""             # 这条命令是替库里哪一条记录发的(驱离会话、派单)
    max_s: float = 0.0          # standoff:最多跑多久


@dataclass
class Ack:
    accepted: bool
    why: str = ""
    seen_epoch: int = 0         # 狗见过的最大任期(「站点过期」时带回去)


@dataclass
class Robot:
    name: str
    disk_epoch: int = 0         # 落盘的:重启后只剩它
    mem_epoch: int = 0
    executed: list[Command] = field(default_factory=list)
    outputs_until: float = -1.0
    #: 狗上在跑的运动任务:(种类, 替谁跑的, 最迟什么时候停)。不靠站点续。
    task: tuple[str, str, float] | None = None
    persist_before_execute: bool = True     # 设成假 = 故意做错,演不变式 3 为什么要有

    def receive(self, cmd: Command, now: float) -> Ack:
        seen = max(self.disk_epoch, self.mem_epoch)
        if cmd.site_epoch < seen:
            return Ack(False, "stale_site", seen)
        if cmd.site_epoch > seen:
            self.mem_epoch = cmd.site_epoch
            if self.persist_before_execute:
                self.disk_epoch = cmd.site_epoch
        self.executed.append(cmd)
        if cmd.kind == "outputs_on":
            self.outputs_until = now + KEEP_S
        elif cmd.kind == "standoff":
            self.task = ("standoff", cmd.owner, now + min(cmd.max_s, STANDOFF_MAX_S))
        elif cmd.kind in ("stop", "move"):
            self.task = None                # 叫停、派了别的活:原来那个任务结束
        return Ack(True, "", max(self.disk_epoch, self.mem_epoch))

    def running(self, now: float) -> tuple[str, str, float] | None:
        """此刻在跑的运动任务(到时限就自己停了)。断不断站点都一样。"""
        return self.task if self.task is not None and now < self.task[2] else None

    def see_leader_marker(self, epoch: int) -> bool:
        """连上 broker 先看保留的 site/leader:比我记的小就不连(回假)。"""
        if epoch < max(self.disk_epoch, self.mem_epoch):
            return False
        self.mem_epoch = self.disk_epoch = max(epoch, self.disk_epoch)
        return True

    def reboot(self) -> None:
        self.mem_epoch = self.disk_epoch

    def outputs_on(self, now: float) -> bool:
        return now < self.outputs_until


class PromoteRefused(RuntimeError):
    pass


@dataclass
class Site:
    leader_id: str
    state: str = "standby"      # leader / standby / deposed
    epoch: int = 0              # 库里的任期(跟着数据复制)
    heard_epoch: int = 0        # 听说过的最大任期(对方、狗的回执、broker 上的标记)
    log: list[str] = field(default_factory=list)       # 库里的事(意图、告警、会话)
    synced_from: int = 0        # 备机:收到主的第几条了
    sent: list[Command] = field(default_factory=list)
    #: 占用着的狗:在跑一个我库里没有记录的运动任务 → 它最迟什么时候结束(值班台上要显示)。
    busy: dict[str, float] = field(default_factory=dict)
    _n: int = 0

    # ---- 发命令
    def issue(self, kind: str, *, robot: str = "", owner: str = "",
              max_s: float = 0.0) -> Command | None:
        """只有主发。备机、下台的一条都不发(不变式 6、7)。占用着的狗只发人下的叫停(不变式 9)。"""
        if self.state != "leader":
            return None
        if robot in self.busy and kind not in HUMAN_OVERRIDE:
            return None
        self._n += 1
        cmd = Command(self.epoch, self.leader_id, kind, f"{self.leader_id}-{self.epoch}-{self._n}",
                      owner, max_s)
        self.sent.append(cmd)
        return cmd

    def reconcile(self, robot: str, running: tuple[str, str, float] | None) -> None:
        """狗报它在跑什么(连上时、任务结束时)。在跑、替它跑的那条记录我库里没有:不续、不撤,
        这台狗占用到任务结束。没在跑、或者是我认识的:不占用。"""
        if running is not None and running[1] not in self.log:
            self.busy[robot] = running[2]
        else:
            self.busy.pop(robot, None)

    def broker_open(self) -> bool:
        return self.state == "leader"

    # ---- 知道了更大的任期
    def learn(self, epoch: int) -> None:
        self.heard_epoch = max(self.heard_epoch, epoch)
        if self.state == "leader" and self.heard_epoch > self.epoch:
            self.state = "deposed"

    def on_ack(self, ack: Ack) -> None:
        if not ack.accepted and ack.why == "stale_site":
            self.learn(ack.seen_epoch)

    def restart(self) -> None:
        """进程重启:状态和任期都在盘上,不会因为重启又变回主。"""
        if self.heard_epoch > self.epoch and self.state == "leader":
            self.state = "deposed"

    # ---- 升主、回来当备机
    def promote(self, *, old_leader_is_off: bool, accept_stale_data: bool = False,
                peer_position: int | None = None) -> None:
        if self.state == "leader":
            raise PromoteRefused("已经是主")
        if self.state == "deposed":
            raise PromoteRefused("下台的要先重新同步(rejoin)再说")
        if not old_leader_is_off:
            raise PromoteRefused("没确认旧主已经断开")
        if self.heard_epoch > self.epoch:
            raise PromoteRefused("手上的数据不是最新任期的")
        if peer_position is not None and self.synced_from < peer_position \
                and not accept_stale_data:
            raise PromoteRefused("数据落后于主:要加确认才升(会丢这一段)")
        self.epoch += 1
        self.heard_epoch = self.epoch
        self.state = "leader"

    def rejoin(self, leader: Site) -> list[str]:
        """下台的旧主回来当备机:分叉的那些导出给人看,整份换成新主的。回分叉的条目。"""
        if self.state == "leader":
            raise PromoteRefused("还是主,不能当备机")
        diverged = [x for x in self.log if x not in leader.log]
        self.log = list(leader.log)
        self.synced_from = len(leader.log)
        self.epoch = self.heard_epoch = leader.epoch
        self.state = "standby"
        return diverged


@dataclass
class World:
    a: Site
    b: Site
    robots: dict[str, Robot]
    now: float = 0.0
    cut: set[frozenset[str]] = field(default_factory=set)

    @classmethod
    def fresh(cls, *robots: str) -> World:
        a, b = Site("a", state="leader", epoch=1, heard_epoch=1), Site("b", epoch=1, heard_epoch=1)
        return cls(a, b, {r: Robot(r) for r in robots})

    def site(self, name: str) -> Site:
        return self.a if name == "a" else self.b

    def link(self, x: str, y: str) -> bool:
        return frozenset((x, y)) not in self.cut

    def partition(self, x: str, y: str) -> None:
        self.cut.add(frozenset((x, y)))

    def heal(self) -> None:
        self.cut.clear()

    def power_off(self, site: str) -> None:
        for other in ("a", "b", *self.robots):
            if other != site:
                self.partition(site, other)

    def write(self, site: str, what: str, *, sync: bool = False) -> bool:
        """主写一笔。``sync``:要等备机收到才算成(P1 告警、驱离会话、命令意图);备机够不着就退成
        只写本机(回假 = 没同步到)。"""
        s = self.site(site)
        other = self.b if s is self.a else self.a
        s.log.append(what)
        if sync and self.link("a", "b") and other.state == "standby":
            other.log = list(s.log)
            other.synced_from = len(s.log)
            other.epoch = max(other.epoch, s.epoch) if other.state == "standby" else other.epoch
            return True
        return False

    def replicate(self) -> None:
        """异步复制追一次(连得通才行)。"""
        for s, other in ((self.a, self.b), (self.b, self.a)):
            if s.state == "leader" and other.state == "standby" and self.link("a", "b"):
                other.log = list(s.log)
                other.synced_from = len(s.log)
                other.epoch = other.heard_epoch = max(other.epoch, s.epoch)

    def send(self, site: str, robot: str, kind: str, *, owner: str = "",
             max_s: float = 0.0) -> Ack | None:
        """站点给狗发一条。连不通、不是主、狗占用着:什么都没发生(回 None)。"""
        s = self.site(site)
        if not self.link(site, robot) or not s.broker_open():
            return None
        r = self.robots[robot]
        if not r.see_leader_marker(s.epoch):
            ack = Ack(False, "stale_site", max(r.disk_epoch, r.mem_epoch))
            s.on_ack(ack)
            return ack
        s.reconcile(robot, r.running(self.now))     # 连得上:狗报它此刻在跑什么
        cmd = s.issue(kind, robot=robot, owner=owner, max_s=max_s)
        if cmd is None:
            return None
        ack = r.receive(cmd, self.now)
        s.on_ack(ack)
        if ack.accepted:
            s.reconcile(robot, r.running(self.now))
        return ack

    def gossip(self) -> None:
        """两台主机连得通时互相问一下任期。"""
        if self.link("a", "b"):
            top = max(self.a.epoch, self.b.epoch)
            self.a.learn(top)
            self.b.learn(top)

    def leaders(self) -> list[str]:
        return [s.leader_id for s in (self.a, self.b) if s.state == "leader"]
