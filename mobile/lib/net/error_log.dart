/// App 自己的错误记录（W55）：没接住的异常、Flutter 画面出错、站点回 5xx，记在内存里（最多 500 条，满了丢最老的），
/// 登录着的时候每 10 分钟传给站点一次（站点留 14 天、进诊断包）。不记画面、不记密码、不记请求体。
///
/// 外审 W55-1：
/// - 第 15 条：**每条记着是在哪个站点里出的**，只传给那个站点；换了站点，别的站点的、登录前的都扔掉
///   （一部手机可能登过好几个客户的站点）。
/// - 第 14 条：按 JSON 字节数分批（站点一次最多收 64 KiB），站点收不下的那一批扔掉，不卡住后面的。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'site_client.dart';

class ErrorLog {
  ErrorLog({this.max = 500, int Function()? now}) : _now = now ?? (() => DateTime.now().millisecondsSinceEpoch);

  /// 整个 App 共用这一份（站点客户端、画面出错的钩子都往这里记）。
  static final ErrorLog instance = ErrorLog();

  final int max;
  final int Function() _now;
  final List<Map<String, dynamic>> _items = <Map<String, dynamic>>[];
  SiteApi? _api;
  /// 现在接着的是哪个站点（空 = 没登录）。
  String _site = '';
  Timer? _timer;
  bool _sending = false;

  List<Map<String, dynamic>> get pending => List<Map<String, dynamic>>.unmodifiable(_items);

  /// 一批最多多少字节（留余量）。
  static const int batchBytes = 48000;

  void note(String kind, String message, {String detail = '', String page = ''}) {
    String cut(String s) => s.length > 2000 ? s.substring(0, 2000) : s;
    _items.add(<String, dynamic>{
      '_site': _site,
      'at_ms': _now(),
      'kind': kind,
      'page': page,
      'message': cut(message),
      'detail': cut(detail),
      'app_version': '$appVersion+$appBuild',
      'platform': kIsWeb ? 'web' : Platform.operatingSystem,
    });
    if (_items.length > max) _items.removeRange(0, _items.length - max);
  }

  /// 登录进站点时接上：之后每 [every] 传一次。换站点、退出时 [detach]。[site] 认站点用（地址 + 证书指纹）：
  /// 不是这个站点的记录（别的站点的、登录前的）都扔掉。
  void attach(SiteApi api, {String site = '', Duration every = const Duration(minutes: 10)}) {
    _timer?.cancel();
    _api = api;
    _site = site.isEmpty ? 'api-${identityHashCode(api)}' : site;
    _items.removeWhere((e) => e['_site'] != _site);
    _timer = Timer.periodic(every, (_) => unawaited(flush()));
  }

  void detach(SiteApi api) {
    if (!identical(_api, api)) return;
    _timer?.cancel();
    _timer = null;
    _api = null;
    _site = '';
  }

  /// 传一批（最多 200 条）。没登录、站点连不上：留着下次再传。
  Future<void> flush() async {
    final api = _api;
    if (api == null || api.session == null || _sending || _items.isEmpty) return;
    _sending = true;
    final site = _site;
    final batch = <Map<String, dynamic>>[];
    var bytes = 64;
    for (final e in _items) {
      if (e['_site'] != site) continue;
      final n = utf8.encode(jsonEncode(e)).length + 1;
      if (batch.isNotEmpty && (bytes + n > batchBytes || batch.length >= 200)) break;
      batch.add(e);
      bytes += n;
    }
    if (batch.isEmpty) {
      _sending = false;
      return;
    }
    final wire = [for (final e in batch) Map<String, dynamic>.of(e)..remove('_site')];
    try {
      await api.reportClientErrors(wire);
      _items.removeWhere((e) => batch.any((b) => identical(b, e)));
    } on SiteError catch (err) {
      // 站点收不下、格式不认：这一批扔掉（不卡住后面的）；连不上（status 0）：留着下次再传
      if (err.status == 413 || err.status == 400) {
        _items.removeWhere((e) => batch.any((b) => identical(b, e)));
      }
    } on Object {
      // 留着，下次再传
    } finally {
      _sending = false;
    }
  }
}

/// 接上 Flutter 的出错钩子（`main()` 里调一次）：记下来，原来的处理照旧。
void installErrorHooks([ErrorLog? log]) {
  final l = log ?? ErrorLog.instance;
  final prev = FlutterError.onError;
  FlutterError.onError = (details) {
    l.note('flutter', details.exceptionAsString(), detail: '${details.stack ?? ''}');
    prev?.call(details);
  };
  PlatformDispatcher.instance.onError = (error, stack) {
    l.note('uncaught', '$error', detail: '$stack');
    return false;
  };
}
