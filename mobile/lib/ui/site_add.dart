/// 添加站点（App V2）：扫值班台「系统」页上的二维码，或者粘贴那一行配对码，或者手填。
///
/// 配对码里只有站点名、地址、证书指纹（`net/pairing.dart`），**没有账号口令**。扫完把三样填进表单、
/// 摆在眼前：别人给的码能把手机指到别的站点去，所以存之前人要跟值班台上显示的核对一遍。
///
/// 2026-10-14 按设计稿重做（P1）：换成新设计系统的页头、按钮、输入框；流程和每一栏没变。
library;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/pairing.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p1_entry.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_scan.dart';

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

  /// 表单里这三样是从配对码来的（提示人去核对）。
  bool _fromCode = false;

  @override
  void dispose() {
    for (final c in [_name, _url, _fp]) {
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
      builder: (c) {
        final nw = Nw.of(c);
        return AlertDialog(
          scrollable: true,
          backgroundColor: nw.surface,
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: nw.outline)),
          title: Text(tr('Paste site code', '粘贴配对码'), style: NwText.titleLarge.copyWith(color: nw.text)),
          content: NwField(
            fieldKey: const Key('pair-code'),
            controller: code,
            autofocus: true,
            autocorrect: false,
            enableSuggestions: false,
            maxLines: 4,
            minLines: 2,
            mono: true,
            hint: '$pairingPrefix…',
          ),
          actions: [
            NwButton(
                label: tr('Cancel', '取消'),
                variant: NwButtonVariant.ghost,
                onPressed: () => Navigator.pop(c, false)),
            NwButton(
              buttonKey: const Key('pair-code-ok'),
              label: tr('Use code', '用这个码'),
              onPressed: () => Navigator.pop(c, true),
            ),
          ],
        );
      },
    );
    if (ok != true || !mounted) return;
    _take(code.text);
  }

  void _save() {
    final bad = checkSiteEntry(_url.text, _fp.text);
    if (bad != null || _name.text.trim().isEmpty) {
      _say(tr('Not saved: ${bad ?? 'enter a name'}', '未保存：${bad ?? '请填写名称'}'));
      return;
    }
    Navigator.pop(
      context,
      SiteEntry(
        name: _name.text.trim(),
        url: _url.text.trim(),
        fingerprint: normalizeFingerprint(_fp.text),
        username: '',                   // 账号第一次登录时再输（用户 10-30：加站点时还没有账号，填什么）
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scan = widget.scanner != null || canScanCodes;
    final c = Nw.of(context);
    const gap = SizedBox(height: Nw.s16);
    return Scaffold(
      backgroundColor: c.bg,
      appBar: nwHeaderBar(
          context, NwHeader(leading: BackButton(color: c.text), title: tr('Add site', '添加站点'))),
      // 不用懒加载的列表：横屏弹键盘时剩不了多高，表单要整个都在（能滚到），不然光标落不到输入框上。
      body: SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(Nw.s24),
          child: Center(
            // 宽屏（桌面版）上表单不拉满一整行
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 560),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    tr(
                      'The site code is on the duty console under Admin → System → "Add this site to the phone app".',
                      '配对码位于值班台「管理 → 系统 →「把这个站点加到手机 App」」。',
                    ),
                    style: NwText.bodySmall.copyWith(color: c.textSecondary),
                  ),
                  gap,
                  Wrap(
                    spacing: Nw.s16,
                    runSpacing: Nw.s8,
                    children: [
                      if (scan)
                        NwButton(
                          buttonKey: const Key('site-scan'),
                          onPressed: _scan,
                          icon: Icons.qr_code_scanner_rounded,
                          label: tr('Scan code', '扫码'),
                        ),
                      NwButton(
                        buttonKey: const Key('site-paste'),
                        onPressed: _paste,
                        icon: Icons.content_paste_rounded,
                        label: tr('Paste code', '粘贴配对码'),
                        variant: NwButtonVariant.outline,
                      ),
                    ],
                  ),
                  gap,
                  if (_fromCode)
                    Padding(
                      key: const Key('site-check-note'),
                      padding: const EdgeInsets.only(bottom: Nw.s16),
                      child: NwNotice(
                        text: tr(
                          'Check that the site name and certificate fingerprint match the duty console, then save.',
                          '请核对下方站点名称和证书指纹与值班台一致后再保存。',
                        ),
                      ),
                    ),
                  NwField(fieldKey: const Key('site-name'), controller: _name, label: tr('Name', '名字')),
                  gap,
                  NwField(
                    fieldKey: const Key('site-url'),
                    controller: _url,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    label: tr('Address (https://…:8443)', '地址（https://…:8443）'),
                  ),
                  gap,
                  NwField(
                    fieldKey: const Key('site-fp'),
                    controller: _fp,
                    autocorrect: false,
                    enableSuggestions: false,
                    minLines: 1,
                    maxLines: 3,
                    mono: true,
                    label: tr(
                      'Certificate fingerprint (d1max-site fingerprint)',
                      '证书指纹（d1max-site fingerprint）',
                    ),
                  ),
                  const SizedBox(height: Nw.s24),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      Flexible(
                        child: NwButton(
                          label: tr('Cancel', '取消'),
                          variant: NwButtonVariant.ghost,
                          onPressed: () => Navigator.pop(context),
                        ),
                      ),
                      const SizedBox(width: Nw.s16),
                      Flexible(
                        child: NwButton(
                          buttonKey: const Key('site-save'),
                          label: tr('Save', '保存'),
                          onPressed: _save,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
