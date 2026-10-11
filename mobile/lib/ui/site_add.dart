/// 添加站点（App V2）：扫值班台「系统」页上的二维码，或者粘贴那一行配对码，或者手填。
///
/// 配对码里只有站点名、地址、证书指纹（`net/pairing.dart`），**没有账号口令**。扫完把三样填进表单、
/// 摆在眼前：别人给的码能把手机指到别的站点去，所以存之前人要跟值班台上显示的核对一遍。
library;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../net/pairing.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'site_scan.dart';
import 'theme.dart';

/// 扫一个码，回扫到的字（没扫、退出来了回 null）。测试换成假的。
typedef CodeScanner = Future<String?> Function(BuildContext context);

/// 这个平台能不能扫码：只有手机（安卓、iOS）有相机插件；桌面版粘贴。
bool get canScanCodes =>
    defaultTargetPlatform == TargetPlatform.android || defaultTargetPlatform == TargetPlatform.iOS;

class SiteAddPage extends StatefulWidget {
  /// 不给：按平台（手机上开相机，桌面版没有「扫码」按钮）。
  final CodeScanner? scanner;
  const SiteAddPage({super.key, this.scanner});

  @override
  State<SiteAddPage> createState() => _SiteAddPageState();
}

class _SiteAddPageState extends State<SiteAddPage> {
  final _name = TextEditingController();
  final _url = TextEditingController(text: 'https://');
  final _fp = TextEditingController();
  final _user = TextEditingController();

  /// 表单里这三样是从配对码来的（提示人去核对）。
  bool _fromCode = false;

  @override
  void dispose() {
    for (final c in [_name, _url, _fp, _user]) {
      c.dispose();
    }
    super.dispose();
  }

  void _say(String s) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(s)));

  void _take(String raw) {
    final PairingInfo p;
    try {
      p = decodePairing(raw);
    } on PairingException catch (e) {
      _say(e.message);
      return;
    }
    setState(() {
      _name.text = p.name;
      _url.text = p.url;
      _fp.text = p.fingerprint;
      _fromCode = true;
    });
  }

  Future<void> _scan() async {
    final scan = widget.scanner ?? scanSiteCode;
    final raw = await scan(context);
    if (raw == null || !mounted) return;
    _take(raw);
  }

  Future<void> _paste() async {
    final code = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        scrollable: true,
        title: Text(tr('Paste site code', '粘贴配对码')),
        content: TextField(
          key: const Key('pair-code'),
          controller: code,
          autofocus: true,
          maxLines: 4,
          minLines: 2,
          style: D1Text.osd,
          decoration: const InputDecoration(hintText: '$pairingPrefix…'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
          FilledButton(
            key: const Key('pair-code-ok'),
            onPressed: () => Navigator.pop(c, true),
            child: Text(tr('Use code', '用这个码')),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    _take(code.text);
  }

  void _save() {
    final bad = checkSiteEntry(_url.text, _fp.text);
    if (bad != null || _name.text.trim().isEmpty || _user.text.trim().isEmpty) {
      _say(tr('Not saved: ${bad ?? 'enter a name and an account'}', '没保存：${bad ?? '名字和账号要填'}'));
      return;
    }
    Navigator.pop(
      context,
      SiteEntry(
        name: _name.text.trim(),
        url: _url.text.trim(),
        fingerprint: normalizeFingerprint(_fp.text),
        username: _user.text.trim(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scan = widget.scanner != null || canScanCodes;
    return Scaffold(
      appBar: AppBar(title: Text(tr('Add site', '添加站点'))),
      // 不用懒加载的列表：横屏弹键盘时剩不了多高，表单要整个都在（能滚到），不然光标落不到输入框上。
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              tr(
                'Get the site code from the duty console: Admin, System, "Add this site to the phone app".',
                '配对码在值班台上：管理、系统、「把这个站点加到手机 App」。',
              ),
              style: D1Text.caption,
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: [
                if (scan)
                  FilledButton.icon(
                    key: const Key('site-scan'),
                    onPressed: _scan,
                    icon: const Icon(Icons.qr_code_scanner),
                    label: Text(tr('Scan code', '扫码')),
                  ),
                OutlinedButton.icon(
                  key: const Key('site-paste'),
                  onPressed: _paste,
                  icon: const Icon(Icons.content_paste),
                  label: Text(tr('Paste code', '粘贴配对码')),
                ),
              ],
            ),
            const SizedBox(height: 16),
            if (_fromCode)
              Container(
                key: const Key('site-check-note'),
                margin: const EdgeInsets.only(bottom: 16),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: D1Color.selectBg,
                  border: Border.all(color: D1Color.select),
                  borderRadius: BorderRadius.circular(D1Radius.control),
                ),
                child: Text(
                  tr(
                    'Check that the site name and certificate fingerprint below match the ones on the console, then enter your account.',
                    '核对下面的站点名和证书指纹跟值班台上显示的一样，再填你的账号。',
                  ),
                  style: D1Text.body,
                ),
              ),
            TextField(
              key: const Key('site-name'),
              controller: _name,
              decoration: InputDecoration(labelText: tr('Name', '名字')),
            ),
            const SizedBox(height: 12),
            TextField(
              key: const Key('site-url'),
              controller: _url,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: InputDecoration(labelText: tr('Address (https://…:8443)', '地址（https://…:8443）')),
            ),
            const SizedBox(height: 12),
            TextField(
              key: const Key('site-fp'),
              controller: _fp,
              autocorrect: false,
              enableSuggestions: false,
              minLines: 1,
              maxLines: 3,
              style: D1Text.osd,
              decoration: InputDecoration(
                labelText: tr(
                  'Certificate fingerprint (d1max-site fingerprint)',
                  '证书指纹（d1max-site fingerprint）',
                ),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              key: const Key('site-user'),
              controller: _user,
              autocorrect: false,
              decoration: InputDecoration(labelText: tr('Account', '账号')),
            ),
            const SizedBox(height: 20),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(onPressed: () => Navigator.pop(context), child: Text(tr('Cancel', '取消'))),
                const SizedBox(width: 12),
                FilledButton(key: const Key('site-save'), onPressed: _save, child: Text(tr('Save', '保存'))),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
