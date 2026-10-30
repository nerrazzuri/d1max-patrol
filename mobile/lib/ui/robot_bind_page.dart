/// 机器人绑定（W42，`docs/W42-机器人绑定-设计.md` §9）：添加机器人、换 WiFi、工程师用解绑令解绑、
/// 站点上「等待批准加入」。
///
/// 三个流程都是：扫上装贴纸 → 手机连机器人的热点（Android 自动连；连不上就把热点名、密码摆出来，教人去
/// 系统设置里手动连）→ 经热点跟机器人的配网服务说话 → 离开热点，回到站点上批准 / 确认。
/// **离开热点再找站点**：Android 上连着热点时这个 App 的请求都走热点，站点够不着。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../design/nw_tokens.g.dart';
import '../l10n.dart';
import '../net/robot_bind.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p1_entry.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_add.dart' show CodeScanner, canScanCodes;
import 'site_scan.dart';

/// 跟一台机器人的配网服务说话的东西从哪来（测试换成假的）。
typedef BindLinkFactory = RobotBindLink Function(RobotSticker sticker);

RobotBindLink defaultBindLink(RobotSticker s) => RobotHotspot(s);

Future<String?> scanRobotCode(BuildContext context) =>
    Navigator.push<String>(context, MaterialPageRoute<String>(builder: (_) => const SiteScanPage()));

/// 机器人配网服务报的「在干什么」→ 给人看的一行。
String bindPhaseText(Map<String, dynamic> st) {
  final state = st['state'], phase = st['phase'];
  if (state == 'bound') return tr('Joined the site', '已经加入站点');
  if (state == 'wiping') return tr('Resetting to factory state (deleting data)…', '正在恢复出厂（删数据）…');
  if (state == 'unbound') return tr('Not joined to any site', '还没加入站点');
  if (phase == 'waiting') {
    return tr('Waiting for the site to approve. Check code ${st['check'] ?? ''}',
        '等站点批准。核对码 ${st['check'] ?? ''}');
  }
  return tr('Connecting to the WiFi and the site…', '正在连 WiFi 和站点…');
}

/// 连热点：自动连上回 true；连不上把热点名、密码摆出来，人在系统设置里连好了点「已经连上」回 true，取消回 false。
Future<bool> joinRobotHotspot(BuildContext context, HotspotJoiner joiner, RobotSticker s) async {
  if (await joiner.join(s.ssid, s.psk)) return true;
  if (!context.mounted) return false;
  final ok = await showDialog<bool>(
    context: context,
    builder: (c) {
      final nw = Nw.of(c);
      return AlertDialog(
        scrollable: true,
        backgroundColor: nw.surface,
        title: Text(tr("Connect to the robot's hotspot", '连机器人的热点'),
            style: NwText.titleLarge.copyWith(color: nw.text)),
        content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(tr('Open the phone WiFi settings and join this network, then come back.',
              '打开手机的 WiFi 设置，连上这个网络，再回来。'),
              style: NwText.bodyMedium.copyWith(color: nw.text)),
          const SizedBox(height: Nw.s16),
          SelectableText(tr('Network: ${s.ssid}', '网络：${s.ssid}'),
              key: const Key('hotspot-ssid'), style: NwText.bodyMedium.copyWith(fontFamily: NwFont.mono, color: nw.text)),
          SelectableText(tr('Password: ${s.psk}', '密码：${s.psk}'),
              key: const Key('hotspot-psk'), style: NwText.bodyMedium.copyWith(fontFamily: NwFont.mono, color: nw.text)),
        ]),
        actions: [
          NwButton(label: tr('Cancel', '取消'), variant: NwButtonVariant.ghost,
              onPressed: () => Navigator.pop(c, false)),
          NwButton(buttonKey: const Key('hotspot-joined'), label: tr("I'm connected", '已经连上'),
              onPressed: () => Navigator.pop(c, true)),
        ],
      );
    },
  );
  return ok == true;
}

/// 页面骨架：页头、可滚动、宽屏不拉满。
class _Frame extends StatelessWidget {
  final String title;
  final List<Widget> children;
  const _Frame({required this.title, required this.children});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Scaffold(
      backgroundColor: c.bg,
      appBar: nwHeaderBar(context, NwHeader(leading: BackButton(color: c.text), title: title)),
      body: SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(Nw.s24),
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 560),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: children),
            ),
          ),
        ),
      ),
    );
  }
}

const _gap = SizedBox(height: Nw.s16);

/// 公共的「扫贴纸」一步。
mixin _StickerScan<T extends StatefulWidget> on State<T> {
  CodeScanner? get scanner;
  RobotSticker? sticker;

  void say(String s) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(s)));

  Future<void> scanSticker() async {
    final raw = await (scanner ?? scanRobotCode)(context);
    if (raw == null || !mounted) return;
    takeSticker(raw);
  }

  /// 桌面版没有相机：粘贴贴纸码（装机工程师从出厂文件里拷、演示站点打印出来的）。
  Future<void> pasteSticker() async {
    final code = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) {
        final nw = Nw.of(c);
        return AlertDialog(
          scrollable: true,
          backgroundColor: nw.surface,
          title: Text(tr('Paste sticker code', '粘贴贴纸码'), style: NwText.titleLarge.copyWith(color: nw.text)),
          content: NwField(fieldKey: const Key('sticker-code'), controller: code, autofocus: true,
              autocorrect: false, enableSuggestions: false, maxLines: 4, minLines: 2, mono: true,
              hint: '$stickerPrefix…'),
          actions: [
            NwButton(label: tr('Cancel', '取消'), variant: NwButtonVariant.ghost,
                onPressed: () => Navigator.pop(c, false)),
            NwButton(buttonKey: const Key('sticker-code-ok'), label: tr('Use code', '用这个码'),
                onPressed: () => Navigator.pop(c, true)),
          ],
        );
      },
    );
    final text = code.text;
    if (ok == true && mounted) takeSticker(text);
  }

  /// 二维码坏了 / 手动连热点：手输贴纸上印的序列号和绑定码。
  Future<void> typeSticker() async {
    final serial = TextEditingController();
    final code = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) {
        final nw = Nw.of(c);
        return AlertDialog(
          scrollable: true,
          backgroundColor: nw.surface,
          title: Text(tr('Type serial and code', '手输序列号和绑定码'), style: NwText.titleLarge.copyWith(color: nw.text)),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            NwField(fieldKey: const Key('manual-serial'), controller: serial, autocorrect: false,
                label: tr('Serial number', '序列号')),
            const SizedBox(height: Nw.s16),
            NwField(fieldKey: const Key('manual-code'), controller: code, autocorrect: false,
                enableSuggestions: false, mono: true, hint: 'XXXX-XXXX-XXXX-XXXX',
                label: tr('Binding code', '绑定码')),
          ]),
          actions: [
            NwButton(label: tr('Cancel', '取消'), variant: NwButtonVariant.ghost,
                onPressed: () => Navigator.pop(c, false)),
            NwButton(buttonKey: const Key('manual-ok'), label: tr('Use', '用这个'),
                onPressed: () => Navigator.pop(c, true)),
          ],
        );
      },
    );
    final (sv, cv) = (serial.text, code.text);
    if (ok != true || !mounted) return;
    try {
      final s = manualSticker(sv, cv);
      setState(() => sticker = s);
      onSticker(s);
    } on FormatException catch (e) {
      say(e.message);
    }
  }

  void takeSticker(String raw) {
    try {
      final s = parseSticker(raw);
      setState(() => sticker = s);
      onSticker(s);
    } on FormatException catch (e) {
      say(e.message);
    }
  }

  void onSticker(RobotSticker s) {}

  Widget scanButton() => Wrap(spacing: Nw.s16, runSpacing: Nw.s8, children: [
        if (scanner != null || canScanCodes)
          NwButton(
            buttonKey: const Key('bind-scan'),
            onPressed: scanSticker,
            icon: Icons.qr_code_scanner_rounded,
            label: tr('Scan the sticker on the robot', '扫机器人身上的贴纸'),
          ),
        NwButton(
          buttonKey: const Key('bind-paste'),
          onPressed: pasteSticker,
          icon: Icons.content_paste_rounded,
          variant: NwButtonVariant.outline,
          label: tr('Paste sticker code', '粘贴贴纸码'),
        ),
        NwButton(
          buttonKey: const Key('bind-manual'),
          onPressed: typeSticker,
          icon: Icons.keyboard_rounded,
          variant: NwButtonVariant.ghost,
          label: tr('Type serial and code', '手输序列号和绑定码'),
        ),
      ]);

  /// 手输的贴纸没有证书指纹：提醒一句。
  Widget? unpinnedNote() => sticker != null && sticker!.fingerprint.isEmpty
      ? Padding(
          padding: const EdgeInsets.only(top: Nw.s8),
          child: NwNotice(
              key: const Key('bind-unpinned'),
              tone: NwTone.warning,
              text: tr("Typed in by hand, so the phone can't check it is really this robot. Scan the sticker when you can.",
                  '手输的，手机核不了连上的确实是这台机器人。能扫码就扫码。')))
      : null;
}

// ------------------------------------------------------------ 添加机器人

class RobotAddPage extends StatefulWidget {
  final SiteApi api;
  final SiteEntry site;
  final CodeScanner? scanner;
  final HotspotJoiner joiner;
  final BindLinkFactory link;
  /// 轮询机器人状态的间隔（测试调短）。
  final Duration pollEvery;
  const RobotAddPage(
      {super.key,
      required this.api,
      required this.site,
      this.scanner,
      this.joiner = const ChannelHotspotJoiner(),
      this.link = defaultBindLink,
      this.pollEvery = const Duration(seconds: 2)});

  @override
  State<RobotAddPage> createState() => _RobotAddPageState();
}

class _RobotAddPageState extends State<RobotAddPage> with _StickerScan<RobotAddPage> {
  final _ssid = TextEditingController();
  final _psk = TextEditingController();
  RobotBindLink? _link;
  Map<String, dynamic>? _st;
  Timer? _poll;
  bool _busy = false;

  /// 离开热点了（之后在站点上批准）。
  bool _left = false;
  bool _approved = false;

  @override
  CodeScanner? get scanner => widget.scanner;

  @override
  void dispose() {
    _poll?.cancel();
    _link?.close();
    if (_link != null && !_left) unawaited(widget.joiner.leave());
    _ssid.dispose();
    _psk.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final s = sticker;
    if (s == null) return;
    final bad = checkWifi(_ssid.text, _psk.text);
    if (bad != null) return say(bad);
    setState(() => _busy = true);
    try {
      if (!await joinRobotHotspot(context, widget.joiner, s)) return;
      final link = _link ??= widget.link(s);
      final now = await link.status();
      if (now['state'] == 'bound' || now['state'] == 'wiping') {
        return say(tr('This robot already belongs to a site. Unbind it there first.',
            '这台机器人已经绑在一个站点上了：先在原来的站点上解绑。'));
      }
      final st = await link.bind(
          ssid: _ssid.text, psk: _psk.text, siteUrl: widget.site.url,
          siteFingerprint: widget.site.fingerprint, siteName: widget.site.name);
      if (!mounted) return;
      setState(() => _st = st);
      _poll?.cancel();
      _poll = Timer.periodic(widget.pollEvery, (_) => _refresh());
    } on RobotBindError catch (e) {
      say(e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _refresh() async {
    final link = _link;
    if (link == null || _left) return;
    try {
      final st = await link.status();
      if (mounted) setState(() => _st = st);
    } on RobotBindError {
      return; // 热点一时断了（机器人换网时会掉一下）：下一次再问
    }
  }

  Future<void> _leaveAndApprove() async {
    _poll?.cancel();
    await widget.joiner.leave();
    if (!mounted) return;
    setState(() => _left = true);
    if (widget.api.session?.canManageRobots != true) return;
    final serial = sticker!.serial;
    try {
      final rows = await widget.api.bindRequests();
      final req = rows.where((r) => r['serial'] == serial).firstOrNull;
      if (!mounted) return;
      if (req == null) {
        return say(tr("The site doesn't show this robot yet. Try again in a moment.",
            '站点上还没看到这台的申请，等一下再试。'));
      }
      final ok = await approveBindDialog(context, widget.api, req);
      if (ok && mounted) setState(() => _approved = true);
    } on SiteError catch (e) {
      say(e.toString());
    }
  }

  Future<void> _cancel() async {
    try {
      await _link?.cancel();
    } on RobotBindError catch (e) {
      say(e.message);
    }
    if (mounted) Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = sticker, st = _st;
    final waiting = st != null && st['state'] == 'pending' && st['phase'] == 'waiting';
    return _Frame(title: tr('Add robot', '添加机器人'), children: [
      Text(tr(
          'Stand next to the robot. Scan the sticker on its payload, enter the estate WiFi, then approve it on the site.',
          '站在机器人旁边：扫上装上的贴纸，填庄园 WiFi，然后在站点上批准。'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      _gap,
      if (s == null) scanButton(),
      if (s != null && st == null) ...[
        NwNotice(key: const Key('bind-serial'), text: tr('Robot ${s.serial}', '机器人 ${s.serial}')),
        ?unpinnedNote(),
        _gap,
        NwField(fieldKey: const Key('bind-ssid'), controller: _ssid, autocorrect: false,
            label: tr('Estate WiFi name', '庄园 WiFi 名')),
        _gap,
        NwField(fieldKey: const Key('bind-psk'), controller: _psk, obscureText: true, autocorrect: false,
            enableSuggestions: false, label: tr('WiFi password', 'WiFi 密码')),
        const SizedBox(height: Nw.s8),
        Text(tr('2.4 GHz WiFi only. If the estate only has 5 GHz, turn on 2.4 GHz on its router first.',
            '只支持 2.4 GHz 的 WiFi。庄园只有 5 GHz 的，先在庄园路由器上把 2.4 GHz 打开。'),
            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
        const SizedBox(height: Nw.s24),
        NwButton(buttonKey: const Key('bind-send'), label: tr('Send to robot', '发给机器人'),
            onPressed: _busy ? null : _send),
      ],
      if (st != null && !_left) ...[
        NwNotice(key: const Key('bind-phase'), text: bindPhaseText(st),
            tone: (st['error'] ?? '') != '' ? NwTone.warning : NwTone.info),
        if ('${st['error'] ?? ''}'.isNotEmpty) ...[
          _gap,
          Text('${st['error']}', key: const Key('bind-error'),
              style: NwText.bodySmall.copyWith(color: c.textSecondary)),
        ],
        if (waiting) ...[
          _gap,
          Text('${st['check']}', key: const Key('bind-check'), textAlign: TextAlign.center,
              style: NwText.headlineMedium.copyWith(fontFamily: NwFont.mono, color: c.text, letterSpacing: 4)),
          _gap,
          NwButton(buttonKey: const Key('bind-leave'),
              label: widget.api.session?.canManageRobots == true
                  ? tr('Leave the hotspot and approve', '离开热点，去批准')
                  : tr('Leave the hotspot', '离开热点'),
              onPressed: _leaveAndApprove),
        ],
        _gap,
        NwButton(buttonKey: const Key('bind-cancel'), label: tr('Give up adding', '不加了'),
            variant: NwButtonVariant.ghost, onPressed: _cancel),
      ],
      if (_left)
        NwNotice(
            key: const Key('bind-done'),
            tone: _approved ? NwTone.success : NwTone.info,
            text: _approved
                ? tr('Approved. The robot shows up in the robot list in a few seconds.',
                    '已批准。机器人几秒后出现在机器人列表里。')
                : tr('Ask the control room to approve it on the site (Robots page). Check code ${st?['check'] ?? ''}.',
                    '请控制室在站点上批准（机器人页）。核对码 ${st?['check'] ?? ''}。')),
    ]);
  }
}

// ------------------------------------------------------------ 换 WiFi

class RobotWifiPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final CodeScanner? scanner;
  final HotspotJoiner joiner;
  final BindLinkFactory link;
  const RobotWifiPage(
      {super.key,
      required this.api,
      required this.robotId,
      this.scanner,
      this.joiner = const ChannelHotspotJoiner(),
      this.link = defaultBindLink});

  @override
  State<RobotWifiPage> createState() => _RobotWifiPageState();
}

class _RobotWifiPageState extends State<RobotWifiPage> with _StickerScan<RobotWifiPage> {
  final _ssid = TextEditingController();
  final _psk = TextEditingController();
  Map<String, dynamic>? _change;
  bool _busy = false;
  bool _confirmed = false;

  @override
  CodeScanner? get scanner => widget.scanner;

  @override
  void onSticker(RobotSticker s) {
    if (s.serial != widget.robotId) {
      setState(() => sticker = null);
      say(tr("That sticker is for robot ${s.serial}, not ${widget.robotId}",
          '这张贴纸是机器人 ${s.serial} 的，不是 ${widget.robotId}'));
    }
  }

  @override
  void dispose() {
    _ssid.dispose();
    _psk.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final s = sticker;
    if (s == null) return;
    final bad = checkWifi(_ssid.text, _psk.text);
    if (bad != null) return say(bad);
    setState(() => _busy = true);
    RobotBindLink? link;
    try {
      if (!await joinRobotHotspot(context, widget.joiner, s)) return;
      link = widget.link(s);
      final st = await link.wifi(_ssid.text, _psk.text);
      final ch = st['change'];
      if (mounted && ch is Map) setState(() => _change = Map<String, dynamic>.from(ch));
    } on RobotBindError catch (e) {
      say(e.message);
    } finally {
      link?.close();
      await widget.joiner.leave();
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _confirm() async {
    final ch = _change;
    if (ch == null) return;
    try {
      await widget.api.confirmRobotWifi(widget.robotId, '${ch['id']}', true);
      if (mounted) setState(() => _confirmed = true);
    } on SiteError catch (e) {
      say(e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final ch = _change;
    final manage = widget.api.session?.canManageRobots == true;
    return _Frame(title: tr('Change WiFi', '换 WiFi'), children: [
      Text(tr(
          'Stand next to the robot and scan its sticker. The new WiFi only takes effect after someone with management rights approves it on the site.',
          '站在机器人旁边扫它的贴纸。新 WiFi 要有管理权限的人在站点上确认了才生效。'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      _gap,
      if (sticker == null) scanButton(),
      if (sticker != null && ch == null) ...[
        ?unpinnedNote(),
        _gap,
        NwField(fieldKey: const Key('wifi-ssid'), controller: _ssid, autocorrect: false,
            label: tr('New WiFi name', '新的 WiFi 名')),
        _gap,
        NwField(fieldKey: const Key('wifi-psk'), controller: _psk, obscureText: true, autocorrect: false,
            enableSuggestions: false, label: tr('WiFi password', 'WiFi 密码')),
        const SizedBox(height: Nw.s8),
        Text(tr('2.4 GHz WiFi only.', '只支持 2.4 GHz 的 WiFi。'),
            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
        const SizedBox(height: Nw.s24),
        NwButton(buttonKey: const Key('wifi-send'), label: tr('Send to robot', '发给机器人'),
            onPressed: _busy ? null : _send),
      ],
      if (ch != null) ...[
        NwNotice(
            key: const Key('wifi-sent'),
            tone: _confirmed ? NwTone.success : NwTone.info,
            text: _confirmed
                ? tr('Approved. The robot switches, and switches back if it cannot reach the site within 5 minutes.',
                    '已确认。机器人换过去，5 分钟内连不上站点会自己退回。')
                : ch['mode'] == 'trial'
                    ? tr("The robot can't reach the site, so it is trying “${ch['ssid']}” now. Approve it on the robot page within 30 minutes once the robot is back online, otherwise it switches back.",
                        '机器人现在连不上站点，正在试「${ch['ssid']}」。它连上站点后 30 分钟内在机器人页确认，不然退回原来的。')
                    : tr('Sent. Nothing changes until it is approved on the site.',
                        '已发给机器人。站点上确认了才换。')),
        if (ch['mode'] == 'online' && manage && !_confirmed) ...[
          _gap,
          NwButton(buttonKey: const Key('wifi-confirm'), label: tr('Approve WiFi change', '确认换 WiFi'),
              onPressed: _confirm),
        ],
      ],
    ]);
  }
}

// ------------------------------------------------------------ 工程师：解绑令

class EngineerUnbindPage extends StatefulWidget {
  final CodeScanner? scanner;
  final HotspotJoiner joiner;
  final BindLinkFactory link;
  const EngineerUnbindPage(
      {super.key, this.scanner, this.joiner = const ChannelHotspotJoiner(), this.link = defaultBindLink});

  @override
  State<EngineerUnbindPage> createState() => _EngineerUnbindPageState();
}

class _EngineerUnbindPageState extends State<EngineerUnbindPage> with _StickerScan<EngineerUnbindPage> {
  final _token = TextEditingController();
  bool _busy = false;
  bool _done = false;

  @override
  CodeScanner? get scanner => widget.scanner;

  @override
  void dispose() {
    _token.dispose();
    super.dispose();
  }

  Future<void> _scanToken() async {
    final raw = await (widget.scanner ?? scanRobotCode)(context);
    if (raw != null && mounted) setState(() => _token.text = raw.trim());
  }

  Future<void> _send() async {
    final s = sticker;
    if (s == null) return;
    if (!_token.text.trim().startsWith(unbindPrefix)) {
      return say(tr('That is not an unbind token', '这不是解绑令'));
    }
    setState(() => _busy = true);
    RobotBindLink? link;
    try {
      if (!await joinRobotHotspot(context, widget.joiner, s)) return;
      link = widget.link(s);
      await link.unbind(_token.text.trim());
      if (mounted) setState(() => _done = true);
    } on RobotBindError catch (e) {
      say(e.message);
    } finally {
      link?.close();
      await widget.joiner.leave();
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return _Frame(title: tr('Unbind with a token', '用解绑令解绑'), children: [
      Text(tr(
          'For engineers, when the site is gone and there is no backup. The company signs a token for this one robot (valid 24 hours, single use). The robot is reset to factory state.',
          '给工程师用：站点主机坏了又没备份时。公司给这台机器人签一张解绑令（24 小时内有效、只能用一次）。机器人会恢复出厂。'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      _gap,
      NwField(fieldKey: const Key('unbind-token'), controller: _token, autocorrect: false,
          enableSuggestions: false, minLines: 2, maxLines: 4, mono: true, hint: '$unbindPrefix…',
          label: tr('Unbind token', '解绑令')),
      const SizedBox(height: Nw.s8),
      NwButton(buttonKey: const Key('unbind-scan-token'), label: tr('Scan token', '扫解绑令'),
          variant: NwButtonVariant.outline, icon: Icons.qr_code_scanner_rounded, onPressed: _scanToken),
      _gap,
      if (sticker == null) scanButton() else
        NwNotice(text: tr('Robot ${sticker!.serial}', '机器人 ${sticker!.serial}')),
      ?unpinnedNote(),
      const SizedBox(height: Nw.s24),
      if (_done)
        NwNotice(key: const Key('unbind-done'), tone: NwTone.success,
            text: tr('Accepted. The robot is deleting its data; in a few minutes it can be added to a site again.',
                '机器人收下了，正在删数据；几分钟后就能重新添加到站点。'))
      else
        NwButton(buttonKey: const Key('unbind-send'), label: tr('Unbind', '解绑'),
            variant: NwButtonVariant.destructive,
            onPressed: _busy || sticker == null ? null : _send),
    ]);
  }
}

// ------------------------------------------------------------ 站点上：等待批准加入

/// 批准一条（核对码 + 起名 + 再输密码）。批了回 true。
Future<bool> approveBindDialog(BuildContext context, SiteApi api, Map<String, dynamic> req) async =>
    await showDialog<bool>(context: context, builder: (_) => _ApproveDialog(api: api, req: req)) == true;

class _ApproveDialog extends StatefulWidget {
  final SiteApi api;
  final Map<String, dynamic> req;
  const _ApproveDialog({required this.api, required this.req});

  @override
  State<_ApproveDialog> createState() => _ApproveDialogState();
}

class _ApproveDialogState extends State<_ApproveDialog> {
  final _name = TextEditingController();
  final _pw = TextEditingController();
  String? _err;
  bool _busy = false;

  @override
  void dispose() {
    _name.dispose();
    _pw.dispose();
    super.dispose();
  }

  Future<void> _ok() async {
    setState(() => _busy = true);
    try {
      await widget.api.approveBind('${widget.req['id']}', _pw.text, name: _name.text.trim());
      if (mounted) Navigator.pop(context, true);
    } on SiteError catch (e) {
      if (!mounted) return;
      setState(() => _err = e.status == 403 ? tr('Wrong password', '密码不对') : e.toString());
      _pw.clear();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final nw = Nw.of(context);
    final req = widget.req;
    return AlertDialog(
      scrollable: true,
      backgroundColor: nw.surface,
      title: Text(tr('Approve ${req['serial']}', '批准 ${req['serial']} 加入'),
          style: NwText.titleLarge.copyWith(color: nw.text)),
      content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(tr('Approve only if this check code matches the phone at the robot.',
            '核对码跟机器人旁边那台手机上的一样才批准。'),
            style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
        const SizedBox(height: Nw.s8),
        Text('${req['check']}', key: const Key('approve-check'), textAlign: TextAlign.center,
            style: NwText.headlineMedium.copyWith(fontFamily: NwFont.mono, color: nw.text, letterSpacing: 4)),
        const SizedBox(height: Nw.s16),
        NwField(fieldKey: const Key('approve-name'), controller: _name, label: tr('Name (optional)', '名字（可不填）')),
        const SizedBox(height: Nw.s16),
        NwField(fieldKey: const Key('approve-pw'), controller: _pw, obscureText: true,
            label: tr('Your password again', '再输一次你的密码')),
        if (_err != null) ...[
          const SizedBox(height: Nw.s8),
          Text(_err!, key: const Key('approve-error'), style: NwText.bodySmall.copyWith(color: nw.error)),
        ],
      ]),
      actions: [
        NwButton(label: tr('Cancel', '取消'), variant: NwButtonVariant.ghost,
            onPressed: () => Navigator.pop(context, false)),
        NwButton(buttonKey: const Key('approve-ok'), label: tr('Approve', '批准'), onPressed: _busy ? null : _ok),
      ],
    );
  }
}

/// 机器人列表顶上的「等待批准加入」（有管理权限的人才显示）。
class BindApprovals extends StatefulWidget {
  final SiteApi api;
  final Duration refreshEvery;
  const BindApprovals({super.key, required this.api, this.refreshEvery = const Duration(seconds: 10)});

  @override
  State<BindApprovals> createState() => _BindApprovalsState();
}

class _BindApprovalsState extends State<BindApprovals> {
  List<Map<String, dynamic>> _rows = const <Map<String, dynamic>>[];
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    _timer = Timer.periodic(widget.refreshEvery, (_) => _load());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final rows = await widget.api.bindRequests();
      if (mounted) setState(() => _rows = rows);
    } on SiteError {
      if (mounted) setState(() => _rows = const <Map<String, dynamic>>[]); // 老站点没开绑定：不显示
    } on FormatException {
      return;
    }
  }

  Future<void> _reject(Map<String, dynamic> r) async {
    try {
      await widget.api.rejectBind('${r['id']}');
    } on SiteError catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString())));
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    if (_rows.isEmpty) return const SizedBox.shrink();
    final c = Nw.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: Nw.s8),
      child: NwCard(
        key: const Key('bind-approvals'),
        tone: NwTone.warning,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text(tr('Waiting to join', '等待批准加入'), style: NwText.titleMedium.copyWith(color: c.text)),
          const SizedBox(height: Nw.s8),
          for (final r in _rows)
            Padding(
              key: Key('bind-req-${r['serial']}'),
              padding: const EdgeInsets.only(top: Nw.s8),
              child: Wrap(spacing: Nw.s16, runSpacing: Nw.s8, crossAxisAlignment: WrapCrossAlignment.center, children: [
                Text('${r['serial']}', style: NwText.bodyMedium.copyWith(fontFamily: NwFont.mono, color: c.text)),
                Text(tr('Check ${r['check']}', '核对码 ${r['check']}'), style: NwText.bodyMedium.copyWith(color: c.text)),
                NwButton(
                    buttonKey: Key('bind-approve-${r['serial']}'),
                    size: NwButtonSize.small,
                    label: tr('Approve', '批准'),
                    onPressed: () async {
                      if (await approveBindDialog(context, widget.api, r)) await _load();
                    }),
                NwButton(
                    buttonKey: Key('bind-reject-${r['serial']}'),
                    size: NwButtonSize.small,
                    variant: NwButtonVariant.ghost,
                    label: tr('Reject', '拒绝'),
                    onPressed: () => _reject(r)),
              ]),
            ),
        ]),
      ),
    );
  }
}
