/// 上手向导（W53 §8，用户 2026-10-31：只买设备、没买交付服务的客户要能自己学会用）。
///
/// - **上手导览**：这个账号没看过当前版本（站点记在账号上，换手机不用再看），进了值守页就开始。在真页面上
///   挡一层半透明、圈出一块，旁边一两句，「下一步」往下走，随时「跳过」。按角色各一套。
/// - **报警练习**（保安、能处理报警的）：导览最后弹一条「演练」报警，照着点 去处理 → 我来处理 → 误报。
///   **只在 App 里模拟**：不发给站点、不进报警记录、别人看不到（设计 D11）。
/// - **常见问题**：按角色挑几条，每条三四句话。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p2_duty.dart';
import 'nw/nw_p4_admin.dart' show nwGap8, NwListView;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_duty_page.dart';

/// 导览的版本：内容大改了加一，大家再看一遍。
const int kTourVersion = 1;

const Key tourCardKey = Key('tour-card');
const Key tourNextKey = Key('tour-next');
const Key tourSkipKey = Key('tour-skip');

class TourStep {
  final GlobalKey? target;
  final String title;
  final String body;
  const TourStep(this.target, this.title, this.body);
}

/// 按角色的导览步骤（值守页上圈的那几块）。
List<TourStep> tourSteps(String role, DutyTargets t) {
  final mode = TourStep(
      t.mode,
      tr('Arming and weather', '布防和天气'),
      role == 'guard'
          ? tr('This line shows whether the site is armed, and the weather. You can switch to Armed; disarming is up to the manager.',
              '这一行是布防状态和天气。你能切到「布防」；撤防要经理定。')
          : tr('Whether the site is armed, and the weather. Tap Change to arm or disarm (disarming asks you to confirm).',
              '布防状态和天气。点「切换」布防、撤防（撤防要确认）。'));
  final robots = TourStep(t.chips, tr('Robots', '机器人'),
      tr('These are your robots. Tap a name to switch, or swipe left/right on the video.', '这一排是机器人。点名字切换，或者在画面上左右滑。'));
  final video = TourStep(t.video, tr('Live video', '实时画面'),
      tr('Tap ▶ to watch live at full speed; tap ✕ to stop (saves data on 4G).', '点 ▶ 看实时画面（全速），✕ 关掉（4G 省流量）。'));
  final status = TourStep(t.status, tr('What it is doing', '它在干嘛'),
      tr('What the robot is doing, when it will finish, and its battery.', '这里写着它在干嘛、预计几点走完、电量。'));
  final today = TourStep(t.today, tr("Today's patrols", '今天的巡逻'),
      tr('Every patrol today: scheduled ones and one-off ones. Tap one that didn\'t finish to see why.',
          '今天所有的巡逻：日程的和临时派的。没完成的点开看原因。'));
  return switch (role) {
    'guard' => [
        mode,
        robots,
        video,
        status,
        TourStep(t.actions, tr('Patrol, drive, stop', '派巡逻、遥控、叫停'),
            tr('Patrol: pick a route. If it runs into the schedule it waits until the schedule is done, and the manager is told. '
                'Drive: steer it by hand. Stop: it stops where it is, right away.',
                '派巡逻：选路线；撞上日程会排在日程后面，派了经理会知道。遥控：手动开着它走。叫停：机器人马上停在原地。')),
        today,
        TourStep(null, tr('When an alarm comes in', '有报警的时候'),
            tr('A P1 alarm rings, and a red bar appears at the top. Tap Respond. Let\'s practise once now.',
                '有 P1 报警会响铃，最上面出红条，点「去处理」。现在练一次。')),
      ],
    'owner' => [
        mode,
        robots,
        TourStep(t.actions, tr('Patrol and stop', '派巡逻、叫停'),
            tr('Send a one-off patrol, or stop a robot right away.', '临时派一趟巡逻，或者马上叫停。')),
        today,
        TourStep(null, tr('Schedule', '日程'),
            tr('The 5th tab at the bottom is the schedule: when each route runs, on which robot.', '底部第 5 栏是日程：几点走哪条路线、哪台机器人。')),
      ],
    'viewer' => [
        robots,
        video,
        today,
      ],
    _ => [
        mode,
        robots,
        video,
        today,
        TourStep(null, tr('Admin', '管理'),
            tr('The Admin tab starts with "Needs you" and the site setup checklist: work through it once. '
                'Robots → Add to add a robot; Map to confirm no-go zones; Admin → Accounts to approve people.',
                '「管理」栏最上面是「要你处理」和开站清单，照着做一遍。加机器人在「机器人 → 添加」，确认禁行区在「地图」，批准账号在「管理 → 账号」。')),
      ],
  };
}

bool _running = false;

/// 进站点后值守页建好了调：这个账号没看过当前版本就开始。
Future<void> maybeStartTour(BuildContext context, SiteApi api, DutyTargets t) async {
  if (_running) return;
  int seen;
  try {
    final me = await api.me();
    final g = me['guide'];
    seen = g is Map && g['tour'] is num ? (g['tour'] as num).toInt() : kTourVersion; // 老站点不带：不打扰
  } on SiteError {
    return;
  } on FormatException {
    return;
  }
  if (seen >= kTourVersion || !context.mounted) return;
  await runTour(context, api, t);
}

/// 外壳登记的「切回值守页、再走一遍导览」（「使用教程」从别的栏点进来：那一栏切走就拆了，导览要在外壳上跑）。
Future<void> Function()? replayTour;

/// 「使用教程」：回到值守页再走一遍（外壳没登记就只讲文字）。
Future<void> restartTour(BuildContext context, SiteApi api) async {
  final again = replayTour;
  if (again != null) return again();
  await runTour(context, api, DutyTargets());
}

Future<void> runTour(BuildContext context, SiteApi api, DutyTargets t) async {
  if (_running) return;
  _running = true;
  try {
    final role = api.session?.role ?? '';
    final steps = tourSteps(role, t);
    var finished = true;
    for (var i = 0; i < steps.length; i++) {
      if (!context.mounted) return;
      final s = steps[i];
      final ctx = s.target?.currentContext;
      if (ctx != null && ctx.mounted) {
        await Scrollable.ensureVisible(ctx, duration: const Duration(milliseconds: 200), alignment: 0.3);
      }
      if (!context.mounted) return;
      final go = await _showStep(context, s, i, steps.length);
      if (go != true) {
        finished = false;
        break;
      }
    }
    try {
      await api.setGuide(<String, dynamic>{'tour': kTourVersion});
    } on SiteError {
      // 记不上：下次再看一遍，不要紧
    }
    _running = false;
    // 练习页开着的时候导览已经算走完了（不挡下一次「使用教程」）
    if (finished && (api.session?.canHandleAlerts ?? false) && role != 'owner' && context.mounted) {
      unawaited(Navigator.push(context, MaterialPageRoute<void>(builder: (_) => const PracticeAlarmPage())));
    }
  } finally {
    _running = false;
  }
}

Rect? _rectOf(GlobalKey? k) {
  final ro = k?.currentContext?.findRenderObject();
  if (ro is! RenderBox || !ro.attached || !ro.hasSize) return null;
  final o = ro.localToGlobal(Offset.zero);
  return o & ro.size;
}

Future<bool?> _showStep(BuildContext context, TourStep s, int i, int n) => showGeneralDialog<bool>(
      context: context,
      barrierDismissible: false,
      barrierColor: Colors.transparent,
      pageBuilder: (c, _, _) {
        final rect = _rectOf(s.target);
        final size = MediaQuery.sizeOf(c);
        final below = rect == null || rect.center.dy < size.height / 2;
        final nw = Nw.of(c);
        final card = Material(
          key: tourCardKey,
          color: nw.surface,
          elevation: 6,
          borderRadius: BorderRadius.circular(Nw.r12),
          child: Padding(
            padding: const EdgeInsets.all(Nw.s16),
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              Text('${i + 1} / $n · ${s.title}',
                  style: NwText.titleSmall.copyWith(color: nw.text, fontWeight: FontWeight.w700)),
              const SizedBox(height: Nw.s8),
              Text(s.body, style: NwText.bodyMedium.copyWith(color: nw.text)),
              const SizedBox(height: Nw.s8),
              Row(children: [
                TextButton(
                    key: tourSkipKey, onPressed: () => Navigator.pop(c, false), child: Text(tr('Skip', '跳过'))),
                const Spacer(),
                NwButton(
                    buttonKey: tourNextKey,
                    label: i + 1 == n ? tr('Done', '好了') : tr('Next', '下一步'),
                    onPressed: () => Navigator.pop(c, true)),
              ]),
            ]),
          ),
        );
        return Stack(children: [
          Positioned.fill(child: SizedBox.expand(child: CustomPaint(size: size, painter: _Dim(rect, nw.primary)))),
          Positioned(
              left: Nw.s16,
              right: Nw.s16,
              top: below ? ((rect?.bottom ?? size.height / 3) + Nw.s8).clamp(0, size.height - 220) : null,
              bottom: below ? null : (size.height - rect.top + Nw.s8).clamp(0, size.height - 220),
              child: Center(child: ConstrainedBox(constraints: const BoxConstraints(maxWidth: 480), child: card))),
        ]);
      },
    );

class _Dim extends CustomPainter {
  final Rect? hole;
  final Color ring;
  _Dim(this.hole, this.ring);

  @override
  void paint(Canvas canvas, Size size) {
    final all = Offset.zero & size;
    final h = hole?.inflate(4);
    // 整屏压暗，再把要看的那一块挖空（单独一层里清掉）
    canvas.saveLayer(all, Paint());
    canvas.drawRect(all, Paint()..color = const Color(0x99000000));
    if (h != null) {
      canvas.drawRRect(RRect.fromRectAndRadius(h, const Radius.circular(Nw.r8)), Paint()..blendMode = BlendMode.clear);
    }
    canvas.restore();
    if (h != null) {
      canvas.drawRRect(RRect.fromRectAndRadius(h, const Radius.circular(Nw.r8)),
          Paint()
            ..color = ring
            ..style = PaintingStyle.stroke
            ..strokeWidth = 2);
    }
  }

  @override
  bool shouldRepaint(_Dim old) => old.hole != hole || old.ring != ring;
}

// ------------------------------------------------------------ 报警练习

/// 一条假的 P1 报警，照着点一遍。**什么都不发给站点。**
class PracticeAlarmPage extends StatefulWidget {
  const PracticeAlarmPage({super.key});

  static const Key respondKey = Key('practice-respond');
  static const Key takeKey = Key('practice-take');
  static Key outcomeKey(String o) => Key('practice-$o');
  static const Key doneKey = Key('practice-done');

  @override
  State<PracticeAlarmPage> createState() => _PracticeAlarmPageState();
}

class _PracticeAlarmPageState extends State<PracticeAlarmPage> {
  int _step = 0;

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final steps = <Widget>[
      // 0：值守页最上面的红条
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(tr('This is how an alarm looks at the top of the duty page:', '报警来了，值守页最上面是这样：'),
            style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
        nwGap8,
        NwCard(
          tone: NwTone.error,
          child: Row(children: [
            const NwLevelBox(level: 'P1'),
            const SizedBox(width: Nw.s8),
            Expanded(
                child: Text(tr('PRACTICE · A sees a person in the North area', '演练 · A 在北区看见人'),
                    style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700))),
            NwButton(
                buttonKey: PracticeAlarmPage.respondKey,
                size: NwButtonSize.small,
                variant: NwButtonVariant.destructive,
                label: tr('Respond', '去处理'),
                onPressed: () => setState(() => _step = 1)),
          ]),
        ),
        nwGap8,
        Text(tr('Tap Respond.', '点「去处理」。'), style: NwText.bodyMedium.copyWith(color: c.text)),
      ]),
      // 1：报警页
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        AspectRatio(
            aspectRatio: 16 / 9,
            child: Container(
                decoration: BoxDecoration(color: Colors.black87, borderRadius: BorderRadius.circular(Nw.r8)),
                alignment: Alignment.center,
                child: Text(tr('Snapshot from the robot (practice)', '机器人抓拍（演练）'),
                    style: NwText.bodySmall.copyWith(color: Colors.white70)))),
        nwGap8,
        Text(
            tr('The alarm page shows the snapshot, the live video, and where it happened on the map. '
                'Tap "I\'ll handle it" so everyone sees someone is on it.',
                '报警页有抓拍、实时画面、地图上的位置。点「我来处理」，别人就知道有人在管了。'),
            style: NwText.bodyMedium.copyWith(color: c.text)),
        nwGap8,
        NwButton(
            buttonKey: PracticeAlarmPage.takeKey,
            label: tr("I'll handle it", '我来处理'),
            onPressed: () => setState(() => _step = 2)),
      ]),
      // 2：写结论
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(
            tr('After you check (video, or go there), record what it was:', '看过画面、或者去现场看过之后，写下结论：'),
            style: NwText.bodyMedium.copyWith(color: c.text)),
        nwGap8,
        Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
          for (final (o, label) in [
            ('real', tr('Real person', '真有人')),
            ('false', tr('False alarm', '误报')),
            ('handled', tr('Handled', '处理好了')),
          ])
            NwButton(
                buttonKey: PracticeAlarmPage.outcomeKey(o),
                variant: NwButtonVariant.outline,
                label: label,
                onPressed: () => setState(() => _step = 3)),
        ]),
      ]),
      // 3：好了
      Column(key: PracticeAlarmPage.doneKey, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Icon(Icons.check_circle_rounded, size: 48, color: c.success),
        nwGap8,
        Text(tr("That's it. When a real one comes in, do the same.", '好了，真的来了就这么处理。'),
            textAlign: TextAlign.center, style: NwText.titleSmall.copyWith(color: c.text)),
        nwGap8,
        NwButton(label: tr('Back', '回去'), onPressed: () => Navigator.pop(context)),
      ]),
    ];
    return Scaffold(
      appBar: AppBar(title: Text(tr('Practice alarm', '报警练习'))),
      body: NwListView(children: [
        NwNotice(
            tone: NwTone.info,
            text: tr('Practice only: nothing here is sent to the site or recorded.', '只是练习：这里点的都不发给站点、不记录。')),
        const SizedBox(height: Nw.s16),
        steps[_step],
      ]),
    );
  }
}

// ------------------------------------------------------------ 常见问题

class _Faq {
  final Set<String>? roles; // null = 谁都看
  final String q;
  final String a;
  const _Faq(this.roles, this.q, this.a);
}

List<_Faq> _faqs() => [
      _Faq(null, tr('An alarm came in. What do I do?', '报警来了怎么办'),
          tr('Tap the red bar → look at the snapshot and live video → tap "I\'ll handle it". If someone is really there, '
              'go or call security; then record Real person / False alarm / Handled.',
              '点红条 → 看抓拍和实时画面 → 点「我来处理」。真有人就去现场或者叫人；处理完写结论：真有人 / 误报 / 处理好了。')),
      _Faq({'guard', 'owner', 'admin', 'engineer'}, tr('How do I stop a robot right now?', '怎么马上让它停'),
          tr('Duty page → Stop. It stops where it is and the site won\'t send it anywhere until someone taps Resume. '
              'The hardware emergency stop on the robot always works too.',
              '值守页 →「叫停」。它停在原地，之后有人点「恢复」站点才会再派它。机器人身上的急停按钮任何时候都能按。')),
      _Faq({'guard', 'owner', 'admin', 'engineer'}, tr("The robot won't go out. Why?", '机器人派不出去'),
          tr('The message says why and has a button to fix it. Most common: no-go zones not confirmed (administrator), '
              "the robot doesn't know where it is (set its position), it was stopped (Resume), it is offline or charging.",
              '弹出来的框里写着原因，带着去处理的按钮。最常见：禁行区还没确认（找管理员）、机器人不知道自己在哪（设位置）、被叫停了（恢复）、离线或者在充电。')),
      _Faq({'guard', 'owner'}, tr('My patrol was queued. Why?', '我派的巡逻怎么排队了'),
          tr('Scheduled patrols come first. If yours would run into one, it starts after the scheduled one finishes. '
              'You get a notification when it starts. To look right away, use Drive.',
              '日程优先。你派的跟日程撞了，就等日程那趟走完再走，开始时会通知你。要马上去看，用「遥控」。')),
      _Faq(null, tr('A robot is offline', '机器人掉线了'),
          tr('Check it is powered on and the WiFi is up. If it changed WiFi, an administrator sets it under Robot → Change WiFi.',
              '看一下它开着没有、WiFi 通不通。换过 WiFi 的，管理员在「机器人 → 换 WiFi」里重新配。')),
      _Faq(null, tr('How do I find a recording?', '怎么看录像'),
          tr('Robots → pick a robot → Recordings, or History → Patrols for the photos of each patrol.',
              '机器人 → 点一台 → 录像；每趟巡逻拍的照片在「记录 → 巡逻」。')),
      _Faq({'admin', 'engineer'}, tr('How do I add people?', '怎么加账号'),
          tr('People apply in the app (sign-in page → New Account Registration). You approve under Admin → Accounts and pick their role; '
              'new accounts are View only by default.',
              '让他们在 App 登录页点「新账号注册」；你在「管理 → 账号」里批准、选权限，默认「只看」。')),
      _Faq({'admin', 'engineer'}, tr('Where do I start?', '从哪开始'),
          tr('Admin → Site setup checklist. Work through it top to bottom; each line has a "Do it" button.',
              '「管理」页的开站清单，从上往下做，每一项都有「去做」。')),
    ];

class HelpPage extends StatelessWidget {
  final String role;
  const HelpPage({super.key, required this.role});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final list = [for (final f in _faqs()) if (f.roles == null || f.roles!.contains(role)) f];
    return Scaffold(
      appBar: AppBar(title: Text(tr('Common questions', '常见问题'))),
      body: NwListView(children: [
        for (var i = 0; i < list.length; i++)
          Padding(
            padding: const EdgeInsets.only(bottom: Nw.s8),
            child: NwCard(
              key: Key('faq-$i'),
              padding: EdgeInsets.zero,
              child: ExpansionTile(
                title: Text(list[i].q, style: NwText.titleSmall.copyWith(color: c.text)),
                childrenPadding: const EdgeInsets.fromLTRB(Nw.s16, 0, Nw.s16, Nw.s16),
                children: [Text(list[i].a, style: NwText.bodyMedium.copyWith(color: c.text))],
              ),
            ),
          ),
      ]),
    );
  }
}
