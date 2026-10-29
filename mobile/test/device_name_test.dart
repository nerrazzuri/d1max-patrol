// W46：上装 V2 的设备健康多了主控板温度、风扇、功放，设备名说人话。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('主控板、风扇、功放说人话', () {
    appLang.value = AppLang.zh;
    expect(deviceName('board'), '主控板温度');
    expect(deviceName('fan:stir'), '风扇 stir');
    expect(deviceName('amp'), '功放');
    expect(deviceName('lens:thermal'), '热像镜头');
    appLang.value = AppLang.en;
    expect(deviceName('fan:stir'), 'Fan stir');
    appLang.value = AppLang.zh;
  });
}
