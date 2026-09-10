/// 盘况屏。**只读，不清盘。**
///
/// `POST /api/storage/sweep` 那条路的另一头是 `shutil.rmtree`，它的安全前提
/// 是「人先看名单，再点第二下」。在手机上、戴着手套、站在现场，那第二下点得
/// 太容易了。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock。**
/// 这一屏最硬的那条守备是「从头到尾没向狗发出过一个 POST」—— 那条只有在
/// 真的网络层上才数得出来。
///
/// **「有人把清盘做回来」这件事在这个文件里由好几条测试分头守着**，它们不是
/// 冗余，量的东西不在一个层面上：网络层数**真的发出去的请求**、源码层扫
/// **这个模块的文本**、还有一条是给源码层那把尺子做正对照的、widget 层认
/// **渲染出来的按钮**。各自抓什么、各自漏什么，写在「只读那道守备」那一段
/// 每条测试自己头上。**一条都不许删** —— 失效模式不相交。
///
/// （这里**不写总数**。序号是标签，加第五道守备只要给它写「第五层」；总数
/// 是记账，增删的时候没有任何一条测试会红。）
///
/// （历史留痕：这段原来写着「盯 `ButtonStyleButton.onPressed` 的那条将来谁
/// 把清盘做成 `IconButton` 它照样绿」。那是 `find.byType` 时代的话，早就不
/// 成立了：现在用的是 `find.bySubtype<ButtonStyleButton>()`，而 M3 的
/// `IconButton` 内部正是 `_IconButtonM3 extends ButtonStyleButton`，收得到。
///
/// **「收得到」这句不是从 Flutter 源码上抄来的口头承诺，仓内当场证着它**：
/// widget 树那条测试里的刷新按钮本身就是一个 `IconButton`（见
/// `storage_page.dart` 的 `AppBar.actions`），而那条测试有一句
/// `expect(refresh, isNotEmpty)` —— `refresh` 正是从
/// `find.bySubtype<ButtonStyleButton>()` 的匹配结果里按 key 捞出来的。哪天
/// Flutter 把 M3 的 `IconButton` 从 `ButtonStyleButton` 底下挪走，`refresh`
/// 会是空的，那条测试当场红，红在这句 `expect` 上。所以这个事实判断有仓内
/// 落锚，不是一条会随着上游版本悄悄过期的注释。）
///
/// **所有数字断言一律精确匹配。** `find.textContaining('72')` 收得下 `172`、
/// `720`、`0.72` —— 这个毛病这一卷已经抓过两次。
///
/// **报文一律从 `test/fixtures/storage.json` 改出来**，不手搓 JSON 字面量：
/// 那份夹具是 `tests/app/test_wire_fixtures.py` 从真的路由处理器生成的
/// （挂账 11）。手搓的那份跟狗那头分家的那天，两边都是绿的。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:d1max_patrol/ui/storage_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

/// 存储页那个模块的源码路径。
///
/// widget 测试的工作目录就是包根（`mobile/`）—— 上面那几条读
/// `test/fixtures/storage.json` 走的是同一个相对路径。
const String storagePagePath = 'lib/ui/storage_page.dart';

/// 一份源码里所有**写得出一个 POST** 的行（去掉首尾空白）。
///
/// **认的是文本，所以是一根便宜的绊线，不是探针。** 真正的探针要跑得起 Dart
/// 的分析器，这个仓里没有那个东西；而绊线的价值就一样：手滑的时候当场绊一下，
/// 比真机早。同一招在 `tests/app/test_operator.py::test_手机那头的路径跟狗这头
/// 是同一条` 里已经用过一次。
///
/// **`.post(` / `.put(` 是调用的形状，`POST` 是散文里的说法**，两样分开数：
/// 前者出现在哪儿都不行（注释里也不行 —— 注释里写得出来的下一步就是把 `//`
/// 删掉），后者只许出现在注释里。
List<String> postCalls(String source) => <String>[
      for (final String ln in source.split('\n'))
        if (ln.contains('.post(') || ln.contains('.put(')) ln.trim(),
    ];

/// 一份源码里所有提到 `POST` 这三个字母的行（去掉首尾空白）。
List<String> postMentions(String source) => <String>[
      for (final String ln in source.split('\n'))
        if (ln.contains('POST')) ln.trim(),
    ];

/// 这一行是不是注释。
bool isComment(String trimmed) => trimmed.startsWith('//');

/// 一次挂好的现场：一只假狗、一个解过锁的客户端。
class Rig {
  Rig(this.dog, this.client);

  final FakeDog dog;
  final PatrolClient client;

  /// 这一屏问了几次盘况。**refresh 真的又问了一遍**靠它钉住。
  int get asks => dog.received
      .where((FakeCall r) => r.method == 'GET' && r.path == '/api/storage')
      .length;

  /// 这一屏往狗上发了几个 POST。**必须一直是 0。**
  ///
  /// 解锁那两条在 [mount] 里已经从账上抹掉了，所以这个数只算这一屏自己的。
  int get posts =>
      dog.received.where((FakeCall r) => r.method == 'POST').length;
}

/// 照着 `test/fixtures/storage.json` 改一份盘况报文出来。
///
/// **夹具里 `forecast.runs` 是空数组**（生成它的那台狗上一趟归档也没有），
/// 所以「要过期的那几趟列得出来」只能自己造 `runs` 喂进去。
Map<String, dynamic> storageWire({
  bool? noticeWritten,
  String? noticeDetail,
  String? backupLevel,
  String? backupDetail,
  String? forecastDetail,
  int? bytesAtRisk,
  List<Map<String, dynamic>>? runs,
}) {
  final Map<String, dynamic> m =
      jsonDecode(File('test/fixtures/storage.json').readAsStringSync())
          as Map<String, dynamic>;
  final Map<String, dynamic> fc =
      Map<String, dynamic>.from(m['forecast'] as Map<String, dynamic>);
  final Map<String, dynamic> bk =
      Map<String, dynamic>.from(m['backup'] as Map<String, dynamic>);
  if (forecastDetail != null) fc['detail'] = forecastDetail;
  if (bytesAtRisk != null) fc['bytes_at_risk'] = bytesAtRisk;
  if (runs != null) fc['runs'] = runs;
  if (backupLevel != null) bk['level'] = backupLevel;
  if (backupDetail != null) bk['detail'] = backupDetail;
  return <String, dynamic>{
    ...m,
    'notice_written': ?noticeWritten,
    'notice_detail': ?noticeDetail,
    'forecast': fc,
    'backup': bk,
  };
}

/// 一趟归档在 wire 上的样子（`engine/retention.py` 的 `RunInfo.to_wire()`）。
///
/// **`started_at` 是生戳**（`%Y%m%dT%H%M%SZ`），不是 `2026-08-30`。
Map<String, dynamic> runWire({
  String path = '/data/runs/20260830T041500Z',
  String mission = '巡检A',
  String startedAt = '20260830T041500Z',
  int retentionDays = 30,
  int sizeBytes = 3000000000,
}) =>
    <String, dynamic>{
      'path': path,
      'mission': mission,
      'started_at': startedAt,
      'retention_days': retentionDays,
      'size_bytes': sizeBytes,
      'settled': true,
      'uploaded': false,
      'exported': false,
    };

/// 起一只假狗、解一次锁、把盘况屏挂上去。
///
/// `unlock` 是两步（`GET /api/auth/challenge` 拿 nonce，再 `POST /api/auth`
/// 交证明），两条路径都得先备好回复。而且它是真的网络 I/O：假时钟上推不动，
/// 得进 `runAsync`。
Future<Rig> mount(WidgetTester t,
    {Map<String, dynamic>? storage, int? status}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': '张三',
    'operator_verified': false,
    'readonly': false,
  };
  dog.replies['/api/storage'] = storage ?? storageWire();
  if (status != null) dog.statusCodes['/api/storage'] = status;
  final PatrolClient c = PatrolClient(dog.baseUrl);
  await t.runAsync(() => c.unlock('864209', operator: '张三'));
  // **把解锁那两条从账上抹掉。** `POST /api/auth` 是解锁发的，不是这一屏发的；
  // 不抹的话「这一屏一个 POST 都没有」那条永远数出 1，只好放宽成「不超过 1」，
  // 而放宽之后它就再也拦不住真的第一个清盘请求。
  dog.received.clear();
  await t.pumpWidget(MaterialApp(
      home: StoragePage(
          client: c, robot: const Robot(sn: 'C40221', name: '三号'))));
  await pumpUntil(
      t,
      () =>
          find.byKey(StoragePage.capacityKey).evaluate().isNotEmpty ||
          find.byKey(StoragePage.errorKey).evaluate().isNotEmpty,
      '盘况问回来（或者报出读不到）',
      step: const Duration(milliseconds: 20));
  return Rig(dog, c);
}

/// 收摊。**客户端也要关**：一条没收的 `HttpClient` 会留着自己那个 15 秒空闲
/// 计时器，`flutter_test` 的 `!timersPending` 会当场把测试打红。
Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, null);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
}

/// 挂一次、把备份盘那句话的颜色**连同整屏的配色**一起读出来、收摊。
///
/// 每一档都得是自己挂一次的结果 —— 拿被测代码之外另算一份「应该是什么颜色」
/// 来比，比的是那份副本，不是屏上真画出来的东西。
///
/// [scheme] 一起带出来，是为了让「写死的期望色到底还是不是主题里那个中性色」
/// 这件事**在测试里自己说得清**：Flutter 升级换了 M3 的默认调色板时，红的会是
/// 那条自洽断言，而不是三条看不出所以然的颜色断言。
typedef BackupInk = ({Color? color, ColorScheme scheme});

Future<BackupInk> backupInkFor(WidgetTester t, String level) async {
  final Rig rig = await mount(t,
      storage: storageWire(backupLevel: level, backupDetail: '这一档的原话'));
  final BackupInk ink = (
    color: t.widget<Text>(find.byKey(StoragePage.backupKey)).style?.color,
    scheme: Theme.of(t.element(find.byKey(StoragePage.backupKey))).colorScheme,
  );
  await unmount(t, rig);
  return ink;
}

/// 只要颜色的那几条用这个。
Future<Color?> backupColorFor(WidgetTester t, String level) async =>
    (await backupInkFor(t, level)).color;

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides：这个文件
  // 要证的正是「这一屏只发 GET、一个 POST 都没发」。
  useRealHttp();

  // ------------------------------------------------------------ 容量

  testWidgets('盘用了多少要看得见,数字精确对得上', (WidgetTester t) async {
    // 夹具是 420000000000 / 1000000000000，ratio 0.42。
    // **精确匹配**：`textContaining('42')` 收得下 `142`、`420`、`0.42`。
    final Rig rig = await mount(t);
    expect(find.text('已用 42%（420.0 GB / 1000.0 GB）'), findsOneWidget);
    await unmount(t, rig);
  });

  // ------------------------------------------------- 预告落没落盘（两个局面）

  testWidgets('预告没落盘要顶到脸上,而且顶在最上面', (WidgetTester t) async {
    // **这一条是这块屏存在的理由。** `notice_written == false` 的意思是盘满了
    // 或者被挂成只读 —— 「再过几天开始删除」那句话说了不算，钟没开始走。
    // 不显示的话，人看到的是一块一切正常的屏，而归档会无声地堆到盘炸。
    final Rig rig = await mount(t,
        storage: storageWire(
            noticeWritten: false, noticeDetail: '预告没能记到盘上(只读文件系统)'));
    expect(find.text('预告没能记到盘上(只读文件系统)'), findsOneWidget);
    // 画出来还不够 —— 排在容量那一行下面的话，它就是一条要往下滚才看得见的
    // 提示，而这块屏上唯一「必须现在就看见」的就是它。
    expect(t.getTopLeft(find.byKey(StoragePage.noticeKey)).dy,
        lessThan(t.getTopLeft(find.byKey(StoragePage.capacityKey)).dy),
        reason: '预告没落盘那块卡片要顶在容量上面');
    await unmount(t, rig);
  });

  testWidgets('预告落了盘就不挂那块红卡片', (WidgetTester t) async {
    // 另一个局面。两个局面都得跑到 —— 全都跑在 `notice_written == false` 上
    // 的话，「落了盘就别吓人」这一支一次都没执行过。
    final Rig rig = await mount(t);
    expect(find.byKey(StoragePage.capacityKey), findsOneWidget,
        reason: '屏真的画出来了，下面那条 findsNothing 才不是空绿');
    expect(find.byKey(StoragePage.noticeKey), findsNothing);
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 备份盘那三档

  testWidgets('备份盘 ok 那一档的原话上屏', (WidgetTester t) async {
    final Rig rig = await mount(t,
        storage: storageWire(
            backupLevel: backupLevelOk, backupDetail: '镜像盘跟得上,落后 0 趟'));
    expect(find.text('镜像盘跟得上,落后 0 趟'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('备份盘 neutral 那一档的原话上屏', (WidgetTester t) async {
    // 夹具里就是这一档（`"level": "neutral"`）—— 没配备份盘是出厂常态。
    final Rig rig = await mount(t);
    expect(find.text('未配备份盘。归档现在只有一份,存在这台狗自己的盘上'),
        findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('备份盘 push 那一档的原话上屏', (WidgetTester t) async {
    final Rig rig = await mount(t,
        storage: storageWire(
            backupLevel: backupLevelPush, backupDetail: '镜像盘落后 12 趟'));
    expect(find.text('镜像盘落后 12 趟'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('neutral 不许用警示色:ok 和 neutral 都得等于那个写死的中性色',
      (WidgetTester t) async {
    // `engine/backup.py` 的 `backup_notice` 写着：没配镜像盘不许常年报红
    // （spec §7.6）—— 常年报警的东西等于没报警，现场的人会先学会忽略它，
    // 然后连真的那次也一起忽略。`NEUTRAL` 单独立一档就是为了这件事。
    // 手机端把它画成黄的或红的，每一台没插镜像盘的狗从此常年顶着警示色。
    //
    // **这里断的是绝对值，不是「跟 push 不一样」。** 相对断言拦不住整体平移：
    // 把 ok 和 neutral **一起**画成告警黄、push 仍留着红，三者两两照样不等，
    // 相对断言全绿 —— 而屏上每一台没插镜像盘的狗从此常年顶着一块黄。
    // 这一刀真下过：`ok`+`neutral` 一起改成 `0xFFFFA000`，182 条全绿。
    const Color neutralInk = Color(0xFF49454F); // M3 浅色主题的 onSurfaceVariant
    final BackupInk ok = await backupInkFor(t, backupLevelOk);
    final BackupInk neutral = await backupInkFor(t, backupLevelNeutral);
    final BackupInk push = await backupInkFor(t, backupLevelPush);

    // **自洽**：写死的那个值确实还是主题里的中性色。这一条红了说明 Flutter
    // 换了 M3 的默认调色板 —— 那就先确认新值仍是中性的（不是黄、不是红），
    // 再把上面那个常量改过来。**不许反过来把下面几条改松。**
    expect(neutral.scheme.onSurfaceVariant, neutralInk,
        reason: '写死的期望色还是主题里那个 onSurfaceVariant');

    expect(ok.color, neutralInk, reason: 'ok 那一档就是中性色，不是绿的也不是黄的');
    expect(neutral.color, neutralInk,
        reason: 'neutral 必须是这个中性色（spec §7.6）：没配镜像盘不许常年报警');
    expect(push.color, neutral.scheme.error,
        reason: '真该喊的那一档才用警示色 —— 否则「不报警」是靠整屏都不报警做到的');

    // 狗那侧的契约：`NoticeLevel` 只有 ok/neutral/push 三档，没有 warn。
    // **对着夹具断**（`test/fixtures/storage.json` 是 `tests/app/
    // test_wire_fixtures.py` 从真的路由处理器生成的），不是拿这边三个本地常量
    // 自己断自己 —— 那样只有人把某个常量改名成 'warn' 时才会红，跟狗那头发
    // 什么毫无关系：当注释读没问题，当断言读是零。
    final String wireLevel = ((jsonDecode(
                File('test/fixtures/storage.json').readAsStringSync())
            as Map<String, dynamic>)['backup'] as Map<String, dynamic>)['level']
        as String;
    expect(<String>[backupLevelOk, backupLevelNeutral, backupLevelPush],
        contains(wireLevel),
        reason: '狗真发出来的那个档，手机这头认得出来；狗那头加了第四档，这一条先红');
  });

  testWidgets('认不出的档不许崩,按中性处理', (WidgetTester t) async {
    // 狗以后加了新档，手机这头不能白屏，更不能凭一个读不懂的字符串把屏染红。
    final Color? unknown = await backupColorFor(t, 'huh');
    final Color? neutral = await backupColorFor(t, backupLevelNeutral);
    expect(unknown, isNotNull);
    expect(unknown, neutral);
  });

  // ------------------------------------------------------- 预告那一段

  testWidgets('预告那句话原样上屏,bytes_at_risk 为 0 时不挂那一行',
      (WidgetTester t) async {
    final Rig rig = await mount(t);
    // 夹具里那句原话。**手机不重新组织** —— 重新组织的版本迟早跟狗分家。
    expect(find.text('盘 42%,7 天内没有归档到期。'), findsOneWidget);
    expect(find.byKey(StoragePage.atRiskKey), findsNothing,
        reason: '一个字节都没到风险里，不该凭空写一句「其中 0.0 GB 即将到期」');
    await unmount(t, rig);
  });

  testWidgets('有东西要到期时,其中多少即将到期写出来', (WidgetTester t) async {
    // 另一个局面（夹具里 `bytes_at_risk` 是 0，这一支夹具驱动不到）。
    final Rig rig = await mount(t,
        storage: storageWire(
            forecastDetail: '再过 3 天开始删最早的 2 趟。',
            bytesAtRisk: 12000000000));
    expect(find.text('再过 3 天开始删最早的 2 趟。'), findsOneWidget);
    expect(find.text('其中 12.0 GB 即将到期'), findsOneWidget);
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 名单那几行

  testWidgets('要过期的那几趟列得出来,生戳不许原样上屏', (WidgetTester t) async {
    // 狗那头写的是 `engine/archive.py` 的 `STAMP_FMT`：`20260830T041500Z`。
    // 那是给目录名排序用的，不是给人读的。
    final Rig rig = await mount(t,
        storage: storageWire(runs: <Map<String, dynamic>>[
          runWire(),
          runWire(
              path: '/data/runs/20260901T230000Z',
              mission: '巡检B',
              startedAt: '20260901T230000Z'),
        ]));
    expect(find.text('2026-08-30 04:15 UTC 巡检A'), findsOneWidget);
    expect(find.text('2026-09-01 23:00 UTC 巡检B'), findsOneWidget);
    expect(find.textContaining('20260830T041500Z'), findsNothing,
        reason: '生戳不是给人读的');
    await unmount(t, rig);
  });

  testWidgets('行右边只写保留期,不许冒充「还剩几天」', (WidgetTester t) async {
    // wire 上压根没有「还剩几天」（`RunInfo.days_left()` 没进 `to_wire()`）。
    // 在一份标着「预告名单」的表里写「保留 30 天」，百分之百会被读成
    // 「还剩 30 天」—— 而真正动手的时刻是 `delete_starts_at()` 算的
    // `max(到期日, 首次预告 + 预告期)`，能差出十几天。
    final Rig rig = await mount(t,
        storage: storageWire(
            forecastDetail: '盘 42%,7 天内没有归档到期。',
            runs: <Map<String, dynamic>>[runWire(retentionDays: 30)]));
    expect(find.text('保留期 30 天'), findsOneWidget,
        reason: '这一行真的画出来了，下面那两条 findsNothing 才不是空绿');
    expect(find.textContaining('还剩'), findsNothing);
    expect(find.textContaining('即将删除'), findsNothing);
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 只读那道守备

  testWidgets('这一屏从头到尾没向狗发过 POST,点过刷新也没有', (WidgetTester t) async {
    // **第一层：网络层。量的是真的发出去了几个请求。**
    //
    // 抓得住：**任何真的打到狗身上的 POST**，不管它写在哪个模块里 —— 哪怕
    // `client` 被递进另一个 widget、由那边去发，这里照样数得到。这几层里
    // 只有这一层不受「写在哪个文件」限制。
    //
    // 漏得掉：**这次挂载走不到的分支。** 清盘入口真加进来的时候多半挂在一条
    // 今天没走到的支上（`if (v.usedRatio > 0.9)` 之类），那时这里还是 0。
    // 那一半交给下面扫源码那条。
    final Rig rig = await mount(t);
    expect(rig.asks, 1, reason: '这一屏真的问过狗 —— 否则下面数 POST 数的是一片空白');
    await t.tap(find.byKey(StoragePage.refreshKey));
    await pumpUntil(t, () => rig.asks >= 2, '刷新真的又问了一次',
        step: const Duration(milliseconds: 20));
    expect(rig.asks, 2, reason: '刷新是一次性的，不是起了个轮询');
    expect(rig.posts, 0,
        reason: '这一卷只读不删。清盘那条路的另一头是 shutil.rmtree');
    await unmount(t, rig);
  });

  // ------------------------------------------------ 挂账 66：扫源码那一层
  //
  // **第二层：源码文本层。量的是这个模块的源文件里写着什么。**
  //
  // 原文判的是「记账即可」，因为第一道防线是网络层那条计数（上面那条）。
  // **但那条只证得了「跑过的那几条路上没发 POST」。** 清盘入口真加进来的时候，
  // 它多半挂在一条今天的测试压根走不到的支上（`if (v.usedRatio > 0.9)` 之类），
  // 于是网络层那条照样是 0，照样绿。
  //
  // 抓得住：**这个模块的源码里写下来的 POST**，走不走得到那条支它一概不管 ——
  // 连注释里第四次提起 POST 都红。
  //
  // 漏得掉：两样。一是 `client` 被递进另一个模块、由那个模块去发 POST（源码
  // 在别人家里），那条得靠上面网络层那条计数；二是**不长成 `.post(` 这个形
  // 状**的写法（`client.request('POST', ...)` 之类），尺子的量程就在这儿。
  //
  // 紧跟着的「那把扫源码的尺子真的量得出东西」是**第三层，但它守的不是这一
  // 屏，是这一层的尺子本身**：尺子坏了（读错文件、`contains` 写反、循环在空
  // 表上转）的话这一层会假绿，而假绿跟源码真干净在报告上长得一模一样。它抓
  // 的是「尺子失灵」，抓不到源码里的任何东西 —— 所以它不能替这一层，这一层
  // 也不能替它。

  test('存储页那个模块的源码里发不出一个 POST', () {
    final String source = File(storagePagePath).readAsStringSync();
    // **自洽。** 路径写错、文件搬了家的话，`readAsStringSync` 会抛；但源码被
    // 换成一份不相干的东西（或者哪天这一屏改名了）不会抛 —— 那时候下面两条
    // 是在一份跟存储页无关的文本上转，绿得毫无意义。
    expect(source, contains('class StoragePage'),
        reason: '读到的不是存储页那个模块：下面两条扫的是别人的源码');

    expect(postCalls(source), isEmpty,
        reason: '清盘那条路的另一头是 shutil.rmtree。'
            '注释里也不许出现调用的形状 —— 注释里写得出来的下一步就是把 // 删掉');

    final List<String> mentions = postMentions(source);
    for (final String ln in mentions) {
      expect(isComment(ln), isTrue,
          reason: 'POST 进了代码行，不再只是散文里的一句说明：$ln');
    }
    // **数目也钉住。** 只要「都是注释行」的话，往里加一句 `// POST` 是绿的 ——
    // 而那正好是「有人开始在这个模块里琢磨 POST」的第一个动作。
    //
    // 这三行是**文件头那两句 + 指路那句的 docstring**，都在说「这一屏为什么
    // 不发 POST」。白名单按数目记在测试里，不写进源码注释：写进源码的话，
    // 改源码的人顺手把注释一起改了，这根绊线自己就松了。
    expect(mentions.length, 3,
        reason: '$storagePagePath 里提到 POST 的行从 3 行变成了 ${mentions.length} 行。'
            '真是新写的一句说明，就把这个数一起改掉，并且想清楚为什么要在'
            '一个只读的模块里第四次提起 POST');
  });

  test('那把扫源码的尺子真的量得出东西', () {
    // **正对照。** 上面那条要是因为尺子坏了（读错文件、`contains` 写反、
    // 循环在空表上转）而绿，红是永远不会来的 —— 而「扫描根本没生效」跟
    // 「源码真的干净」在报告上长得一模一样。
    //
    // 喂给它的是**同一把尺子**，不是另写一个长得像的：另写的那个明天就跟
    // 上面那条分家了，分家那天两边都是绿的。
    const String dirty = '''
class StoragePage {
  Future<void> sweep() async {
    await widget.client.post('/api/storage/sweep');
  }
}
''';
    expect(postCalls(dirty), hasLength(1),
        reason: '一句明明白白的 POST 调用都量不出来的话，上面那条是空绿的');
    expect(postCalls(dirty).single, contains('.post('));

    const String commented = '// POST 这一句是有人开始琢磨清盘的第一个动作';
    expect(postMentions(commented), hasLength(1),
        reason: '一句 // POST 都量不出来的话，上面那条数目断言拦不住任何人');
    expect(postCalls(commented), isEmpty,
        reason: '散文里的 POST 不是调用的形状，两把尺子不许混成一把');
  });

  testWidgets('这块屏上除了刷新,没有第二个按得动的按钮', (WidgetTester t) async {
    // **第四层：widget 树层。量的是这一屏真正渲染出来的按钮。**
    //
    // 抓得住：**一个按得动的新按钮，哪怕它一行 `.post(` 都不写** —— 跳去另一
    // 个页面、调本地的东西、或者把请求藏进别的模块，源码层那条尺子量不到的
    // 形状，这里按「屏上有没有一个按得动的东西」一概收。
    //
    // 漏得掉：**不是 `ButtonStyleButton` 子类的入口** —— `InkWell` /
    // `GestureDetector` / 长按 / 弹出菜单项都不在这把尺子的量程里；以及这次
    // 挂载没渲染出来的分支（跟第一层同一个盲区）。
    //
    // 真加了别的按钮，这条会红，那时再决定它该不该在。
    //
    // **不许用 `find.byType(ButtonStyleButton)`**：`byType` 是精确 runtimeType
    // 匹配，而 `ButtonStyleButton` 是抽象基类 —— 屏上真正的控件是
    // `_IconButtonM3` / `ElevatedButton` 这些子类，`byType` 一个都收不到。
    // 这一圈曾经就是在空列表上转：往屏上插一个真按得动的
    // `ElevatedButton('清盘')`，182 条照样全绿。
    final Rig rig = await mount(t);
    final Finder buttons = find.bySubtype<ButtonStyleButton>();
    // **自洽断言。** 匹配到 0 个的话，下面那一圈什么也没查 —— 这一卷栽在
    // 「测试没跑到那条路径所以恒真」上已经不止一次了。
    expect(buttons, findsWidgets,
        reason: '这个 finder 真的匹配到了按钮 —— 匹配到 0 个的话下面这一圈是在空列表上转');

    // 右上角那个刷新在 M3 下内部就是一个 `ButtonStyleButton`，而且它**是按得
    // 动的** —— 它只发 GET，是这一屏唯一的例外。按 key 把它认出来，剩下的
    // 一个都不许按得动。
    final Set<Element> refresh = find
        .descendant(of: find.byKey(StoragePage.refreshKey), matching: buttons)
        .evaluate()
        .toSet();
    expect(refresh, isNotEmpty,
        reason: '刷新那个按钮真的落在匹配结果里 —— 例外名单不是凭空写的');

    for (final Element e in buttons.evaluate()) {
      if (refresh.contains(e)) continue;
      expect((e.widget as ButtonStyleButton).onPressed, isNull,
          reason: '这一卷只读不删。清盘那条路的另一头是 rmtree');
    }
    await unmount(t, rig);
  });

  testWidgets('挡了清盘就得指路,而且指路那句里不许有地址端口密码',
      (WidgetTester t) async {
    // 只挡不指路的话，人看完这块屏只是更急：他知道盘要满了，不知道下一步该
    // 走哪。**但指路那句里不许出现端口号、IP、密码** —— 它会跟着截图和工单
    // 一路流到客户手上。
    final Rig rig = await mount(t);
    expect(find.byKey(StoragePage.sweepHintKey), findsOneWidget);
    expect(find.text(sweepElsewhereHint), findsOneWidget);
    expect(sweepElsewhereHint, contains('网页端'), reason: '指的路要指得出去哪');
    for (final String leak in <String>[
      '8095',
      '192.168',
      'http',
      'PIN',
      'sweep',
      '/api/',
    ]) {
      expect(sweepElsewhereHint, isNot(contains(leak)),
          reason: '这句话会跟着截图外流：$leak 不许出现在里面');
    }
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 狗不回话

  testWidgets('狗不回话时说人话,异常原文不上屏', (WidgetTester t) async {
    final Rig rig = await mount(t, status: 500);
    expect(find.text(storageLoadFailed), findsOneWidget);
    expect(find.textContaining('读不到盘况'), findsOneWidget);
    // `PatrolError` 那份原文是「狗回了 500」。它既不告诉人下一步该干什么，
    // 又会把内部细节推到客户面前。
    expect(find.textContaining('500'), findsNothing);
    expect(find.byKey(StoragePage.refreshKey), findsOneWidget,
        reason: '出错这一屏也得给得出再试一次的路');
    await unmount(t, rig);
  });

  testWidgets('读不到之后点刷新,狗回话了就画出来', (WidgetTester t) async {
    // 出错屏上那个刷新真的能把人救回来 —— 不然它只是个装饰。
    final Rig rig = await mount(t, status: 500);
    expect(find.byKey(StoragePage.errorKey), findsOneWidget);
    rig.dog.statusCodes.remove('/api/storage');
    await t.tap(find.byKey(StoragePage.refreshKey));
    await pumpUntil(
        t,
        () => find.byKey(StoragePage.capacityKey).evaluate().isNotEmpty,
        '再问一次之后盘况画出来了',
        step: const Duration(milliseconds: 20));
    expect(find.byKey(StoragePage.errorKey), findsNothing);
    await unmount(t, rig);
  });
}
