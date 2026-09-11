/// 跟狗说话。**PIN 从不上网。**
///
/// 狗那头是质询-应答（`src/d1max_patrol/app/auth.py`）：先取一个一次性
/// nonce，再交 `HMAC-SHA256(PIN, nonce)` 的十六进制。`auth.py` 开头写得很
/// 直白：热点上报文人人可解。
///
/// **不许走 `{"pin": ...}` 那条老路。** 它也换得到 token，但在狗的热点上换到
/// 的是**只读**凭证 —— 遥控会全部 403，而报错指向权限，不指向「你把 PIN 明文
/// 发出去了」。
///
/// 用 `dart:io` 的 `HttpClient` 而不是 `package:http`：这个 app 只做 Android，
/// 少一个依赖少一次升级。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

import '../model/alert.dart';

/// 报名字那条接口的路径（挂账 90）。
///
/// **这个字面量在手机这一侧只许出现一次。** 它以前直接写在 [PatrolClient
/// .setOperator] 的调用里，而狗那头 `server.py` 里又写了两遍 —— 三份互相自洽
/// 的手抄件，改一处另外两处不会红。狗那侧改了路径，两侧的测试**各自全绿**，
/// 现场表现是「换人到不了狗，留痕永远建不起来」。
///
/// 狗那头对应的是 `app/auth.py` 的 `OPERATOR_PATH`。**两个常量还是两份手抄
/// 件**，只是从三份收成两份；真正把这条缝钉死的是狗那侧的
/// `tests/app/test_operator.py::test_手机那头的路径跟狗这头是同一条` ——
/// 它读这一行，跟 `OPERATOR_PATH` 比。所以**这一行的形状不要改**（单引号、
/// 一行写完），改了那条测试认不出来。
///
/// **`GET` 这条路手机不走。** 狗那头那份是服务器级的「上一个报过名字的
/// 人」，不是「现在是谁在开这只狗」（挂账 93）—— 后者读 `GET /api/control`
/// 的 `holder.operator`。
const String operatorPath = '/api/operator';

/// 这一轮质询的证明：`HMAC-SHA256(PIN, nonce)` 的十六进制。
///
/// 跟 `auth.proof_for` 逐字对应。**跨语言的向量在
/// `test/fixtures/proof_vectors.json` 里**，那份文件是狗那头生成的。
String proofFor(String pin, String nonce) =>
    Hmac(sha256, utf8.encode(pin)).convert(utf8.encode(nonce)).toString();

/// 跟狗说话时出的任何岔子。
///
/// **连不上、超时、狗回了 4xx —— 全归一到这一个类型。** 让每一处调用点各自
/// 去认 `SocketException` / `TimeoutException`，早晚漏一处，而漏的那一处在
/// 手机上是一个红屏。
class PatrolError implements Exception {
  /// HTTP 状态码。压根没连上是 0。
  final int status;
  final String error;
  final String detail;

  const PatrolError(this.status, this.error, [this.detail = '']);

  @override
  String toString() => detail.isEmpty ? error : '$error：$detail';
}

/// 换到的那次会话。
class Session {
  final String token;
  final String operator;

  /// **狗记下名字但不核实**（§6.3）。界面上不许说成「已登录：张三」。
  final bool operatorVerified;
  final bool readonly;

  /// 狗跟着这次应答发过来的那句话（`auth.OPERATOR_NOTICE`）。**原样留着。**
  ///
  /// [operatorVerified] 只说得出「核没核」，说不出「为什么不核、要可信的
  /// 身份该怎么办」—— 那句话在狗那头，措辞也归狗那头。这里硬编一句的话，
  /// 狗改了措辞手机不跟。
  final String notice;

  const Session({
    required this.token,
    required this.operator,
    required this.operatorVerified,
    required this.readonly,
    this.notice = '',
  });
}

class PatrolClient {
  final String baseUrl;
  final HttpClient _io;

  /// 只有自己造的 `HttpClient` 才是 `true`。调用方传进来的那个不归我们
  /// 所有——不许改它的超时，`close()` 也不许替调用方把它强关掉（N-1/N-2）。
  final bool _ownsIo;

  /// 罩住「发请求到读完整个响应体」这一整段的超时（B-2）。
  ///
  /// **不是只罩住等响应头。** `dart:io` 的 `HttpClientRequest.close()` 在
  /// 响应头一到就完成——狗发完头就沉默时，读 body 那一步没有别的东西兜底，
  /// 会挂到天荒地老，界面上连「狗没回话」都弹不出来。默认 10 秒；测试可以
  /// 注入一个短值，不必真等 10 秒去证明超时生效。
  final Duration timeout;

  String? _token;

  /// 自动重新解锁要用的那份凭证。**只喂给 [proofFor],绝不进任何请求体、
  /// 任何日志、任何异常信息（S-3）。**
  ///
  /// 记住它是为了在 token 闲置死掉之后自己换一张（见 [_send] 那一支 401）。
  /// 重新解锁走的仍然是现成的质询-应答，PIN 本身照旧不出这个类。
  String? _pin;

  /// 重新解锁时报的名字。**记的是狗回来的那个规范名**（`Session.operator`），
  /// 不是调用方递进来的原样字符串 —— 狗那头会规范化它，拿规范名再报一次，
  /// 审计环里前后就还是同一个人；拿原样字符串报，两次留痕可能对不上。
  String? _operator;

  /// 正在路上的那次重新解锁。**单飞（single-flight）就靠它。**
  Future<void>? _relock;

  PatrolClient(this.baseUrl,
      {HttpClient? io, this.timeout = const Duration(seconds: 10)})
      : _io = io ?? HttpClient(),
        _ownsIo = io == null {
    if (_ownsIo) {
      // 狗在热点上，慢一点是常态，但不该等到人以为死机。
      // 只在自己造的连接上设——注入进来的那个不归我们管（N-1）。
      _io.connectionTimeout = const Duration(seconds: 5);
    }
  }

  String? get token => _token;

  /// 收连接。**只关自己造的那个（N-2）。**
  ///
  /// [force] 给 `false` 的时候是**温和地收**：不再受理新请求，但已经在路上的
  /// 那几条让它们跑完。退出遥控屏那条路要的就是这个（必修 2）——那一屏
  /// `dispose()` 里会补一拍全零，硬关的话那一拍连一个字节都出不去，而它正是
  /// 「松手就停」的最后一道兜底。**别的地方一律用默认的硬关**：连不上狗那
  /// 条路上没有什么值得等的东西。
  void close({bool force = true}) {
    if (_ownsIo) _io.close(force: force);
  }

  /// PIN 换 token。**PIN 不出这个函数。**
  ///
  /// 狗回的质询/应答缺了该有的字段时（连错热点、撞上强制门户都会长这样），
  /// **直接抛 `PatrolError`——绝不退回 `{'pin': pin}` 那条老路**去凑一个
  /// token。那条路狗会静默降级成只读凭证：遥控全部 403，报错指向权限而不
  /// 是「PIN 明文发出去了」，比一个裸的类型错误更难查（B-1）。
  Future<Session> unlock(String pin, {required String operator}) async {
    final ch = await _send('GET', '/api/auth/challenge', null,
        auth: false, allowRelock: false);
    final nonce = ch['nonce'];
    if (nonce is! String) {
      throw const PatrolError(
          0, '狗回的质询看不懂', '没有 nonce 字段。热点连对了吗，还是撞上了强制门户');
    }
    final got = await _send('POST', '/api/auth', <String, dynamic>{
      'nonce': nonce,
      'proof': proofFor(pin, nonce),
      'operator': operator,
    }, auth: false, allowRelock: false);
    final token = got['token'];
    if (token is! String) {
      throw const PatrolError(0, '狗回的应答看不懂', '没有 token 字段');
    }
    _token = token;
    final Session session = Session(
      token: token,
      operator: (got['operator'] as String?) ?? '',
      operatorVerified: got['operator_verified'] == true,
      readonly: got['readonly'] == true,
      notice: (got['notice'] as String?) ?? '',
    );
    // **成功了才记。** 失败的那次不许把上一份好凭证覆盖掉。
    _pin = pin;
    _operator = session.operator.isEmpty ? operator : session.operator;
    return session;
  }

  /// [deadline] 只给**周期路径**用：心跳、发拍、校准这些一秒钟发好几次的
  /// 小请求，等满 [timeout]（10 秒，那是给 unlock/盘况这类一次性请求的）
  /// 毫无意义 —— 那一拍的答案早就过期了。不给就还是 [timeout]。
  Future<Map<String, dynamic>> get(String path, [Duration? deadline]) =>
      _send('GET', path, null, deadline: deadline);

  /// 见 [get] 里那段关于 [deadline] 的话。
  ///
  /// **`deadline` 排在 `body` 后面是被 Dart 逼的**：一个函数不许同时有可选
  /// 位置参数和命名参数，而 `body` 这个位置参数已经有几十个调用点了。
  Future<Map<String, dynamic>> post(String path,
          [Object? body, Duration? deadline]) =>
      _send('POST', path, body, deadline: deadline);

  Future<Map<String, dynamic>> put(String path, [Object? body]) =>
      _send('PUT', path, body);

  /// 换人：把新的自报姓名告诉狗（`PUT` [operatorPath]）。
  ///
  /// **姓名的家在手机上**（`store/registry_store.dart`，按狗分别记）。这一条
  /// 不是"存到狗上"，它是让狗在审计环里留下一条「从这一刻起账记在这个名字
  /// 下」—— 人拍的板 3 接受的代价（甲忘了切，乙的操作签在甲名下）事后能翻
  /// 的就是这条记录。
  ///
  /// **狗不核实这个名字**（§6.3）。返回的报文里 `operator_verified` 恒为
  /// `false`，界面上不许说成「已登录」。
  Future<String> setOperator(String name) async {
    final r = await put(operatorPath, <String, dynamic>{'name': name});
    return (r['name'] as String?) ?? '';
  }

  // ---------------------------------------------------------------- 值守

  /// 未解决的告警。**P1 已经在最上面，这头不再排一遍。**
  ///
  /// 排法（先级别再 `last_ms` 倒序）是判据的一部分，判据只许在
  /// `engine/alerts.py` 里说一次 —— 狗那侧的 `_alerts_open` 处理器自己都写着
  /// 「这儿不再排一遍」。手机这头再排一次就是同一件事有两个出处，而对不上的
  /// 那天，屏幕第一行显示的不是该起身的那件事。
  Future<List<Alert>> alertsOpen() async =>
      alertsFromWire(await get('/api/alerts'));

  /// 连已确认、已解决的一起。交接班要看的是这一张。
  Future<List<Alert>> alertsAll() async =>
      alertsFromWire(await get('/api/alerts/all'));

  /// 记名确认：「我看见了，我在处理」（§5.3）。
  ///
  /// **`who` 空串狗那侧回 400**，这里不替它兜 —— 确认会把升级链停下来，
  /// 匿名确认等于任何人都能把声音关掉而没人负责。姓名从界面一路传下来
  /// （`ui/roster_page.dart` 里问的那一句），不拿会话上那个 `operator`
  /// 顶替：那个名字狗记下来但**不核实**（§6.3）。
  Future<Alert> ackAlert(String key, {required String who}) async =>
      Alert.fromWire(
          (await post('${_alertPath(key)}/ack', <String, dynamic>{'who': who}))[
                  'alert'] as Map<String, dynamic>? ??
              const <String, dynamic>{});

  /// 这件事没了。**不记名** —— 解决了不等于有人看见过。
  Future<Alert> resolveAlert(String key) async => Alert.fromWire(
      (await post('${_alertPath(key)}/resolve', const <String, dynamic>{}))[
              'alert'] as Map<String, dynamic>? ??
          const <String, dynamic>{});

  /// §5.1 那六项。
  Future<WatchSummary> watchSummary() async =>
      WatchSummary.fromWire(await get('/api/watch/summary'));

  /// 一条告警的路径前缀。**键要整段转义。**
  ///
  /// 键形如 `robot/kind#seq`（`engine/alerts.py` 的 `_key`），`/` 和 `#`
  /// 两个都在里面。狗那侧为此专门开了一个跨斜杠的路由占位符（`<key*>`），
  /// 但那救不了一个在客户端就已经被砍断的请求：裸着拼进去的话，`Uri.parse`
  /// 会把 `#` 之后的整段当 fragment 切掉 —— 连 `/ack` 都发不出去，而狗那边
  /// 回的是 404，报错指向「没这条告警」，不指向「路径拼错了」。
  static String _alertPath(String key) =>
      '/api/alerts/${Uri.encodeComponent(key)}';

  /// 发一次请求。**撞上 401 就自己重新解锁一次，再把原请求原样重发一次。**
  ///
  /// ## 为什么非要有这一支
  ///
  /// 手机在现场走的是狗的热点，而 `app/auth.py` 的
  /// `AP_TOKEN_IDLE_S = 30 * 60.0` —— **热点通道上的 token 闲置半小时就被
  /// 当成不存在**（`TokenStore.info()` 对闲置死的记录返回 `None`，只有急停
  /// 那条豁免路径认它）。「一只狗一条连接」之后，`PatrolClient` 是按 SN 缓存
  /// 住的（`ui/roster_page.dart` 的 `_clients`），进屏命中就直接复用、不再
  /// `unlock`。人从某一屏退回名册、把手机往兜里一揣，半小时之后再点进任何
  /// 一屏，那条缓存连接上的 token 已经死了 —— 没有这一支的话，这台手机对
  /// 这只狗就彻底废掉，唯一的出路是杀掉 app 重开。
  ///
  /// **不能退回「每进一次屏 unlock 一次」**：那样每进一次屏就烧掉狗那头一个
  /// 会话名额（`auth.MAX_SESSIONS = 3`），一台手机自己就能把名额坐满。
  ///
  /// ## 401 之后原样重发，为什么对每一条路由都是安全的
  ///
  /// 狗那头**鉴权在路由匹配之前**：`app/server.py` 的 `handle()` 第一句就是
  /// `self._auth.gate(...)`，`Denied` 当场翻成 `HttpError` 抛出去，底下
  /// `for route in self._routes` 那个循环压根走不到。也就是说**一个收到 401
  /// 的请求在狗那头一件事都没做过** —— 它不是「做了一半」，是「没进门」。
  /// 重发得到的因此是这条请求的**第一次**执行，不是第二次。幂等性在这里
  /// 根本没被动到。
  ///
  /// 手机这一侧会发的、本身**不**幂等的那几条，逐条核过：
  ///
  /// * `POST /api/control/acquire`、`/api/control/takeover`、
  ///   `/api/control/takeover/approve`、`/api/control/release`
  ///   （`ui/control_panel.dart` 的 `_act` / `_releaseOnLeave`）——
  ///   都在 `gate()` 后面，401 的那一下租约状态一个字节没动过。
  /// * `POST /api/teleop`、`/api/teleop/mode`（`ui/teleop_page.dart`）——
  ///   同上；何况这两条本来就是「最后一拍说了算」，重放同一拍是同一件事。
  /// * `PUT /api/operator`、`POST /api/alerts/<key>/ack`、`.../resolve` ——
  ///   同上，而且重放的是同一个名字、同一个键。
  /// * `POST /api/missions/<id>/run` **手机这一侧根本不发**（`mobile/lib`
  ///   里没有任何一处提到 missions），不在这条路上。
  ///
  /// 结论：**没有哪一条需要「只重新解锁、不重发」的口子，所以这里不开那个
  /// 口子。** 开一个没有调用方的参数，下一个人会以为某处真有不能重发的路由，
  /// 反倒把这段推导读歪。
  ///
  /// **这段推导的前提就一条：狗那头的鉴权在处理函数之前。** 哪天有人把某条
  /// 路由的鉴权挪进处理函数里（那样 401 就可能发生在它已经动过状态之后），
  /// 这段话连同这一支都要重写。狗那侧的
  /// `tests/app/test_auth.py` 是这条前提现在的守门人。
  ///
  /// [allowRelock] 是**挡递归的那道闸**：`unlock` 自己发的那两条请求给的是
  /// `false`，它们撞上 401（PIN 被改了）的时候绝不许再去重新解锁一次。
  ///
  /// **这道闸必须是按请求给的，不能是客户端身上一个「正在重新解锁」的全局
  /// 布尔。** 全局布尔会连坐：重新解锁那半秒里，另外两条周期路径撞上的 401
  /// 会被当成递归直接抛出去 —— 而它们恰恰是**应该等这一次结果**的那两条。
  Future<Map<String, dynamic>> _send(String method, String path, Object? body,
      {bool auth = true, Duration? deadline, bool allowRelock = true}) async {
    // 这一条**实际带出去的**是哪张 token。拿它跟事后的 `_token` 比，就知道
    // 「我撞 401 的这段时间里有没有别人已经把票换掉了」——换掉了就不必再换
    // 一次（见下面那个 `==`）。
    final String? used = _token;
    try {
      return await _sendOnce(method, path, body,
          auth: auth, deadline: deadline);
    } on PatrolError catch (first) {
      // 只有「带着 token 发出去、狗回了 401」这一种才走自动重新解锁。
      if (first.status != 401 || !auth || !allowRelock) rethrow;
      final String? pin = _pin;
      final String? operator = _operator;
      // 身上没有记着的凭证（从来没 `unlock` 过）时，**行为跟以前一模一样**：
      // 401 原样抛出去，不重试。
      if (pin == null || operator == null) rethrow;
      if (_token == used) {
        try {
          await _relockOnce(pin, operator);
        } on Object catch (_) {
          // 重新解锁失败（任何异常、任何非 200）：**把原来那个 401 原样抛
          // 出去**，不换话术。屏上那句「登录过期了，重新输一次 PIN」是狗写
          // 的，人照着做是对的；换成「重新登录失败」只会把人引去查网络。
          throw first;
        }
      }
      // **只重发一次。** 这一条走的是 `_sendOnce`，它自己不带这个 catch，
      // 所以重发再撞 401 就是直接抛出去，死循环不成立。
      return await _sendOnce(method, path, body,
          auth: auth, deadline: deadline);
    }
  }

  /// 重新解锁。**单飞（single-flight）：同一时刻只许有一个在路上。**
  ///
  /// 遥控屏上同时挂着发拍、心跳、校准三条周期路径，token 死掉的那一下它们
  /// 会几乎同时撞上 401。三条各自去 `unlock` 一次 = 一口气烧掉狗那头三个
  /// 会话名额（`auth.MAX_SESSIONS` 一共就 3 个），等于把「一只狗一条连接」
  /// 当场废掉。**其余的等这一次，拿同一个结果。**
  Future<void> _relockOnce(String pin, String operator) {
    final Future<void>? live = _relock;
    if (live != null) return live;
    // `_doRelock` 同步跑到 `unlock` 里第一个 await 才让出去，所以这一行
    // 赋值跟上一行的判空之间插不进别人 —— 单飞不会漏。
    return _relock = _doRelock(pin, operator);
  }

  Future<void> _doRelock(String pin, String operator) async {
    try {
      await unlock(pin, operator: operator);
    } finally {
      // **清在这儿。** 清晚了，下一次 401 会挂在一个已经完成的 future 上，
      // 永远等不到新的那张票。
      _relock = null;
    }
  }

  Future<Map<String, dynamic>> _sendOnce(
      String method, String path, Object? body,
      {bool auth = true, Duration? deadline}) async {
    final Duration cap = deadline ?? timeout;
    // 超时那一支要掐的就是它。**不能在 `_sendAndRead` 里面掐** —— 那个
    // future 超时之后没人再看它一眼，里面的 `req` 也就再也拿不到了。
    final _InFlight live = _InFlight();
    try {
      final res =
          await _sendAndRead(method, path, body, auth: auth, live: live)
              .timeout(cap);
      Map<String, dynamic> m = <String, dynamic>{};
      if (res.raw.isNotEmpty) {
        try {
          final parsed = jsonDecode(res.raw);
          if (parsed is Map<String, dynamic>) m = parsed;
        } on FormatException {
          // 非 JSON 的响应体解析不出来时，状态码仍然要报对（S-2）——
          // 下面照样走 >=400 分支，不许把这种情况压成「连不上狗」、
          // 状态码归零。
        }
      }
      if (res.status >= 400) {
        throw PatrolError(res.status,
            (m['error'] as String?) ?? '狗回了 ${res.status}',
            (m['detail'] as String?) ?? '');
      }
      return m;
    } on PatrolError {
      rethrow;
    } on TimeoutException {
      // **真的把这条连接掐掉（必修 4）。** `future.timeout` 只是不再等，
      // 底下那条 TCP 连接和 socket 一个都不会动 —— 而 `dart:io` 的
      // `HttpClient` 不限 `maxConnectionsPerHost`：狗那头 HTTP 线程卡住
      // 但 TCP 还接得上的时候（扫盘、写归档），周期路径会在超时窗口里堆
      // 出几十条谁也不回收的连接，把自己的热点上行挤死，而屏上报的是
      // 「看不见就不许开狗」，人会去修相机。
      live.kill();
      throw PatrolError(
          0, '狗没回话', '等了 ${cap.inMilliseconds} 毫秒。看看还连着它的热点吗');
    } catch (e) {
      // **这里原样带 `$e` 的前提是请求体里从来没有长期凭证（S-3）。**
      // `unlock` 走的是质询-应答，body 里只有 nonce/proof/operator，PIN
      // 本身从不出现在任何请求里；异常信息里能带的至多是 URL/nonce 这类
      // 不敏感的东西。哪天有人往某个请求体里塞了敏感字段，这一行要跟着
      // 改，不能让那类内容原样出现在错误信息里。
      throw PatrolError(0, '连不上狗', '$e');
    }
  }

  /// 发请求、等响应头、读完整个响应体——三步算一个超时窗口（B-2）。
  ///
  /// **只适用于一次性的 JSON 响应。** `/api/events`（SSE）和
  /// `/api/video/<name>`（MJPEG）是**永不结束**的响应：这里最后那句
  /// `.join()` 要等 `onDone`，而它永远不来，于是外面那个 `.timeout` 会把
  /// 一条正常工作的长连接整体掐掉，还报成「狗没回话」——文案指向的是狗，
  /// 真正的原因却是拿错了方法。这两条路自己开 `HttpClient` 增量读
  /// （MJPEG 的做法见 `ui/widget/live_video.dart` 的 `mjpegFrames`）。
  Future<({int status, String raw})> _sendAndRead(
      String method, String path, Object? body,
      {required bool auth, _InFlight? live}) async {
    final req = await _io.openUrl(method, Uri.parse('$baseUrl$path'));
    // 交给外面那一层，好让超时的时候掐得到（见 `_send`）。
    live?.req = req;
    // **不跟重定向。** 跟的话 302 会把下面那个 `Authorization: Bearer` 原样
    // 带到重定向目标去 —— 那是一条 token 外泄的路。狗从不发 3xx，关掉它
    // 不影响任何正常路径;真收到 3xx 的话现在会当成一个非 200 的状态码报
    // 出来，那也正是该有的反应。
    req.followRedirects = false;
    // token 走请求头，不走 cookie 也不走查询串。狗那头的理由写在
    // `auth.py` 开头：cookie 是浏览器自动带的，等于替人开狗。
    if (auth && _token != null) {
      req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $_token');
    }
    if (body != null) {
      req.headers.contentType = ContentType.json;
      req.write(jsonEncode(body));
    }
    final resp = await req.close();
    // **响应体一个字节一个字节地自己收，不用 `.join()`。** `.join()` 那条
    // 订阅是它自己在肚子里开的，外面拿不到，也就取消不了 —— 而「头到了、
    // body 永远不来」正是超时最常落在的那一步（见 [_InFlight.kill]）。
    final StringBuffer buf = StringBuffer();
    final Completer<void> done = Completer<void>();
    live?.body = utf8.decoder.bind(resp).listen(
      buf.write,
      onDone: () {
        if (!done.isCompleted) done.complete();
      },
      onError: (Object e, StackTrace st) {
        if (!done.isCompleted) done.completeError(e, st);
      },
      cancelOnError: true,
    );
    if (live?.body == null) {
      // 没人要那个把手（内部调用）的时候还是走最省事的那条。
      final String raw = await utf8.decoder.bind(resp).join();
      return (status: resp.statusCode, raw: raw);
    }
    await done.future;
    return (status: resp.statusCode, raw: buf.toString());
  }
}

/// 一条**正在路上**的请求的把手。超时那一下要靠它去掐。
///
/// **两样都要留。** `HttpClientRequest.abort()` 只在响应头**还没到**的时候
/// 才真的 `destroy()` 那条连接（`http_impl.dart` 的
/// `if (!_responseCompleter.isCompleted)`）；头到了、body 卡住的那一种 ——
/// 也就是狗那头 HTTP 线程正忙着扫盘写归档的那一种 —— `abort()` 是个空操作，
/// 连接会一直挂着。那一半只有取消响应体那条订阅才掐得掉。
class _InFlight {
  HttpClientRequest? req;
  StreamSubscription<String>? body;

  /// 掐掉这条连接。**两种收尾方式都试一遍，谁也不依赖谁。**
  void kill() {
    try {
      req?.abort();
    } on Object catch (_) {
      // 已经收尾了的请求再掐一次会抛，那不是错 —— 要的结果已经达到了。
    }
    try {
      unawaited(body?.cancel());
    } on Object catch (_) {
      // 同上。
    }
  }
}
