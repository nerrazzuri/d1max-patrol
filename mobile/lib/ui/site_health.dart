/// 站点体检（C-B4；站点那头是商业化 A1 / A7）：管理员在手机上看站点好不好 —— 每一项检查过没过、
/// 哪几项是要紧的、站点和每台狗的版本配不配、谁还登录着哪一版 App；还能把诊断包存到手机上发给厂家。
///
/// 没过的要紧项用 P1 的形状和颜色，别的没过的用 P2；过了的不上色。站点给的明细是中文（英文界面也摆
/// 出来，比没有强）。读不到就说读不到，不拿上一次的当现在的。
library;

import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../net/site_client.dart';
import 'theme.dart';
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
      'robots' => tr('Robots', '机器狗'),
      _ => key,
    };

/// 狗上的代理跟站点配不配。
String verdictLabel(Object? v) => switch (v) {
      'ok' => tr('OK', '正常'),
      'agent_too_old' => tr('Robot too old', '狗太老'),
      'site_too_old' => tr('Site too old', '站点太老'),
      _ => tr('Unknown', '不清楚'),
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
  const SiteHealthPage(
      {super.key,
      required this.api,
      this.save = saveBundleWithPicker,
      this.share = shareBundle,
      this.canShare,
      this.now = DateTime.now});

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
            ? tr('Handed $name ($kb KB) to the share sheet', '交给分享了（$kb KB）：$name')
            : tr('Not shared: cancelled', '没分享：取消了');
      } else {
        final path = await widget.save(name, bytes);
        msg = path == null
            ? tr('Not saved: cancelled', '没存：取消了')
            : tr('Saved $kb KB to $path', '存好了（$kb KB）：$path');
      }
    } on FileSystemException catch (e) {
      msg = share
          ? tr('Diagnostics bundle not shared: ${e.message}', '诊断包没分享出去：${e.message}')
          : tr('Diagnostics bundle not saved: ${e.message}', '诊断包没存上：${e.message}');
    } on Object catch (e) {
      msg = share
          ? tr('Diagnostics bundle not shared: $e', '诊断包没分享出去：$e')
          : tr('Diagnostics bundle not saved: $e', '诊断包没存上：$e');
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
    final ok = h['ok'] == true;
    final critical = h['critical_ok'] == false;
    final color = ok ? D1Color.text : (critical ? D1Color.p1Text : D1Color.p2);
    return ListTile(
        key: SiteHealthPage.overallKey,
        leading: ok ? const Icon(Icons.check, color: D1Color.textSecondary) : PriorityShape(level: critical ? 'P1' : 'P2', color: color),
        title: Text(
            ok
                ? tr('All checks passed', '各项都正常')
                : critical
                    ? tr('Site is not healthy', '站点有要紧的问题')
                    : tr('Some checks need attention', '有几项要看一下'),
            style: D1Text.heading.copyWith(color: color)),
        subtitle: Text(tr('Site version ${h['version'] ?? '?'}', '站点版本 ${h['version'] ?? '?'}')));
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
          final color = ok ? D1Color.textSecondary : (crit ? D1Color.p1Text : D1Color.p2);
          return ListTile(
              key: SiteHealthPage.checkKey(k),
              dense: true,
              leading: ok ? const Icon(Icons.check, size: 18, color: D1Color.textSecondary) : PriorityShape(level: crit ? 'P1' : 'P2', color: color),
              title: Text(crit ? tr('${checkLabel(k)} · critical', '${checkLabel(k)} · 要紧') : checkLabel(k)),
              subtitle: Text('${c['detail'] ?? ''}'),
              trailing: Text(ok ? tr('OK', '正常') : tr('Failed', '没过'), style: D1Text.label.copyWith(color: color)));
        }(),
    ];
  }

  List<Widget> _versionRows(Map<String, dynamic> v) {
    final robots = v['robots'] is List ? (v['robots'] as List).whereType<Map>().toList() : const <Map>[];
    final clients = v['clients'] is List ? (v['clients'] as List).whereType<Map>().toList() : const <Map>[];
    final minApp = v['site'] is Map ? (v['site'] as Map)['min_app_level'] : null;
    return [
      Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
          child: Text(tr('Robot software', '狗上的软件'), style: D1Text.heading)),
      if (robots.isEmpty) ListTile(dense: true, title: Text(tr('No robots.', '还没有狗。'))),
      for (final r in robots)
        () {
          final bad = r['verdict'] == 'agent_too_old' || r['verdict'] == 'site_too_old';
          return ListTile(
              dense: true,
              leading: bad ? const PriorityShape(level: 'P2', color: D1Color.p2) : null,
              title: Text('${r['robot_id']}', style: D1Text.osd.copyWith(color: D1Color.text)),
              subtitle: Text([
                if (r['agent'] != null) tr('agent ${r['agent']}', '代理 ${r['agent']}'),
                if (r['level'] != null) tr('level ${r['level']}', '级别 ${r['level']}'),
                r['online'] == true ? tr('online', '在线') : tr('offline', '离线'),
              ].join(' · ')),
              trailing: Text(verdictLabel(r['verdict']), style: D1Text.label.copyWith(color: bad ? D1Color.p2 : D1Color.textSecondary)));
        }(),
      Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
          child: Text(tr('Signed-in apps', '登录着的 App'), style: D1Text.heading)),
      if (clients.isEmpty)
        ListTile(
            dense: true,
            title: Text(tr('No app has reported its version yet (older apps do not).', '还没有 App 报过版本（老版本 App 不报）。'))),
      for (final c in clients)
        () {
          final old = c['kind'] == 'app' && c['api_level'] is int && minApp is int && (c['api_level'] as int) < minApp;
          return ListTile(
              dense: true,
              leading: old ? const PriorityShape(level: 'P2', color: D1Color.p2) : null,
              title: Text('${c['name']}'),
              subtitle: Text([
                c['kind'] == 'web' ? tr('Web console', '网页值班台') : tr('Phone app ${c['version'] ?? '?'}', '手机 App ${c['version'] ?? '?'}'),
                if (c['platform'] != null) '${c['platform']}',
              ].join(' · ')),
              trailing: old ? Text(tr('Too old', '太老'), style: D1Text.label.copyWith(color: D1Color.p2)) : null);
        }(),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final h = _health;
    final v = _versions;
    return Scaffold(
      appBar: AppBar(title: Text(tr('Site health', '站点体检')), actions: [
        IconButton(
            key: SiteHealthPage.refreshKey,
            tooltip: tr('Refresh', '刷新'),
            icon: const Icon(Icons.refresh),
            onPressed: _loading ? null : _load),
      ]),
      body: ListView(children: [
        if (_loading) ListTile(key: SiteHealthPage.loadingKey, title: Text(tr('Loading…', '正在读…'))),
        if (_error != null)
          ListTile(
              leading: const PriorityShape(level: 'P2', color: D1Color.p2),
              title: Text(tr("Can't read the site's health: $_error", '站点体检没读到：$_error'),
                  style: const TextStyle(color: D1Color.p2))),
        if (h != null) ...[_overall(h), const Divider(height: 1), ..._checks(h)],
        if (v != null) ..._versionRows(v),
        const Divider(height: 24),
        Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Text(
                tr(
                    'The diagnostics bundle holds the health report, recent logs and settings. It has no passwords or private keys. '
                        'Send it to support when asked.',
                    '诊断包里是体检结果、最近的日志和配置，没有口令和私钥。厂家要的时候发给他们。'),
                style: D1Text.caption.copyWith(color: D1Color.textSecondary))),
        Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Wrap(spacing: 12, runSpacing: 8, children: [
              OutlinedButton.icon(
                  key: SiteHealthPage.bundleKey,
                  icon: const Icon(Icons.download),
                  label: Text(_saving && !_sharing ? tr('Saving…', '正在存…') : tr('Save diagnostics bundle', '保存诊断包')),
                  onPressed: _saving ? null : _bundle),
              if (widget.canShare ?? (Platform.isAndroid || Platform.isIOS))
                OutlinedButton.icon(
                    key: SiteHealthPage.shareKey,
                    icon: const Icon(Icons.share),
                    label: Text(_saving && _sharing ? tr('Preparing…', '正在取…') : tr('Share…', '分享…')),
                    onPressed: _saving ? null : () => _bundle(share: true)),
            ])),
        if (_msg.isNotEmpty)
          Padding(
              padding: const EdgeInsets.all(16),
              child: SelectableText(_msg, key: SiteHealthPage.msgKey, style: D1Text.body)),
        const SizedBox(height: 24),
      ]),
    );
  }
}
