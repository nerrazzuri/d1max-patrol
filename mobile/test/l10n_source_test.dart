// 中英文的硬规矩（App V2）：`lib/` 里给人看的中文只许出现在 `tr(英文, 中文)` 的第二个参数里，
// 第一个参数（英文）里不许有中文、全角标点。漏一处，英文界面上就冒出一句中文。
//
// 做法是把每个 Dart 文件过一遍（认得注释、字符串、`${}` 插值），不是正则：字符串里有引号、插值里
// 又有字符串，正则数不清括号。
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// 故意留着的中文（文件 → 那一行里的一段）。每条都要说得出为什么。
const _allowed = <String, List<String>>{
  // 语言自己的名字：不管界面是什么语言都这么写
  'lib/ui/site_page.dart': ["Text('中文')", r"RegExp(r'^狗没标[:：]')"],
  'lib/ui/site_deter.dart': ["Text('中文')"],
  // 跟站点回的报错原文对：剥掉开头那几个字
  'lib/ui/site_standby.dart': [r"RegExp(r'^狗没标[:：]')"],
  'lib/ui/site_intercepts.dart': [r"RegExp(r'^狗没标[:：]')"],
  // 存到站点上的区域名字（数据，不是界面文字）；显示的时候另外翻（zoneLabelText）
  'lib/ui/site_mapping.dart': ["'禁行区' : '限速区'", "'禁行区' => 'No-go zone', '限速区' => 'Speed-limit zone'"],
  // 只有全角标点、没有字：报错原文的拼接
  'lib/ui/widget/live_video.dart': [r"'$error（$status）'", r"'$head：$detail'"],
};

bool _cjk(int c) =>
    (c >= 0x4e00 && c <= 0x9fff) || (c >= 0x3000 && c <= 0x303f) || (c >= 0xff00 && c <= 0xffef) ||
    c == 0x300c || c == 0x300d;

bool _ident(int c) =>
    (c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a) || c == 0x5f || c == 0x24;

class _Str {
  final int quote;
  final bool triple;
  final bool raw;
  const _Str(this.quote, this.triple, this.raw);
}

/// 回 `(行号, 原因)`：字符串里的中文不在 tr 的第二个参数里，或者 tr 的第一个参数里有中文。
List<(int, String)> scan(String src) {
  final bad = <(int, String)>[];
  final s = src.codeUnits;
  var line = 1;
  // 栈：字符串（_Str）或者插值里的代码（int = 进来时的花括号深度）
  final stack = <Object>[];
  var brace = 0;
  // tr( 的栈：每层记 (进来时的圆括号深度, 现在第几个参数)
  final trs = <List<int>>[];
  var paren = 0;
  var i = 0;
  void flag(String why) {
    if (bad.isEmpty || bad.last.$1 != line) bad.add((line, why));
  }

  while (i < s.length) {
    final c = s[i];
    if (c == 0x0a) line++;
    final top = stack.isEmpty ? null : stack.last;
    if (top is _Str) {
      if (!top.raw && c == 0x5c) {
        i += 2;
        continue;
      }
      if (!top.raw && c == 0x24 && i + 1 < s.length && s[i + 1] == 0x7b) {
        stack.add(brace);
        brace++;
        i += 2;
        continue;
      }
      if (c == top.quote) {
        if (!top.triple) {
          stack.removeLast();
        } else if (i + 2 < s.length && s[i + 1] == c && s[i + 2] == c) {
          stack.removeLast();
          i += 2;
        }
        i++;
        continue;
      }
      if (_cjk(c)) {
        if (trs.isEmpty) {
          flag('中文不在 tr() 里');
        } else if (trs.last[1] == 0) {
          flag('tr() 的第一个参数（英文）里有中文或全角标点');
        }
      }
      i++;
      continue;
    }
    // 代码
    if (c == 0x2f && i + 1 < s.length && s[i + 1] == 0x2f) {
      while (i < s.length && s[i] != 0x0a) {
        i++;
      }
      continue;
    }
    if (c == 0x2f && i + 1 < s.length && s[i + 1] == 0x2a) {
      i += 2;
      while (i + 1 < s.length && !(s[i] == 0x2a && s[i + 1] == 0x2f)) {
        if (s[i] == 0x0a) line++;
        i++;
      }
      i += 2;
      continue;
    }
    if (c == 0x27 || c == 0x22) {
      final raw = i > 0 && s[i - 1] == 0x72 && (i < 2 || !_ident(s[i - 2]));
      final triple = i + 2 < s.length && s[i + 1] == c && s[i + 2] == c;
      stack.add(_Str(c, triple, raw));
      i += triple ? 3 : 1;
      continue;
    }
    if (c == 0x7b) brace++;
    if (c == 0x7d) {
      brace--;
      if (top is int && brace == top) stack.removeLast();
    }
    if (c == 0x28) {
      // 前面正好是独立的标识符 tr
      if (i >= 2 && s[i - 1] == 0x72 && s[i - 2] == 0x74 && (i < 3 || (!_ident(s[i - 3]) && s[i - 3] != 0x2e))) {
        trs.add([paren, 0]);
      }
      paren++;
    }
    if (c == 0x29) {
      paren--;
      if (trs.isNotEmpty && trs.last[0] == paren) trs.removeLast();
    }
    // tr( 自己这一层的逗号才算换参数（里面别的调用的逗号，圆括号更深）
    if (c == 0x2c && trs.isNotEmpty && trs.last[0] == paren - 1) trs.last[1]++;
    i++;
  }
  return bad;
}

void main() {
  test('扫描器自己对不对：注释、插值、原始字符串、tr 的两个参数', () {
    expect(scan("final a = tr('Stop', '停'); // 注释里的中文不算\n/// 文档注释\n"), isEmpty);
    expect(scan("final a = '停';").single.$2, contains('不在 tr()'));
    expect(scan("final a = tr('停', '停');").single.$2, contains('第一个参数'));
    expect(scan("final a = tr('Stop（x）', '停');").single.$2, contains('第一个参数'), reason: '全角标点也算');
    expect(scan(r"final a = tr('n=${f('x', 1)} of $b', '第 ${f('x', 1)} 个，共 $b');"), isEmpty,
        reason: '插值里的逗号、引号不打乱参数的数法');
    expect(scan(r"final a = tr('A ${ok ? 'x' : 'y'}', '甲 ${ok ? '对' : '错'}');"), isEmpty);
    expect(scan(r"final a = tr('A', 'B') + '丙';").single.$1, 1);
    expect(scan("final a = tr(\n  'A',\n  '甲'\n      '乙');\nfinal b = '丙';").single.$1, 5);
    expect(scan("Text(str('停'))").single.$2, contains('不在 tr()'), reason: 'str( 不是 tr(');
    expect(scan("x.tr('停', '停')").length, 1, reason: '别人的 .tr 方法不算');
    expect(scan(r"final r = RegExp(r'^狗\$');").single.$2, contains('不在 tr()'));
    expect(scan("final a = '''多行\n中文''';").map((e) => e.$1), [1, 2], reason: '三引号的多行字符串，行号跟着走');
    expect(scan("/* 块注释\n中文 */ final a = 1;"), isEmpty);
  });

  test('lib/ 里给人看的中文都在 tr() 的第二个参数里；英文那一半里没有中文', () {
    final problems = <String>[];
    final files = Directory('lib')
        .listSync(recursive: true)
        .whereType<File>()
        .where((f) => f.path.endsWith('.dart') && !f.path.endsWith('.g.dart'))
        .toList()
      ..sort((a, b) => a.path.compareTo(b.path));
    expect(files.length, greaterThan(30));
    for (final f in files) {
      final path = f.path.replaceAll('\\', '/');
      final src = f.readAsStringSync();
      final lines = src.split('\n');
      final allow = _allowed[path] ?? const <String>[];
      final used = <String>{};
      for (final (n, why) in scan(src)) {
        final text = lines[n - 1];
        final hit = allow.where(text.contains);
        if (hit.isNotEmpty) {
          used.addAll(hit);
          continue;
        }
        problems.add('$path:$n $why: ${text.trim()}');
      }
      for (final a in allow) {
        if (!used.contains(a)) problems.add('$path: 白名单里的 $a 已经没有了，删掉这一条');
      }
    }
    expect(problems, isEmpty, reason: problems.join('\n'));
  });
}
