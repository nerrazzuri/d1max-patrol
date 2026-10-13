/// 天气（W29，决策 41）。
///
/// 站点按现在的天气管狗：
/// - **正常**：照常。
/// - **下雨**：照常巡、照常派，全狗限速 0.3 m/s（湿地防打滑）。
/// - **雷暴**（大雨也算）：排程巡检暂停、在外巡的狗回待命点；**有入侵照样派狗**（告警里写明雷暴中出动）；限速。
///
/// 天气是站点联网查的（配了庄园坐标才查），也可以在这里手动切（保安、业主、管理员都能），**以手动为准**，
/// 到点回到自动。别的手机切了：站点推一帧 `weather`，这一页当场换。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

/// 三种天气给人看的名字。每次用的时候现取（语言能当场换）。
Map<String, String> get _names => {
      'normal': tr('Normal', '正常'),
      'rain': tr('Rain', '下雨'),
      'storm': tr('Thunderstorm', '雷暴'),
    };

/// 首页那一行：`天气：下雨（手动，gina）· 限速 0.3 m/s`。
String weatherText(Map<String, dynamic> w) {
  final label = '${w['label'] ?? w['condition'] ?? '?'}';
  final m = w['manual'];
  // 站点给的 label 是中文：英文界面按 condition 取自己的名字，不认识的才用站点原文。
  final enLabel = _names[w['condition']] ?? (w['condition'] == 'unknown' ? 'Unknown' : label);
  final who = w['source'] == 'manual' && m is Map
      ? tr(' (manual, ${m['by'] ?? ''})', '（手动，${m['by'] ?? ''}）')
      : '';
  final parts = <String>[tr('Weather: $enLabel$who', '天气：$label$who')];
  if (w['patrols_paused'] == true) parts.add(tr('Scheduled patrols paused', '排程巡检暂停'));
  final cap = w['speed_cap_mps'];
  if (cap is num) {
    parts.add(tr('Speed limit ${cap.toStringAsFixed(1)} m/s', '限速 ${cap.toStringAsFixed(1)} m/s'));
  }
  if (w['condition'] == 'unknown') {
    final a = w['auto'];
    parts.add(a is Map && a['enabled'] == true
        ? tr('Weather unavailable, treated as normal', '天气查不到，按正常管')
        : tr('No site coordinates set, manual only', '没配庄园坐标，只能手动切'));
  }
  return parts.join(' · ');
}

IconData weatherIcon(String condition) => switch (condition) {
      'storm' => Icons.thunderstorm,
      'rain' => Icons.umbrella,
      'normal' => Icons.wb_sunny,
      _ => Icons.help_outline,
    };

/// 这种天气的色调：雷暴停巡检，是要人知道的异常 —— 橙。下雨只是限速，不上色。
NwTone weatherTone(String condition) => condition == 'storm' ? NwTone.warning : NwTone.neutral;

/// 图标的颜色；不上色是 null（跟着所在的主题走）。[colors] 给当前上下文的那一套（`Nw.of(context)`）；
/// 不给按浅色那一套。
Color? weatherColor(String condition, [NwColors? colors]) => switch (weatherTone(condition)) {
      NwTone.neutral => null,
      final t => (colors ?? NwColors.light).fg(t),
    };

/// 手动切能挑的时长（小时）。
const List<int> weatherHours = <int>[1, 3, 6, 12, 24];

class SiteWeatherPage extends StatefulWidget {
  final SiteApi api;
  const SiteWeatherPage({super.key, required this.api});

  static Key setKey(String c) => Key('weather-$c');
  static Key hoursKey(int h) => Key('weather-hours-$h');
  static const Key msgKey = Key('weather-msg');

  @override
  State<SiteWeatherPage> createState() => _SiteWeatherPageState();
}

class _SiteWeatherPageState extends State<SiteWeatherPage> {
  Map<String, dynamic>? _w;
  String _msg = '';
  int _hours = 3;
  StreamSubscription<Map<String, dynamic>>? _sub;

  @override
  void initState() {
    super.initState();
    _load();
    _sub = widget.api.events().listen((f) {
      if (f['kind'] == 'weather' && f['weather'] is Map && mounted) {
        setState(() => _w = Map<String, dynamic>.from(f['weather'] as Map));
      }
    }, onError: (Object _) {}, cancelOnError: false);
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final w = await widget.api.weather();
      if (mounted) setState(() => _w = w);
    } on SiteError catch (e) {
      if (mounted) {
        setState(() => _msg = e.status == 404
            ? tr('Weather is not enabled on this site', '这个站点没开天气')
            : '$e');
      }
    }
  }

  Future<void> _set(String c) async {
    String msg;
    try {
      final w = await widget.api.setWeather(c, hours: c == 'auto' ? null : _hours);
      if (mounted) setState(() => _w = w);
      msg = c == 'auto'
          ? tr('Back to automatic (online weather)', '回到自动（按联网查的）')
          : tr('Set to ${_names[c]}, back to automatic in $_hours h',
              '切成${_names[c]}，$_hours 小时后回到自动');
    } on SiteError catch (e) {
      msg = tr('Not changed: $e', '没切成：$e');
    }
    if (mounted) setState(() => _msg = msg);
  }


  @override
  Widget build(BuildContext context) {
    final w = _w;
    final a = w?['auto'];
    final nw = Nw.of(context);
    final tone = weatherTone('${w?['condition']}');
    return Scaffold(
      appBar: AppBar(title: Text(tr('Weather', '天气'))),
      body: NwListView(children: [
        if (w != null) ...[
          NwSectionLabel(tr('Now', '现在')),
          nwGap8,
          NwCard(
              tone: tone,
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Icon(weatherIcon('${w['condition']}'),
                    size: 24, color: tone == NwTone.neutral ? nw.textSecondary : nw.fg(tone)),
                const SizedBox(width: Nw.s16),
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  Text(weatherText(w), style: NwText.titleSmall.copyWith(color: nw.text)),
                  nwGap4,
                  Text(
                      a is Map && a['enabled'] == true
                          ? tr(
                              'Online: ${a['condition'] == null ? 'no result yet' : (_names[a['condition']] ?? a['condition'])}'
                                  '${'${a['error'] ?? ''}'.isEmpty ? '' : ' (last lookup failed: ${a['error']})'}',
                              '联网查的：${a['condition'] == null ? '还没查到' : (_names[a['condition']] ?? a['condition'])}'
                                  '${'${a['error'] ?? ''}'.isEmpty ? '' : '（上次查不到：${a['error']}）'}')
                          : tr('No site coordinates set, no online lookup', '没配庄园坐标，不联网查'),
                      style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
                ])),
              ])),
          nwGap24,
        ],
        NwSectionLabel(tr('Set manually', '手动切')),
        nwGap8,
        NwCard(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text(
              tr(
                  'Thunderstorm: scheduled patrols pause and robots return to standby; an intrusion '
                      'still sends a robot. Rain and thunderstorm: all robots limited to 0.3 m/s.',
                  '雷暴：排程巡检暂停、狗回待命点，有入侵照样派狗。下雨、雷暴：全狗限速 0.3 m/s。'),
              style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
          nwGap16,
          Wrap(spacing: Nw.s8, runSpacing: Nw.s4, crossAxisAlignment: WrapCrossAlignment.center, children: [
            Text(tr('Manual for:', '手动切多久：'), style: NwText.bodyMedium.copyWith(color: nw.text)),
            for (final h in weatherHours)
              ChoiceChip(
                  key: SiteWeatherPage.hoursKey(h),
                  label: Text(tr('$h h', '$h 小时')),
                  selected: _hours == h,
                  onSelected: (_) => setState(() => _hours = h)),
          ]),
          nwGap16,
          Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
            for (final c in const ['normal', 'rain', 'storm'])
              NwButton(
                  buttonKey: SiteWeatherPage.setKey(c),
                  variant: NwButtonVariant.secondary,
                  onPressed: () => _set(c),
                  icon: weatherIcon(c),
                  label: _names[c]!),
            NwButton(
                buttonKey: SiteWeatherPage.setKey('auto'),
                variant: NwButtonVariant.outline,
                onPressed: () => _set('auto'),
                label: tr('Back to automatic', '回到自动')),
          ]),
          if (_msg.isNotEmpty) ...[
            nwGap16,
            Text(_msg, key: SiteWeatherPage.msgKey, style: NwText.bodyMedium.copyWith(color: nw.text)),
          ],
        ])),
      ]),
    );
  }
}
