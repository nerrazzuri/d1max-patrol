/// 名册界面：我手上有哪几只狗。
///
/// **这是 app 的第一屏，也是唯一一屏离了机器还有意义的。** 别的界面都要连上
/// 某一只狗才说得出话；名册是"我要去哪只"的选单，机器一只都没开机它也得能用。
///
/// 存取全靠外面传进来（[RegistryStore] / [PinVault]），所以这一屏能在没有
/// 手机、没有插件的情况下整屏跑测试。
library;

import 'dart:async';
import 'dart:developer' as developer;

import 'package:flutter/material.dart';

import '../model/registry.dart';
import '../model/robot.dart';
import '../net/patrol_client.dart';
import '../store/pin_vault.dart';
import '../store/registry_store.dart';
import 'storage_page.dart';
import 'teleop_page.dart';
import 'watch_page.dart';
import 'widget/ask_operator.dart';

/// 名册里那条没存过 PIN 的狗，点进去时说的那一句。
///
/// **不拿空 PIN 去撞一次。** 空 PIN 换到的是一个 401，而报错会指向「PIN 不对」，
/// 人于是去翻本子找那个其实从来没填过的号。
const String noPinHint = '还没存 PIN。点开这一条填上，再进来。';

/// 连不上时说的那一句。**异常原文不上屏**，只进 `developer.log`。
const String connectFailedHint = '连不上。热点连对了吗，PIN 对吗';

/// 三个入口共用的那一套：拿到连接、这次报的名字、以及「换人了」往哪儿报。
///
/// 第三位不是可选的装饰：屏上那枚 chip 一点就换人（人拍的板 3 的第二条补偿），
/// 而**名字的家在名册这一屏**（按狗落盘）。不把这条回头路铺好，换完的名字
/// 就只活在那一屏的 `setState` 里 —— 退出去再进来又变回上一个人。
typedef PageBuilder = Widget Function(
    PatrolClient client, String who, ValueChanged<String> onOperatorChanged);

class RosterPage extends StatefulWidget {
  final RegistryStore store;
  final PinVault vault;

  const RosterPage({super.key, required this.store, required this.vault});

  /// 每一行上那三个入口。
  ///
  /// **三个入口走同一套写法**（[_RosterPageState._open]）：读 PIN、问名字、
  /// 解锁、`Navigator.push`、回来收连接。分几套写的话，迟早只有一边
  /// 记得收 `PatrolClient`。
  static Key teleopKeyFor(String sn) => ValueKey<String>('roster-teleop-$sn');
  static Key storageKeyFor(String sn) => ValueKey<String>('roster-storage-$sn');
  static Key watchKeyFor(String sn) => ValueKey<String>('roster-watch-$sn');

  /// 问名字那个框。
  ///
  /// **跟换班时点 chip 弹出来的是同一个框**（`widget/ask_operator.dart`），
  /// 所以也是同一个 Key。以前是两份各自写的框，规范化处理一有一无 ——
  /// 那正是终审报告必修 5 的根源。
  static const Key operatorFieldKey = operatorAskFieldKey;

  @override
  State<RosterPage> createState() => _RosterPageState();
}

class _RosterPageState extends State<RosterPage> {
  RobotRegistry? _registry;
  Object? _loadError;

  /// **一只狗一条连接，也就是一个会话**（终审报告建议 11）。按 SN 存。
  ///
  /// 老写法是每进一次屏就 `PatrolClient(r.baseUrl)` + `unlock` 一次。
  /// `client.close()` 只关本地 socket，**狗那头那个 token 一个都不会少**：
  /// `TokenStore` 的闲置期在热点通道上是 30 分钟（`AP_TOKEN_IDLE_S`），
  /// 而 §3.6 的非本机会话名额是个位数。人在名册屏上依次点值守、退出、
  /// 点盘况、退出、点遥控 —— 一台手机短时间内就把三个名额自己占光了。
  ///
  /// **同名换座救不了这件事。** `TokenStore.issue_with_quota` 只在
  /// `len(pool) >= cap` 那一刻才去找同名的老会话挤掉；名额还没满的时候
  /// 它照发不误，所以前三次进屏拿到的是三个各自独立的会话。第四个人
  /// （另一个名字）来的时候 `mine` 是空的 —— 直接 403。现场执行单
  /// 9.5/9.6/9.7 量的正是这三个名额和「第 4 个人被拒」，手机自己把座位
  /// 坐满的话，那三条量出来的数是假的。
  ///
  /// **复用还顺带把必修 2 那个时序问题连根拔了。** 以前退屏要收连接，
  /// 于是「收连接」和「遥控屏 `dispose()` 里补的那一拍全零」谁先谁后
  /// 就成了一件要专门搭个壳子（老的 `_ClientHolder`）去保证的事。现在
  /// 退屏根本不收连接 —— 那一拍发给的是一个照常活着的 `HttpClient`。
  ///
  /// 连接的归属因此上移到这一屏：只有 [dispose]（app 走人）和
  /// [_dropClient]（这条记录没了/换了一只狗）收它。
  final Map<String, PatrolClient> _clients = <String, PatrolClient>{};

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    // **温和地收，不硬关。** 这一屏被拆的时候，压在上面那一屏（遥控屏）
    // 可能刚在自己的 `dispose()` 里补了一拍全零（必修 2）；`force: true`
    // 会把那一拍连同它那条 socket 一起掐掉。已经在路上的那几条都带着
    // 自己的超时，不会挂着不散。
    for (final PatrolClient c in _clients.values) {
      c.close(force: false);
    }
    _clients.clear();
    super.dispose();
  }

  /// 这只狗的连接不要了：收掉、从缓存里划掉。
  ///
  /// **只在这条记录不再指向同一只狗的时候调**（从名册去掉、SN 改了）。
  /// 别拿它去「刷新一下连接」：`close()` 收的是本地这一头，狗那头那个
  /// token 照样占着名额，再 `unlock` 一次就是又烧掉一个座位。
  void _dropClient(String sn) => _clients.remove(sn)?.close(force: false);

  Future<void> _load() async {
    try {
      final reg = await widget.store.load();
      if (mounted) setState(() => _registry = reg);
    } catch (e) {
      // 名册读不出来要说出来，不能悄悄给一本空的 —— 那样人会以为自己
      // 从来没登记过，然后重新填一遍，而坏掉的文件还躺在盘上。
      if (mounted) setState(() => _loadError = e);
    }
  }

  Future<void> _persist() async {
    final reg = _registry;
    if (reg != null) await widget.store.save(reg);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('我的狗')),
      body: _body(),
      floatingActionButton: _registry == null
          ? null
          : FloatingActionButton(
              onPressed: _onAdd,
              tooltip: '登记一只新的',
              child: const Icon(Icons.add),
            ),
    );
  }

  Widget _body() {
    final err = _loadError;
    if (err != null) {
      return _Notice(
        icon: Icons.error_outline,
        title: '名册读不出来',
        detail: '$err\n\n盘上的文件还在，没有被覆盖。',
      );
    }
    final reg = _registry;
    if (reg == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (reg.isEmpty) {
      return const _Notice(
        icon: Icons.pets,
        title: '还没有登记过狗',
        detail: '每只 D1 Max 的内网地址都一样，靠地址分不出谁是谁，'
            '所以要按机身序列号（SN）一只只登记。\n\n'
            '右下角加一只。',
      );
    }
    return ListView.separated(
      itemCount: reg.length,
      separatorBuilder: (_, _) => const Divider(height: 1),
      itemBuilder: (context, i) => _tile(reg.all[i]),
    );
  }

  Widget _tile(Robot r) {
    return ListTile(
      key: ValueKey(r.sn),
      leading: const Icon(Icons.pets),
      title: Text(r.label),
      subtitle: Text(
        [
          if (r.ssid.isNotEmpty) '热点 ${r.ssid}' else '没记热点',
          if (r.provisional) '序列号是兜底的',
        ].join(' · '),
      ),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          IconButton(
            key: RosterPage.teleopKeyFor(r.sn),
            icon: const Icon(Icons.sports_esports_outlined),
            tooltip: '遥控',
            onPressed: () => unawaited(_open(
                r,
                (PatrolClient c, String who, ValueChanged<String> changed) =>
                    TeleopPage(
                        client: c,
                        robot: r,
                        operatorName: who,
                        onOperatorChanged: changed))),
          ),
          IconButton(
            key: RosterPage.storageKeyFor(r.sn),
            icon: const Icon(Icons.sd_storage_outlined),
            tooltip: '盘况',
            onPressed: () => unawaited(_open(
                r,
                // 盘况屏不显示、也不改操作员姓名 —— 后两位原样丢掉。
                (PatrolClient c, String _, ValueChanged<String> _) =>
                    StoragePage(client: c, robot: r))),
          ),
          IconButton(
            key: RosterPage.watchKeyFor(r.sn),
            icon: const Icon(Icons.monitor_heart_outlined),
            tooltip: '值守',
            // **`who` 一行传下去就够了**（裁决十六）：操作员姓名按计划就不在
            // 狗那侧，它在这一屏上（[_RosterPageState._operatorName]）。值守屏
            // 上那个记名确认（§5.3）带的就是它。
            onPressed: () => unawaited(_open(
                r,
                (PatrolClient c, String who, ValueChanged<String> changed) =>
                    WatchPage(
                        client: c,
                        robot: r,
                        operatorName: who,
                        onOperatorChanged: changed))),
          ),
          IconButton(
            icon: const Icon(Icons.delete_outline),
            tooltip: '从名册里去掉',
            onPressed: () => _onDelete(r),
          ),
        ],
      ),
      onTap: () => _onEdit(r),
    );
  }

  /// 进某一只狗的某一屏。**几个入口共用这一条路。**
  ///
  /// 顺序是：钥匙 → 名字 →（这只狗还没有连接的话）解锁 → 进屏。
  ///
  /// **`who` 交给 [page] 是有意的**（裁决十六）：这里已经问过名字了，值守屏
  /// 那个记名确认（§5.3）要的就是同一个名字。让那一屏自己再去问一遍、或者
  /// 去狗身上取，都会得到另一个名字 —— 而狗那侧的 `operator` 是**记下但不
  /// 核实**的（§6.3）。
  ///
  /// **落盘和上屏的都是狗收拾过的那一份（必修 5）。** 这头只 `trim()`；
  /// 狗那头（`app/identity.py::clean_operator`）还会把中间的连续空白压成
  /// 一个、把不可打印字符换成空格、超长截断。以前这儿把 `unlock` 回的整个
  /// [Session] 丢掉了 —— 人输「老  王」（中间两个空格），手机盘上、屏上署名
  /// chip、`signedAs()` 那一行、值守屏记名确认送出去的 `who` 全是「老  王」，
  /// 狗的审计环里从 unlock 那一刻起记的却是「老 王」。事后交接班查「这条 P1
  /// 是谁确认的」，两边字符串对不上，而这正是 §6.3 那套「记下但不核实」唯一
  /// 还剩下的对账手段。
  ///
  /// **退屏不再收连接**（建议 11）：连接按狗留着，见 [_clients]。
  Future<void> _open(Robot r, PageBuilder page) async {
    final String pin = await widget.vault.read(r.sn) ?? '';
    if (!mounted) return;
    if (pin.isEmpty) {
      _toast('${r.label} $noPinHint');
      return;
    }
    final String? asked = await _operatorFor(r);
    if (!mounted || asked == null) return;

    final PatrolClient client;
    final String who;
    final PatrolClient? kept = _clients[r.sn];
    if (kept != null) {
      // 这只狗已经有一个会话了。**不再 `unlock`** —— 那会再烧一个名额。
      client = kept;
      who = asked;
    } else {
      final PatrolClient made = PatrolClient(r.baseUrl);
      final Session session;
      try {
        session = await made.unlock(pin, operator: asked);
      } catch (e) {
        // 异常原文只进日志：屏上写 `$e` 给不出下一步该干什么，还会把内部
        // 地址推到客户面前。
        developer.log('连 ${r.sn} 失败：$e', name: 'roster_page');
        made.close();
        if (mounted) _toast('${r.label}：$connectFailedHint');
        return;
      }
      // **狗回的那个才是账上那个，以它为准**（必修 5）。狗没回名字
      // （老版本的狗、或者被强制门户截了）就还用人输的那份 —— 有一份
      // 能对的账，好过一份都没有。
      who = session.operator.isNotEmpty ? session.operator : asked;
      // **落盘排在解锁之后，不是之前。** 排在之前的话盘上留的是原字符串，
      // 而且每只狗一辈子只问这一次 —— 那份没规范化的名字会一直用下去。
      await _rememberFor(r, who);
      if (!mounted) {
        made.close();
        return;
      }
      _clients[r.sn] = made;
      client = made;
    }
    await Navigator.of(context).push<void>(MaterialPageRoute<void>(
        builder: (_) =>
            page(client, who, (String name) => unawaited(_rememberFor(r, name)))));
  }

  /// 这只狗这次报什么名字。**落盘的那个直接用，不再问**（人拍的板 3）。
  ///
  /// 盘上没有才弹框问一句 —— 也就是每只狗**一辈子只问这一次**，之后换班靠
  /// 屏上那枚常显的 chip 一步改（`OperatorChip`）。这两件事是同一块板的两半：
  /// 记住是为了不烦人，一步切换是为了「不烦人」不至于变成「没人改」。
  ///
  /// **框本身是共用的那一个**（`widget/ask_operator.dart`）：chip 点开的
  /// 是同一个。空名字那道守卫和那句提示也在里面，这儿不再抄一遍。
  ///
  /// 返回 `null` 表示人放弃了（按了「算了」，或者名字是空的）—— 那就不连。
  /// **回来的这个名字还没经狗规范化**，规范化在 [_open] 里接 `unlock` 的
  /// 回话（必修 5）。
  Future<String?> _operatorFor(Robot r) async {
    final String kept = await _readKept(r);
    if (!mounted) return null;
    if (kept.isNotEmpty) return kept;
    return askOperator(context, confirmLabel: '连上');
  }

  /// 盘上记着的那个名字。**读不动就当没记过** —— 再问一句而已，把人挡在
  /// 名册屏外面反而更坏。
  Future<String> _readKept(Robot r) async {
    try {
      return await widget.store.readOperator(dog: r.sn);
    } catch (e) {
      developer.log('读不到 ${r.sn} 上次是谁开的：$e', name: 'roster_page');
      return '';
    }
  }

  /// 记住这只狗此刻是谁在开。**按狗一格**（见 `RegistryStore.readOperator`）。
  ///
  /// **写不进去不打断人。** 名字已经在屏上、也已经跟着 `unlock` 发给狗了；
  /// 这一步只影响下次开 app 要不要再问一句。为它弹个错框，等于用一句人看
  /// 不懂的话去打断一次正在进行的出勤。
  Future<void> _rememberFor(Robot r, String name) async {
    try {
      await widget.store.saveOperator(dog: r.sn, name: name);
    } catch (e) {
      developer.log('记不住 ${r.sn} 这次是谁开的：$e', name: 'roster_page');
    }
  }

  Future<void> _onAdd() async {
    final draft = await showDialog<_Draft>(
      context: context,
      builder: (_) => const _RobotDialog(title: '登记一只狗'),
    );
    if (draft == null || !mounted) return;
    final reg = _registry!;
    if (reg.contains(draft.robot.sn)) {
      // 重复添加不静默覆盖 —— 覆盖掉的是已经调好的那条，而界面上看不出来。
      _toast('名册里已经有 ${draft.robot.sn} 了。想改就点那一条。');
      return;
    }
    reg.add(draft.robot);
    if (draft.pin.isNotEmpty) {
      await widget.vault.write(draft.robot.sn, draft.pin);
    }
    await _persist();
    if (mounted) setState(() {});
  }

  Future<void> _onEdit(Robot r) async {
    final pin = await widget.vault.read(r.sn) ?? '';
    if (!mounted) return;
    final draft = await showDialog<_Draft>(
      context: context,
      builder: (_) => _RobotDialog(title: '改一改', initial: r, initialPin: pin),
    );
    if (draft == null) return;
    final reg = _registry!;
    if (draft.robot.sn != r.sn) {
      // SN 改了等于换了一只狗。旧条目和旧钥匙都得跟着走，不能留下孤儿。
      reg.remove(r.sn);
      await widget.vault.forget(r.sn);
      // 那条连接是拿旧 SN 的 PIN 换来的，跟新条目不是一回事。
      _dropClient(r.sn);
    }
    reg.upsert(draft.robot);
    if (draft.pin.isEmpty) {
      await widget.vault.forget(draft.robot.sn);
    } else {
      await widget.vault.write(draft.robot.sn, draft.pin);
    }
    await _persist();
    if (mounted) setState(() {});
  }

  Future<void> _onDelete(Robot r) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('从名册去掉 ${r.label}？'),
        content: const Text('只动手机上的名册，不会对机器做任何事。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('算了'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('去掉'),
          ),
        ],
      ),
    );
    if (yes != true) return;
    _registry!.remove(r.sn);
    // 条目没了钥匙还留着，是一份谁也管不到的残留。连接也一样。
    await widget.vault.forget(r.sn);
    _dropClient(r.sn);
    await _persist();
    if (mounted) setState(() {});
  }

  void _toast(String text) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }
}

/// 对话框吐出来的东西：一条记录，外加它的 PIN（PIN 不进记录，见 `Robot.toJson`）。
class _Draft {
  final Robot robot;
  final String pin;
  const _Draft(this.robot, this.pin);
}

class _RobotDialog extends StatefulWidget {
  final String title;
  final Robot? initial;
  final String initialPin;

  const _RobotDialog({
    required this.title,
    this.initial,
    this.initialPin = '',
  });

  @override
  State<_RobotDialog> createState() => _RobotDialogState();
}

class _RobotDialogState extends State<_RobotDialog> {
  late final TextEditingController _sn;
  late final TextEditingController _name;
  late final TextEditingController _ssid;
  late final TextEditingController _pin;
  String? _snError;

  @override
  void initState() {
    super.initState();
    final r = widget.initial;
    _sn = TextEditingController(text: r?.sn ?? '');
    _name = TextEditingController(text: r?.name ?? '');
    _ssid = TextEditingController(text: r?.ssid ?? '');
    _pin = TextEditingController(text: widget.initialPin);
  }

  @override
  void dispose() {
    _sn.dispose();
    _name.dispose();
    _ssid.dispose();
    _pin.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.title),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: _sn,
              decoration: InputDecoration(
                labelText: '机身序列号 SN',
                helperText: '机器上跑 app 时用 D1MAX_SN 填的那个',
                errorText: _snError,
              ),
            ),
            TextField(
              controller: _name,
              decoration: const InputDecoration(
                labelText: '名字',
                helperText: '现场喊着方便，可以不填',
              ),
            ),
            TextField(
              controller: _ssid,
              decoration: const InputDecoration(
                labelText: '热点名 SSID',
                helperText: '形如 XG2WIFI_C40221',
              ),
            ),
            TextField(
              controller: _pin,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'PIN',
                helperText: '存进系统密钥库，不进名册文件',
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('算了'),
        ),
        TextButton(onPressed: _submit, child: const Text('存下')),
      ],
    );
  }

  void _submit() {
    final sn = _sn.text.trim();
    if (sn.isEmpty) {
      // 没有 SN 的条目等于"不知道是哪只"，留在名册里只会让人按错。
      setState(() => _snError = '没有 SN 就分不出是哪只狗');
      return;
    }
    final base = widget.initial ?? const Robot(sn: '');
    Navigator.pop(
      context,
      _Draft(
        base.copyWith(sn: sn, name: _name.text.trim(), ssid: _ssid.text.trim()),
        _pin.text.trim(),
      ),
    );
  }
}

class _Notice extends StatelessWidget {
  final IconData icon;
  final String title;
  final String detail;

  const _Notice({
    required this.icon,
    required this.title,
    required this.detail,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 48, color: theme.colorScheme.outline),
            const SizedBox(height: 16),
            Text(title, style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(
              detail,
              textAlign: TextAlign.center,
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}
