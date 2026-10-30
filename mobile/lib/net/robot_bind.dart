/// 手机跟**机器人的配网服务**说话（W42，`docs/W42-机器人绑定-设计.md` §4、§9）。
///
/// 只在添加机器人、换 WiFi、工程师解绑这几步用：手机扫上装贴纸 → 连机器人的热点 → 经热点找机器人
/// 上的配网服务（HTTPS，**钉贴纸上的证书指纹**：热点密码印在码里、热点上的流量别人能看，WiFi 密码
/// 不能被偷看）。每个请求带贴纸上的绑定码（`X-Bind-Code`）。平时手机还是只连站点。
///
/// 贴纸二维码 `D1MAXBOT1.<base64url(JSON)>`：`s` 序列号、`c` 绑定码、`w` 热点名、`p` 热点密码、
/// `f` 配网证书指纹（前 32 位十六进制）、可选 `a` 配网服务地址（缺省 [defaultBindAddr]）。
/// 跟 `d1max_contract.binding` 同一个格式。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';

import '../l10n.dart';

const String stickerPrefix = 'D1MAXBOT1.';
const String unbindPrefix = 'D1MAXUNBIND1.';

/// 3588 在路由器 LAN1 上的地址（热点上的手机找配网服务）：出厂配置定死，贴纸可以带别的。
const String defaultBindAddr = '192.168.1.2:8450';

/// 热点密码：所有机器人统一一个（用户 2026-10-30：热点只用来配网、要站点同意、跟内网隔开，不用保密）。
/// 跟 `d1max_contract.binding.HOTSPOT_PSK` 一样。
const String hotspotPsk = 'NexusWarden';

/// 热点名：`NW-` + 序列号后 6 位。
String hotspotSsid(String serial) => 'NW-${serial.length > 6 ? serial.substring(serial.length - 6) : serial}';

const String _codeAlphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

/// 手输的绑定码 → 16 位大写（`-`、空格不计较，`I`/`L` 当 `1`、`O` 当 `0`）。不对回 null。
String? normalizeBindCode(String raw) {
  final s = raw.toUpperCase().replaceAll(RegExp(r'[\s-]'), '')
      .replaceAll('I', '1').replaceAll('L', '1').replaceAll('O', '0');
  if (s.length != 16 || s.split('').any((ch) => !_codeAlphabet.contains(ch))) return null;
  return s;
}

/// 贴纸二维码坏了、或者手动连热点的时候：手输序列号和绑定码（贴纸上印着）。**核不了机器人的证书**
/// （指纹只在二维码里），[RobotSticker.fingerprint] 是空的。不对抛 [FormatException]。
RobotSticker manualSticker(String serial, String code) {
  final s = serial.trim();
  if (!RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$').hasMatch(s)) {
    throw FormatException(tr('Enter the serial number printed on the sticker', '填贴纸上印的序列号'));
  }
  final c = normalizeBindCode(code);
  if (c == null) {
    throw FormatException(tr('The binding code is 16 letters and digits', '绑定码是 16 位字母和数字'));
  }
  return RobotSticker(s, c, hotspotSsid(s), hotspotPsk, '');
}

class RobotSticker {
  final String serial;
  final String code;
  final String ssid;
  final String psk;
  /// 配网证书指纹前 32 位；空 = 手输的，核不了机器人身份。
  final String fingerprint;
  final String addr;
  const RobotSticker(this.serial, this.code, this.ssid, this.psk, this.fingerprint,
      {this.addr = defaultBindAddr});
}

/// 读贴纸二维码。读不懂抛 [FormatException]（带给人看的原因）。
RobotSticker parseSticker(String raw) {
  final t = raw.trim();
  if (!t.startsWith(stickerPrefix)) {
    throw FormatException(tr("This isn't a robot sticker", '这不是机器人身上的贴纸码'));
  }
  final body = t.substring(stickerPrefix.length);
  Object? d;
  try {
    d = jsonDecode(utf8.decode(base64Url.decode(base64Url.normalize(body))));
  } catch (_) {
    throw FormatException(tr('The sticker code is damaged', '贴纸码坏了'));
  }
  if (d is! Map) throw FormatException(tr('The sticker code is damaged', '贴纸码坏了'));
  final s = d['s'], c = d['c'], w = d['w'], p = d['p'], f = d['f'], a = d['a'];
  if (s is! String || c is! String || w is! String || p is! String || f is! String ||
      !RegExp(r'^[0-9a-f]{32}$').hasMatch(f) || !RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$').hasMatch(s) ||
      (a != null && (a is! String || !RegExp(r'^[A-Za-z0-9.\-]+:\d{1,5}$').hasMatch(a)))) {
    throw FormatException(tr('The sticker code is damaged', '贴纸码坏了'));
  }
  return RobotSticker(s, c, w, p, f, addr: a is String ? a : defaultBindAddr);
}

/// 现场 WiFi 名和密码对不对（跟机器人那头同一个规矩）：对 → null；不对 → 给人看的理由。
String? checkWifi(String ssid, String psk) {
  final n = utf8.encode(ssid).length;
  if (n < 1 || n > 32) return tr('Enter the WiFi name (at most 32 bytes)', '填 WiFi 名（最长 32 字节）');
  if (psk.isEmpty) return null;
  final hex = RegExp(r'^[0-9a-fA-F]{64}$').hasMatch(psk);
  final ascii = psk.length >= 8 && psk.length <= 63 && psk.codeUnits.every((u) => u >= 0x20 && u < 0x7f);
  if (!hex && !ascii) {
    return tr('WiFi password must be 8–63 letters, digits or symbols (leave empty if open)',
        'WiFi 密码要 8–63 位（字母、数字、符号），不加密的留空');
  }
  return null;
}

/// 连机器人热点。Android 10 起用系统的「指定网络」（这个 App 的请求走热点，手机别的照常上网）；
/// 别的平台（iOS 这一期、老 Android）连不上回 false，界面上教人去系统设置里手动连。
abstract class HotspotJoiner {
  Future<bool> join(String ssid, String psk);
  Future<void> leave();
}

class ChannelHotspotJoiner implements HotspotJoiner {
  static const MethodChannel _ch = MethodChannel('d1max/hotspot');
  const ChannelHotspotJoiner();

  @override
  Future<bool> join(String ssid, String psk) async {
    try {
      return await _ch.invokeMethod<bool>('join', <String, String>{'ssid': ssid, 'psk': psk}) ?? false;
    } on MissingPluginException {
      return false;
    } on PlatformException {
      return false;
    }
  }

  @override
  Future<void> leave() async {
    try {
      await _ch.invokeMethod<void>('leave');
    } on MissingPluginException {
      return;
    } on PlatformException {
      return;
    }
  }
}

/// 机器人配网服务回的错（403 绑定码不对、409 状态不对……）；连不上是 0。
class RobotBindError implements Exception {
  final int status;
  final String message;
  const RobotBindError(this.status, this.message);
  @override
  String toString() => message;
}

/// 跟一台机器人的配网服务说话（测试换成假的）。
abstract class RobotBindLink {
  /// `{serial, state, phase, error, site_name, check?, wifi, change?, change_result?}`。
  Future<Map<String, dynamic>> status();
  Future<Map<String, dynamic>> bind(
      {required String ssid, required String psk, required String siteUrl,
      required String siteFingerprint, required String siteName});
  Future<Map<String, dynamic>> wifi(String ssid, String psk);
  Future<Map<String, dynamic>> unbind(String token);
  Future<Map<String, dynamic>> cancel();
  void close();
}

/// 经热点找配网服务。
class RobotHotspot implements RobotBindLink {
  final RobotSticker sticker;
  final Duration timeout;
  final HttpClient _io;
  RobotHotspot(this.sticker, {this.timeout = const Duration(seconds: 15)})
      : _io = HttpClient(context: SecurityContext(withTrustedRoots: false)) {
    _io.connectionTimeout = const Duration(seconds: 5);
    // 手输的（没有指纹）核不了：接受
    _io.badCertificateCallback = (cert, host, port) =>
        sticker.fingerprint.isEmpty || sha256.convert(cert.der).toString().substring(0, 32) == sticker.fingerprint;
  }

  @override
  void close() => _io.close(force: true);

  Future<Map<String, dynamic>> _send(String method, String path, [Object? body]) async {
    final uri = Uri.parse('https://${sticker.addr}$path');
    try {
      final req = await _io.openUrl(method, uri).timeout(timeout);
      req.headers.set('X-Bind-Code', sticker.code);
      if (body != null) {
        // 带上长度：不带的话 Dart 用分块发送，机器人那头按 Content-Length 读，读到的是空的（演示站点上查出来的）
        final raw = utf8.encode(jsonEncode(body));
        req.headers.contentType = ContentType.json;
        req.contentLength = raw.length;
        req.add(raw);
      } else {
        req.contentLength = 0;
      }
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join().timeout(timeout);
      Object? d;
      try {
        d = jsonDecode(text);
      } on FormatException {
        d = null;
      }
      final m = d is Map ? Map<String, dynamic>.from(d) : <String, dynamic>{};
      if (resp.statusCode != 200) {
        throw RobotBindError(resp.statusCode, '${m['error'] ?? resp.statusCode}');
      }
      return m;
    } on RobotBindError {
      rethrow;
    } on HandshakeException {
      throw RobotBindError(0, tr("This isn't the robot on the sticker (certificate doesn't match)",
          '连上的不是贴纸上这台机器人（证书对不上）'));
    } catch (e) {
      throw RobotBindError(0, tr("Can't reach the robot over its hotspot: $e", '经热点连不上机器人：$e'));
    }
  }

  @override
  Future<Map<String, dynamic>> status() => _send('GET', '/status');

  @override
  Future<Map<String, dynamic>> bind(
          {required String ssid, required String psk, required String siteUrl,
          required String siteFingerprint, required String siteName}) =>
      _send('POST', '/bind', <String, dynamic>{
        'wifi': <String, String>{'ssid': ssid, 'psk': psk},
        'site': <String, String>{'url': siteUrl, 'fp': siteFingerprint, 'name': siteName},
      });

  @override
  Future<Map<String, dynamic>> wifi(String ssid, String psk) =>
      _send('POST', '/wifi', <String, String>{'ssid': ssid, 'psk': psk});

  @override
  Future<Map<String, dynamic>> unbind(String token) =>
      _send('POST', '/unbind', <String, String>{'token': token});

  @override
  Future<Map<String, dynamic>> cancel() => _send('POST', '/cancel', <String, dynamic>{});
}
