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

/// 连狗之前问的那一句。
///
/// **狗记下这个名字但不核实**（§6.3）。它不是登录：它是别人来接管控制权时
/// 屏上显示的那个名字，也是留痕里记的那个名字。所以宁可问一句，也不要拿
/// 空名字去连 —— 空名字在狗的账上是「未具名(ref)」，事后谁也说不清那一趟
/// 是谁开的。
///
/// **问过一次就落盘**（按狗一格，`RegistryStore.saveOperator`），重开 app
/// 也不再问。之后换班不走这个框，走屏上那枚常显的 chip：一点就换
/// （`OperatorChip`，人拍的板 3 的两条补偿）。
const String operatorAskTitle = '你是谁';

/// 那个输入框底下的解释。**不许写成「登录」。**
const String operatorAskHint = '记在狗的账上，狗不核实。别人来接管控制权时'
    '看到的就是这个名字。';

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
  static const Key operatorFieldKey = ValueKey<String>('roster-operator');

  @override
  State<RosterPage> createState() => _RosterPageState();
}

class _RosterPageState extends State<RosterPage> {
  RobotRegistry? _registry;
  Object? _loadError;

  @override
  void initState() {
    super.initState();
    _load();
  }

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
  /// 顺序是：钥匙 → 名字 → 解锁 → 进屏 → 回来收连接。
  ///
  /// **`who` 交给 [page] 是有意的**（裁决十六）：这里已经问过名字了，值守屏
  /// 那个记名确认（§5.3）要的就是同一个名字。让那一屏自己再去问一遍、或者
  /// 去狗身上取，都会得到另一个名字 —— 而狗那侧的 `operator` 是**记下但不
  /// 核实**的（§6.3）。
  ///
  /// **收连接不能省。** `PatrolClient` 自己造的那个 `HttpClient` 带着一个空闲
  /// 计时器；不收的话，人每进出一次就多挂一条，而狗那头那个 token 也一直有效。
  ///
  /// **但收连接的时机以前是错的（必修 2）。** 老写法是 `await push` 回来
  /// 就地 `client.close()`：`push` 返回的 future 在 `didPop` 那一刻就完成
  /// （退场动画刚开始），而被推上去那一屏的 `dispose()` 要等动画跑完才跑 ——
  /// 于是 `close(force: true)` 大概率发生在 `dispose()` **前面**，遥控屏临走
  /// 那一拍全零发给的是一个已经被强关的 `HttpClient`。退场那 300 毫秒里的
  /// 发拍/心跳/校准也跟着全部失败，屏上闪一串红字。
  ///
  /// 现在交给 [_ClientHolder]：它是那一屏的**祖先**，Flutter 拆树是自底向上
  /// 的，所以 `TeleopPage.dispose()` 一定排在它的 `dispose()` 前面。
  Future<void> _open(Robot r, PageBuilder page) async {
    final String pin = await widget.vault.read(r.sn) ?? '';
    if (!mounted) return;
    if (pin.isEmpty) {
      _toast('${r.label} $noPinHint');
      return;
    }
    final String? who = await _operatorFor(r);
    if (!mounted || who == null) return;

    final PatrolClient client = PatrolClient(r.baseUrl);
    try {
      await client.unlock(pin, operator: who);
    } catch (e) {
      // 异常原文只进日志：屏上写 `$e` 给不出下一步该干什么，还会把内部
      // 地址推到客户面前。
      developer.log('连 ${r.sn} 失败：$e', name: 'roster_page');
      client.close();
      if (mounted) _toast('${r.label}：$connectFailedHint');
      return;
    }
    if (!mounted) {
      client.close();
      return;
    }
    await Navigator.of(context).push<void>(MaterialPageRoute<void>(
        builder: (_) => _ClientHolder(
            client: client,
            child: page(client, who,
                (String name) => unawaited(_rememberFor(r, name))))));
  }

  /// 这只狗这次报什么名字。**落盘的那个直接用，不再问**（人拍的板 3）。
  ///
  /// 盘上没有才弹框问一句 —— 也就是每只狗**一辈子只问这一次**，之后换班靠
  /// 屏上那枚常显的 chip 一步改（`OperatorChip`）。这两件事是同一块板的两半：
  /// 记住是为了不烦人，一步切换是为了「不烦人」不至于变成「没人改」。
  ///
  /// 返回 `null` 表示人放弃了（按了「算了」，或者名字是空的）—— 那就不连。
  Future<String?> _operatorFor(Robot r) async {
    final String kept = await _readKept(r);
    if (!mounted) return null;
    if (kept.isNotEmpty) return kept;
    final TextEditingController ctl = TextEditingController();
    final String? asked = await showDialog<String>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text(operatorAskTitle),
        content: TextField(
          key: RosterPage.operatorFieldKey,
          controller: ctl,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: '名字',
            helperText: operatorAskHint,
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('算了'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, ctl.text.trim()),
            child: const Text('连上'),
          ),
        ],
      ),
    );
    ctl.dispose();
    if (asked == null) return null;
    if (asked.isEmpty) {
      // 空名字在狗的账上是「未具名(ref)」，事后谁也说不清那一趟是谁开的。
      if (mounted) _toast('填一个名字：狗的账上要记下是谁开的。');
      return null;
    }
    await _rememberFor(r, asked);
    return asked;
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
    // 条目没了钥匙还留着，是一份谁也管不到的残留。
    await widget.vault.forget(r.sn);
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

/// 替被推上去的那一屏拿着 `PatrolClient`，**在它之后**收掉（必修 2）。
///
/// 这个壳子存在的全部理由是时序。老写法是 `_RosterPageState._open` 里
/// `await push` 回来就地 `client.close()`，而那一刻被推上去那一屏还没
/// `dispose()`（退场动画才刚开始）—— 遥控屏临走那一拍全零于是发给了一个
/// 已经 `close(force: true)` 过的 `HttpClient`，一个字节都出不去。
///
/// 换成祖先之后，拆树的顺序（自底向上）就替我们把这件事定死了：
/// `TeleopPage.dispose()` → 这个壳子的 `dispose()` → `client.close()`。
/// **这条保证是 Flutter 的框架行为，不是一个凑出来的延时**，所以它不会在
/// 动画时长改了、机器慢了的那天悄悄失效。
///
/// 它顺带把另一个洞堵上了：老写法里 `await push` 要是根本不返回
/// （`RosterPage` 自己被销毁），那个 `client` 就漏在那儿了；壳子跟着路由走，
/// 路由没了它一定 `dispose()`。
///
/// **一个更干净的做法是把 `PatrolClient` 按狗缓存复用**（终审报告的建议
/// 11，顺带解决「每进一次屏就 `unlock` 一次、会话席位几下就占满」）。那件事
/// 改的是连接的归属和生命周期，横跨遥控/值守/盘况三屏，本轮留给第 9 卷。
class _ClientHolder extends StatefulWidget {
  const _ClientHolder({required this.client, required this.child});

  final PatrolClient client;
  final Widget child;

  @override
  State<_ClientHolder> createState() => _ClientHolderState();
}

class _ClientHolderState extends State<_ClientHolder> {
  @override
  void dispose() {
    // **温和地收，不硬关。** 遥控屏刚刚在它自己的 `dispose()` 里补了一拍
    // 全零（必修 2），硬关会把那一拍连同它那条 socket 一起掐掉 —— 排对了
    // 顺序却仍然发不出去，等于白排。已经在路上的那几条都带着自己的超时
    // （周期路径 3 秒，超时之后连接是真的被掐掉的），不会挂着不散。
    widget.client.close(force: false);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
