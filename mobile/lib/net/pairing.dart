/// 配对码（App V2）：扫站点值班台上的二维码（或粘贴那一行字）就把站点加进来，不用手抄 64 位指纹。
///
/// 形状跟站点的 `d1max_site/pairing.py` 对着：`D1MAXSITE1.<base64url(JSON)>`，JSON 是
/// `{"n": 站点名, "u": 地址, "f": 证书指纹}`。**里面没有账号口令**：加进来之后照样要登录。
///
/// 别人给的码能把手机指到别的站点去，所以读出来之后界面上先把三样摆出来让人核对，确认了才存
/// （`ui/site_add.dart`）。这里只管读、只管查形状。
library;

import 'dart:convert';

import '../l10n.dart';

const String pairingPrefix = 'D1MAXSITE1.';

/// 狗的开通码的前缀：扫错了（扫成狗的码）要说清楚，不是只说「坏了」。
const String robotCodePrefix = 'D1MAX1.';

class PairingInfo {
  final String name;
  final String url;
  final String fingerprint;
  const PairingInfo({required this.name, required this.url, required this.fingerprint});
}

/// 读不出来（给人看的原因）。
class PairingException implements Exception {
  final String message;
  const PairingException(this.message);
  @override
  String toString() => message;
}

final RegExp _fp = RegExp(r'^[0-9a-f]{64}$');

PairingInfo decodePairing(String raw) {
  final code = raw.trim();
  if (code.startsWith(robotCodePrefix)) {
    throw PairingException(
      tr(
        "That's a robot activation code, not a site code. Use the code on the console's System page.",
        '这是狗的开通码，不是站点的配对码。请用值班台「系统」页上的码。',
      ),
    );
  }
  if (!code.startsWith(pairingPrefix)) {
    throw PairingException(tr("That's not a D1 Max site code.", '这不是 D1 Max 站点的配对码。'));
  }
  final bad = PairingException(tr('The site code is damaged. Scan or copy it again.', '配对码坏了。重新扫一次或者重新复制。'));
  final Object? d;
  try {
    final body = code.substring(pairingPrefix.length);
    d = jsonDecode(utf8.decode(base64Url.decode(body + '=' * ((4 - body.length % 4) % 4))));
  } on FormatException {
    throw bad;
  }
  if (d is! Map) throw bad;
  final n = d['n'], u = d['u'], f = d['f'];
  if (n is! String || u is! String || f is! String) throw bad;
  if (n.trim().isEmpty || n.length > 64 || !_fp.hasMatch(f)) throw bad;
  final Uri uri;
  try {
    uri = Uri.parse(u);
  } on FormatException {
    throw bad;
  }
  // 只收 https://主机[:端口]：不带账号、路径、查询串（站点那头出码时也只出这个形状）。
  if (uri.scheme != 'https' ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      (uri.path.isNotEmpty && uri.path != '/') ||
      uri.hasQuery ||
      uri.hasFragment ||
      u.length > 200) {
    throw bad;
  }
  return PairingInfo(name: n.trim(), url: u.endsWith('/') ? u.substring(0, u.length - 1) : u, fingerprint: f);
}
