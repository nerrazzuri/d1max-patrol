/// 扫配对码的相机页（App V2）。只在手机上有（相机插件没有 Linux / Windows 版）；扫到第一个码就回去，
/// 认不认得出来归 `site_add.dart`（不是站点的码它会说）。相机起不来（没给权限）说清楚怎么办。
library;

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import 'theme.dart';

/// 开相机扫一个码；没扫、退出来了回 null。
Future<String?> scanSiteCode(BuildContext context) =>
    Navigator.push<String>(context, MaterialPageRoute<String>(builder: (_) => const SiteScanPage()));

class SiteScanPage extends StatefulWidget {
  const SiteScanPage({super.key});

  @override
  State<SiteScanPage> createState() => _SiteScanPageState();
}

class _SiteScanPageState extends State<SiteScanPage> {
  final _camera = MobileScannerController(formats: const [BarcodeFormat.qrCode]);
  bool _done = false;

  @override
  void dispose() {
    _camera.dispose();
    super.dispose();
  }

  void _found(BarcodeCapture capture) {
    if (_done) return;
    for (final b in capture.barcodes) {
      final v = b.rawValue;
      if (v == null || v.isEmpty) continue;
      _done = true; // 相机一秒回好几帧：只回去一次
      Navigator.pop(context, v);
      return;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: D1Color.well,
      appBar: AppBar(title: Text(tr('Scan site code', '扫配对码'))),
      body: Stack(
        fit: StackFit.expand,
        children: [
          MobileScanner(
            controller: _camera,
            onDetect: _found,
            errorBuilder: (context, error) => Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  error.errorCode == MobileScannerErrorCode.permissionDenied
                      ? tr(
                          'Camera access is off for this app. Allow it in the phone settings, or go back and paste the code.',
                          '这个 App 没有相机权限。到手机设置里允许，或者返回去粘贴配对码。',
                        )
                      : tr("The camera didn't start. Go back and paste the code instead.", '相机没起来。返回去粘贴配对码。'),
                  textAlign: TextAlign.center,
                  style: D1Text.body,
                ),
              ),
            ),
          ),
          Align(
            alignment: Alignment.bottomCenter,
            child: Container(
              width: double.infinity,
              color: const Color(0xB3000000),
              padding: const EdgeInsets.all(16),
              child: Text(
                tr('Point the camera at the code on the console: Admin, System.', '对准值班台上的二维码：管理、系统。'),
                textAlign: TextAlign.center,
                style: D1Text.body,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
