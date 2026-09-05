/// 手机上的机器名册。
///
/// **为什么名册必须在手机这边。** 每只狗只知道自己是谁（`GET /api/identity`），
/// 它不认识别的狗，也没道理认识 —— 客户买三只，三只之间没有网络往来。
/// "我手上有哪几只、各自的热点和 PIN 是什么"这件事只有操作员的手机知道，
/// 所以名册在这里，不在机器上。
///
/// **一次只能连一只。** 手机连的是狗自己开的热点，同一时刻只能待在一个热点上。
/// 所以名册不是"同时监控 N 只"的面板，是"我要去哪只"的选单。
library;

import 'robot.dart';

/// 名册里已经有这个 SN 了。
class DuplicateRobot implements Exception {
  final String sn;
  const DuplicateRobot(this.sn);
  @override
  String toString() => '名册里已经有 $sn 了';
}

/// 按 SSID 找狗，找出来不止一只。
///
/// **不挑一只交差。** 两条记录用同一个 SSID，说明名册本身填错了（或者现场
/// 真有两只重名的热点）。这时候随便返回一只，操作员就会在错的狗上按"跑" ——
/// 宁可把这个歧义摊到界面上让人来断。
class AmbiguousSsid implements Exception {
  final String ssid;
  final List<String> sns;
  const AmbiguousSsid(this.ssid, this.sns);
  @override
  String toString() => '热点 $ssid 对应了不止一只狗：${sns.join("、")}';
}

/// 一本名册。
///
/// 以 SN 为主键。顺序按加入的先后保留 —— 操作员心里对"我的第一只、第二只"
/// 是有次序的，按字母排会把这个次序打乱。
class RobotRegistry {
  final Map<String, Robot> _bySn = <String, Robot>{};

  RobotRegistry();

  /// 从盘上读回来的一串条目建名册。
  ///
  /// 遇到重复 SN 时**后面的覆盖前面的**而不是抛异常：这是读文件的路，文件
  /// 已经在盘上了，抛异常等于整本名册打不开。重复只可能来自我们自己写坏，
  /// 取最后一条至少还能用。加新条目那条路（[add]）才严格拒重。
  factory RobotRegistry.fromList(Iterable<Robot> robots) {
    final reg = RobotRegistry();
    for (final r in robots) {
      reg._bySn[r.sn] = r;
    }
    return reg;
  }

  /// 名册里所有的狗，按加入顺序。
  List<Robot> get all => List<Robot>.unmodifiable(_bySn.values);

  int get length => _bySn.length;
  bool get isEmpty => _bySn.isEmpty;

  /// 按 SN 取一只，没有就是 null。
  Robot? bySn(String sn) => _bySn[sn.trim()];

  bool contains(String sn) => _bySn.containsKey(sn.trim());

  /// 加一只新的。SN 已经在册就抛 [DuplicateRobot]。
  ///
  /// 想改已有的那只用 [upsert] —— 分成两个方法是故意的：界面上"新增"和
  /// "编辑"是两个动作，把它们并成一个的话，手滑重复添加会静默改掉旧记录。
  void add(Robot robot) {
    if (_bySn.containsKey(robot.sn)) {
      throw DuplicateRobot(robot.sn);
    }
    _bySn[robot.sn] = robot;
  }

  /// 加或改。在册就整条换掉，不在册就加上。
  void upsert(Robot robot) {
    _bySn[robot.sn] = robot;
  }

  /// 从名册里去掉。删掉了返回 true。
  ///
  /// **只动名册，不动机器。** 手机上划掉一条不会对那只狗做任何事，也不该会。
  bool remove(String sn) => _bySn.remove(sn.trim()) != null;

  /// 手机现在连着 [ssid] 这个热点，那是哪只狗？
  ///
  /// 认不出返回 null（可能是只没登记过的新狗）。对上不止一只就抛
  /// [AmbiguousSsid]，理由见那个异常的注释。
  Robot? bySsid(String ssid) {
    final want = ssid.trim();
    if (want.isEmpty) return null;
    final hit = _bySn.values.where((r) => r.ssid == want).toList();
    if (hit.isEmpty) return null;
    if (hit.length > 1) {
      throw AmbiguousSsid(want, hit.map((r) => r.sn).toList());
    }
    return hit.first;
  }

  /// 按热点的 BSSID 认狗。
  ///
  /// 比 [bySsid] 硬：SSID 是人能随便改的字符串，BSSID 是网卡地址。两只狗
  /// 的 SSID 撞名有可能，BSSID 撞不了。**连上之前就能读到**，所以这是
  /// "先认出是谁，再决定拿哪个 PIN"那条路。
  Robot? byBssid(String bssid) {
    final want = normalizeBssid(bssid);
    if (want.isEmpty) return null;
    for (final r in _bySn.values) {
      if (r.bssid.isNotEmpty && r.bssid == want) return r;
    }
    return null;
  }

  /// 落盘用的形状：一串条目。PIN 不在里面（见 `Robot.toJson`）。
  List<Map<String, dynamic>> toJson() =>
      _bySn.values.map((r) => r.toJson()).toList();
}
