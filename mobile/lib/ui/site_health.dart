/// 站点体检（C-B4；站点那头是商业化 A1 / A7）：管理员在手机上看站点好不好 —— 每一项检查过没过、
/// 哪几项是要紧的、站点和每台狗的版本配不配、谁还登录着哪一版 App；还能把诊断包存到手机上发给厂家。
///
/// 没过的要紧项用 P1 的形状和颜色，别的没过的用 P2；过了的是绿色的勾（2026-10-14 重做：正常用绿色）。
/// 站点给的明细是中文（英文界面也摆出来，比没有强）。读不到就说读不到，不拿上一次的当现在的。
///
/// 版面照设计稿的 System Health：「站点状态」一张卡（各项检查）、「固件与软件」一片格子（站点、每只狗、
/// 每台登录着的客户端）、「维护」一张卡（诊断包）。设计稿的「重启站点」没做：站点没有这个功能。
library;

import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_cards.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'widget/alarm_widgets.dart';

/// 要紧的几项（站点 `health.CRITICAL`）：哪一项没过站点就算不能用。
const Set<String> criticalChecks = {'db', 'loop', 'broker', 'disk'};

/// 检查项的名字；不认得的（站点以后多出来的）原样摆。
String checkLabel(String key) => switch (key) {
      'db' => tr('Database', '数据库'),
      'loop' => tr('Event loop', '事件循环'),
      'broker' => tr('Message broker', '消息服务'),
      'disk' => tr('Disk', '磁盘'),
      'certs' => tr('Certificates', '证书'),
      'lanes' => tr('Background tasks', '后台任务'),
      'keys' => tr('Keys', '密钥'),
      'backup' => tr('Backup', '备份'),
      'push' => tr('Push notifications', '推送'),
      'robots' => tr('Robots', '四足机器人'),
      _ => key,
    };

/// 狗上的代理跟站点配不配。
String verdictLabel(Object? v) => switch (v) {
      'ok' => tr('OK', '正常'),
      'agent_too_old' => tr('Robot too old', '机器人版本过旧'),
      'site_too_old' => tr('Site too old', '站点版本过旧'),
      _ => tr('Unknown', '未知'),
    };

/// 把诊断包存成文件，回存到哪儿了。测试换掉它（不碰真的盘）。
/// 回存到哪儿了；null = 人取消了。
typedef BundleSaver = Future<String?> Function(String name, Uint8List bytes);

/// 回 false = 人关掉了分享面板。
typedef BundleSharer = Future<bool> Function(String name, Uint8List bytes);

/// 存到「下载」目录（安卓上是这个 App 自己的外部文件目录，文件管理器里看得到）；没有就存到文档目录。
Future<String> saveBundleToDisk(String name, Uint8List bytes) async {
  Directory? dir;
  try {
    dir = await getDownloadsDirectory();
  } on Object {
    dir = null;
  }
  dir ??= await getApplicationDocumentsDirectory();
  await dir.create(recursive: true);
  final f = File('${dir.path}${Platform.pathSeparator}$name');
  await f.writeAsBytes(bytes, flush: true);
  return f.path;
}

/// 「另存」（RC 加固 H8）：弹系统的保存框，人自己挑放哪儿（安卓上是「下载」、网盘……；桌面版是
/// 文件对话框）。App 自己的目录在安卓 11 以上的文件管理器里进不去，存那儿等于没存。
/// 回 null = 人取消了。保存框起不来（桌面上没装对话框程序之类）才退回 [saveBundleToDisk]。
Future<String?> saveBundleWithPicker(String name, Uint8List bytes) async {
  final Uri? where;
  try {
    where = await FilePicker.saveFile(fileName: name, bytes: bytes, mimeType: 'application/gzip');
  } on Object {
    return saveBundleToDisk(name, bytes);
  }
  if (where == null) return null;
  return where.isScheme('file') ? where.toFilePath() : name;
}

/// 「分享」：先放进临时目录，再交给系统的分享面板（邮件、聊天软件、网盘、文件）。回 false = 人关掉了面板。
Future<bool> shareBundle(String name, Uint8List bytes) async {
  final dir = await getTemporaryDirectory();
  final f = File('${dir.path}${Platform.pathSeparator}$name');
  await f.writeAsBytes(bytes, flush: true);
  final r = await SharePlus.instance
      .share(ShareParams(files: [XFile(f.path, mimeType: 'application/gzip')], subject: name));
  return r.status != ShareResultStatus.dismissed;
}

/// 诊断包的文件名：`d1max-support-20261011-021436.tar.gz`（当地时间）。
String bundleName(DateTime now) {
  String two(int n) => n.toString().padLeft(2, '0');
  return 'd1max-support-${now.year}${two(now.month)}${two(now.day)}-'
      '${two(now.hour)}${two(now.minute)}${two(now.second)}.tar.gz';
}

class SiteHealthPage extends StatefulWidget {
  final SiteApi api;
  final BundleSaver save;
  final BundleSharer share;

  /// 有没有「分享」按钮。不给就按系统：手机有，桌面版没有（那边的分享面板发不了文件）。
  final bool? canShare;
  final DateTime Function() now;

  /// 嵌在底部导航的管理栏里：不带 `AppBar`（没有返回可按），页头换成外壳那种，刷新还在页头右边。
  final bool embedded;

  /// 接在诊断包下面、跟着一起滚的东西（管理栏放去版本、拦截点、摄像头、天气的入口）。
  final Widget? footer;
  const SiteHealthPage(
      {super.key,
      required this.api,
      this.save = saveBundleWithPicker,
      this.share = shareBundle,
      this.canShare,
      this.now = DateTime.now,
      this.embedded = false,
      this.footer});

  static const Key overallKey = Key('health-overall');
  static const Key refreshKey = Key('health-refresh');
  static const Key bundleKey = Key('health-bundle');
  static const Key shareKey = Key('health-share');
  static const Key msgKey = Key('health-msg');
  static const Key loadingKey = Key('health-loading');
  static Key checkKey(String name) => Key('health-check-$name');

  @override
  State<SiteHealthPage> createState() => _SiteHealthPageState();
}

class _SiteHealthPageState extends State<SiteHealthPage> {
  Map<String, dynamic>? _health;
  Map<String, dynamic>? _versions;
  String? _error;
  bool _loading = true;
  bool _saving = false; // 存或者分享正在做：两个按钮都按不了
  bool _sharing = false;
  String _msg = '';
  int _asked = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  /// 体检和版本一览**各问各的、各显示各的**（外审 M1）：一个慢、一个坏，不拖着另一个。一刷新就把上一次的
  /// 结论撤掉（写「正在读」）—— 没读到就是没读到，不留着上一次的当现在的。先发的后到：丢掉。
  void _load() {
    final seq = ++_asked;
    setState(() {
      _loading = true;
      _health = null;
      _versions = null;
      _error = null;
    });
    bool current() => mounted && seq == _asked;
    () async {
      Map<String, dynamic>? h;
      String? err;
      try {
        h = await widget.api.health();
      } on Object catch (e) {
        err = '$e';
      }
      if (!current()) return;
      setState(() {
        _health = h;
        _error = err;
        _loading = false;
      });
    }();
    () async {
      Map<String, dynamic>? v;
      try {
        v = await widget.api.versions();
      } on Object {
        v = null; // 版本一览读不到：那一段不显示，体检照样看
      }
      if (current()) setState(() => _versions = v);
    }();
  }

  Future<void> _bundle({bool share = false}) async {
    setState(() {
      _saving = true;
      _sharing = share;
      _msg = '';
    });
    // 不管哪一步出什么错（取不到、存不下、找目录的插件坏了）：都说出来，按钮一定放开让人再试（外审 I1）
    var msg = '';
    try {
      final bytes = await widget.api.supportBundle();
      final name = bundleName(widget.now());
      final kb = (bytes.length / 1024).ceil();
      if (share) {
        msg = await widget.share(name, bytes)
            ? tr('Shared $name ($kb KB)', '已分享（$kb KB）：$name')
            : tr('Sharing cancelled', '已取消分享');
      } else {
        final path = await widget.save(name, bytes);
        msg = path == null
            ? tr('Saving cancelled', '已取消保存')
            : tr('Saved $kb KB to $path', '已保存（$kb KB）：$path');
      }
    } on FileSystemException catch (e) {
      msg = share
          ? tr('Failed to share the diagnostics bundle: ${e.message}', '诊断包分享失败：${e.message}')
          : tr('Failed to save the diagnostics bundle: ${e.message}', '诊断包保存失败：${e.message}');
    } on Object catch (e) {
      msg = share
          ? tr('Failed to share the diagnostics bundle: $e', '诊断包分享失败：$e')
          : tr('Failed to save the diagnostics bundle: $e', '诊断包保存失败：$e');
    } finally {
      if (mounted) {
        setState(() {
          _saving = false;
          _msg = msg;
        });
      }
    }
  }

  Widget _overall(Map<String, dynamic> h) {
    final nw = Nw.of(context);
    final ok = h['ok'] == true;
    final critical = h['critical_ok'] == false;
    final color = ok ? nw.success : (critical ? nw.error : nw.warning);
    return ListTile(
        key: SiteHealthPage.overallKey,
        leading: ok
            ? Icon(Icons.check_circle_rounded, color: color)
            : PriorityShape(level: critical ? 'P1' : 'P2', color: color, size: 20),
        title: Text(
            ok
                ? tr('All checks passed', '全部正常')
                : critical
                    ? tr('Site is not healthy', '站点存在严重问题')
                    : tr('Some checks need attention', '有几项需要注意'),
            style: NwText.titleMedium.copyWith(color: ok ? nw.text : color)),
        subtitle: Text(tr('Site version ${h['version'] ?? '?'}', '站点版本 ${h['version'] ?? '?'}'),
            style: NwText.bodySmall.copyWith(color: nw.textSecondary)));
  }

  List<Widget> _checks(Map<String, dynamic> h) {
    final checks = h['checks'] is Map ? (h['checks'] as Map).cast<String, dynamic>() : const <String, dynamic>{};
    // 没过的在前，要紧的再在前；同一档里按名字
    final names = checks.keys.toList()
      ..sort((a, b) {
        int rank(String k) {
          final ok = checks[k] is Map && (checks[k] as Map)['ok'] == true;
          return ok ? 2 : (criticalChecks.contains(k) ? 0 : 1);
        }

        final d = rank(a) - rank(b);
        return d != 0 ? d : a.compareTo(b);
      });
    return [
      for (final k in names)
        () {
          final c = checks[k] is Map ? checks[k] as Map : const <String, dynamic>{};
          final ok = c['ok'] == true;
          final crit = criticalChecks.contains(k);
          final nw = Nw.of(context);
          final color = ok ? nw.success : (crit ? nw.error : nw.warning);
          return ListTile(
              key: SiteHealthPage.checkKey(k),
              dense: true,
              leading: ok ? Icon(Icons.check_circle_rounded, size: 20, color: color) : PriorityShape(level: crit ? 'P1' : 'P2', color: color, size: 16),
              title: Text(crit ? tr('${checkLabel(k)} · critical', '${checkLabel(k)} · 严重') : checkLabel(k),
                  style: NwText.bodyMedium.copyWith(color: nw.text)),
              subtitle: Text('${c['detail'] ?? ''}', style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
              trailing: Text(ok ? tr('OK', '正常') : tr('Failed', '未通过'),
                  style: NwText.labelMedium.copyWith(color: color, fontWeight: FontWeight.w700)));
        }(),
    ];
  }

  /// 版本格子：站点一格、每只狗一格、每台登录着的客户端一格。跟站点配不上的（狗太老、App 太老）是橙色的小标。
  List<Widget> _versionRows(Map<String, dynamic> v) {
    final nw = Nw.of(context);
    final site = v['site'] is Map ? v['site'] as Map : null;
    final robots = v['robots'] is List ? (v['robots'] as List).whereType<Map>().toList() : const <Map>[];
    final clients = v['clients'] is List ? (v['clients'] as List).whereType<Map>().toList() : const <Map>[];
    final minApp = site?['min_app_level'];
    final heading = NwText.titleSmall.copyWith(color: nw.text);
    final none = NwText.bodyMedium.copyWith(color: nw.textSecondary);
    // 一格：是谁、哪一版；小标（配不配）压在右上角那一行 —— 放进版本格自己的那一行会把版本那句挤掉一半
    Widget tile(String entity, String version, {String? note, NwTone tone = NwTone.neutral}) =>
        Stack(fit: StackFit.passthrough, children: [
          NwVersionCard(entity: entity, version: version),
          if (note != null)
            Positioned(top: Nw.s8, right: Nw.s8, child: NwStatusPill(label: note, tone: tone)),
        ]);
    return [
      if (site != null) ...[
        NwGrid(minTile: _versionTile, children: [tile(tr('Site', '站点'), '${site['version'] ?? '?'}')]),
        nwGap16,
      ],
      Text(tr('Robot software', '机器人上的软件'), style: heading),
      nwGap8,
      if (robots.isEmpty) Text(tr('No robots.', '还没有机器人。'), style: none),
      NwGrid(minTile: _versionTile, children: [
        for (final r in robots)
          () {
            final bad = r['verdict'] == 'agent_too_old' || r['verdict'] == 'site_too_old';
            return tile(
                '${r['robot_id']}',
                [
                  if (r['agent'] != null) tr('agent ${r['agent']}', '代理 ${r['agent']}'),
                  if (r['level'] != null) tr('level ${r['level']}', '级别 ${r['level']}'),
                  r['online'] == true ? tr('online', '在线') : tr('offline', '离线'),
                ].join(' · '),
                note: verdictLabel(r['verdict']),
                tone: bad ? NwTone.warning : (r['verdict'] == 'ok' ? NwTone.success : NwTone.neutral));
          }(),
      ]),
      nwGap16,
      Text(tr('Signed-in apps', '登录着的 App'), style: heading),
      nwGap8,
      if (clients.isEmpty)
        Text(tr('No app has reported its version yet (older apps do not).', '尚无 App 上报版本（旧版 App 不上报）。'),
            style: none),
      NwGrid(minTile: _versionTile, children: [
        for (final c in clients)
          () {
            final old = c['kind'] == 'app' && c['api_level'] is int && minApp is int && (c['api_level'] as int) < minApp;
            return tile(
                '${c['name']}',
                [
                  c['kind'] == 'web' ? tr('Web console', '网页值班台') : tr('Phone app ${c['version'] ?? '?'}', '手机 App ${c['version'] ?? '?'}'),
                  if (c['platform'] != null) '${c['platform']}',
                ].join(' · '),
                note: old ? tr('Too old', '版本过旧') : null,
                tone: NwTone.warning);
          }(),
      ]),
    ];
  }

  /// 版本格最窄这么宽：「代理 1.4.0 · 级别 2 · 在线」那一句摆得下。
  static const double _versionTile = 320;

  @override
  Widget build(BuildContext context) {
    final h = _health;
    final v = _versions;
    final nw = Nw.of(context);
    final refresh = IconButton(
        key: SiteHealthPage.refreshKey,
        tooltip: tr('Refresh', '刷新'),
        icon: const Icon(Icons.refresh_rounded),
        onPressed: _loading ? null : _load);
    // 整页一次建完（不是懒加载的列表）：页面不长，下面的诊断包、入口随时找得到
    final body = LayoutBuilder(builder: (context, box) {
      final side = nwSideInset(box.maxWidth);
      return SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(side, Nw.s16, side, Nw.s24),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            NwSectionLabel(tr('Site status', '站点状态')),
            nwGap8,
            if (_loading)
              NwListCard(children: [
                ListTile(
                    key: SiteHealthPage.loadingKey,
                    leading: Icon(Icons.hourglass_empty_rounded, color: nw.textSecondary),
                    title: Text(tr('Loading…', '加载中…'), style: NwText.bodyMedium.copyWith(color: nw.textSecondary))),
              ]),
            if (_error != null)
              NwListCard(tone: NwTone.warning, children: [
                ListTile(
                    leading: PriorityShape(level: 'P2', color: nw.warning, size: 16),
                    title: Text(tr("Can't read the site's health: $_error", '无法读取站点体检结果：$_error'),
                        style: NwText.bodyMedium.copyWith(color: nw.text))),
              ]),
            if (h != null) NwListCard(children: [_overall(h), ..._checks(h)]),
            // 版本一览还没读到、读不到：这一节整个不显示（体检照样看），不拿上一次的当现在的
            if (v != null) ...[
              nwGap24,
              NwSectionLabel(tr('Firmware & software', '固件与软件')),
              nwGap8,
              ..._versionRows(v),
            ],
            nwGap24,
            NwSectionLabel(tr('Maintenance', '维护')),
            nwGap8,
            NwCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Container(
                    width: 40,
                    height: 40,
                    decoration: BoxDecoration(color: nw.surfaceVariant, borderRadius: BorderRadius.circular(Nw.r8)),
                    child: Icon(Icons.description_rounded, size: 24, color: nw.textSecondary)),
                const SizedBox(width: Nw.s16),
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  Text(tr('Diagnostics bundle', '诊断包'), style: NwText.titleSmall.copyWith(color: nw.text)),
                  nwGap4,
                  Text(
                      tr(
                          'Diagnostics include the health report, recent logs and settings, with no passwords or private keys.',
                          '诊断包含体检结果、近期日志和配置，不含密码和私钥。'),
                      style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
                ])),
              ]),
              nwGap16,
              Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
                NwButton(
                    buttonKey: SiteHealthPage.bundleKey,
                    variant: NwButtonVariant.outline,
                    icon: Icons.download_rounded,
                    label: _saving && !_sharing ? tr('Saving…', '正在保存…') : tr('Save diagnostics bundle', '保存诊断包'),
                    onPressed: _saving ? null : _bundle),
                if (widget.canShare ?? (Platform.isAndroid || Platform.isIOS))
                  NwButton(
                      buttonKey: SiteHealthPage.shareKey,
                      variant: NwButtonVariant.outline,
                      icon: Icons.share_rounded,
                      label: _saving && _sharing ? tr('Preparing…', '正在准备…') : tr('Share…', '分享…'),
                      onPressed: _saving ? null : () => _bundle(share: true)),
              ]),
              if (_msg.isNotEmpty) ...[
                nwGap16,
                SelectableText(_msg, key: SiteHealthPage.msgKey, style: NwText.bodyMedium.copyWith(color: nw.text)),
              ],
            ])),
            if (widget.footer != null) ...[nwGap24, widget.footer!],
          ]));
    });
    if (widget.embedded) {
      return Column(children: [
        NwHeader(overline: tr('Admin', '管理'), title: tr('Site health', '站点体检'), trailing: [refresh]),
        Expanded(child: body),
      ]);
    }
    return Scaffold(
      appBar: AppBar(title: Text(tr('Site health', '站点体检')), actions: [refresh]),
      body: body,
    );
  }
}
