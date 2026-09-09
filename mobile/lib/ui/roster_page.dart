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

/// 连狗之前问的那一句。
///
/// **狗记下这个名字但不核实**（§6.3）。它不是登录：它是别人来接管控制权时
/// 屏上显示的那个名字，也是留痕里记的那个名字。所以宁可问一句，也不要拿
/// 空名字去连 —— 空名字在狗的账上是「未具名(ref)」，事后谁也说不清那一趟
/// 是谁开的。
///
/// 问过一次就记在这一屏上，同一次开着 app 不再重复问。
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

class RosterPage extends StatefulWidget {
  final RegistryStore store;
  final PinVault vault;

  const RosterPage({super.key, required this.store, required this.vault});

  /// 每一行上那两个入口。
  ///
  /// **两个入口走同一套写法**（[_RosterPageState._open]）：读 PIN、问名字、
  /// 解锁、`Navigator.push`、回来收连接。分两套写的话，两边迟早只有一边
  /// 记得收 `PatrolClient`。
  static Key teleopKeyFor(String sn) => ValueKey<String>('roster-teleop-$sn');
  static Key storageKeyFor(String sn) => ValueKey<String>('roster-storage-$sn');

  /// 问名字那个框。
  static const Key operatorFieldKey = ValueKey<String>('roster-operator');

  @override
  State<RosterPage> createState() => _RosterPageState();
}

class _RosterPageState extends State<RosterPage> {
  RobotRegistry? _registry;
  Object? _loadError;

  /// 这次开着 app 报的名字。问过一次就不再问。
  String _operator = '';

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
            onPressed: () => unawaited(
                _open(r, (PatrolClient c) => TeleopPage(client: c, robot: r))),
          ),
          IconButton(
            key: RosterPage.storageKeyFor(r.sn),
            icon: const Icon(Icons.sd_storage_outlined),
            tooltip: '盘况',
            onPressed: () => unawaited(
                _open(r, (PatrolClient c) => StoragePage(client: c, robot: r))),
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

  /// 进某一只狗的某一屏。**两个入口共用这一条路。**
  ///
  /// 顺序是：钥匙 → 名字 → 解锁 → 进屏 → 回来收连接。
  ///
  /// **收连接不能省。** `PatrolClient` 自己造的那个 `HttpClient` 带着一个空闲
  /// 计时器；不收的话，人每进出一次就多挂一条，而狗那头那个 token 也一直有效。
  Future<void> _open(Robot r, Widget Function(PatrolClient) page) async {
    final String pin = await widget.vault.read(r.sn) ?? '';
    if (!mounted) return;
    if (pin.isEmpty) {
      _toast('${r.label} $noPinHint');
      return;
    }
    final String? who = await _operatorName();
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
    await Navigator.of(context)
        .push<void>(MaterialPageRoute<void>(builder: (_) => page(client)));
    client.close();
  }

  /// 这次报什么名字。问过一次就记着，同一次开着 app 不再重复问。
  ///
  /// 返回 `null` 表示人放弃了（按了「算了」，或者名字是空的）—— 那就不连。
  Future<String?> _operatorName() async {
    if (_operator.isNotEmpty) return _operator;
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
    _operator = asked;
    return asked;
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
