// vel_gate.hpp —— 持续速度(协议 v3 的 vel)的运动安全门与速度驱动。
//
// 不依赖厂商 SDK:patrol_agent.cpp 用它,test_vel_gate.cpp 拿假 SDK 测它(W00d 外审阻断 1)。
//
// 一把锁管住这几件事,互相之间没有缝:
//   - Register:检查(控制权、本地急停闩锁、两路急停、姿态、范围)与登记目标在同一把锁里;
//   - Cancel(halt)/LatchEstop(true):作废目标;
//   - OnState(SDK 状态回调):任一路急停不是明确的 Recover(Stop、Unknown、没见过的值)、
//     趴着、锁死、姿态未知 → 作废目标;
//   - OnControlLost:控制权丢了 → 作废目标(不是暂停:拿回控制权之后要新的 vel 才动);
//   - Live:速度线程每次要发 Move 之前都问一次,上面这些条件现查;
//   - 净空许可(W11 第二层,W08 决定 8):RequireClearance 打开之后,前进分量只在感知节点给的
//     许可没过期时放行,过期就把前进分量置零(转向、后退照常;不在 Register 里拒 —— 拒了代理当失败)。
//   - 速度档位(W12):SDK 的 Move 比例值换成 m/s 全看档位(低速 1.0 = 1 m/s、中速 1.0 = 2 m/s)。要了档位
//     (SetSpeedWant > 0)之后,状态回调报的档位跟要的不一样(有人在厂家 App 上换了档、没收到过状态)就不放行
//     任何运动 —— 代理按低速档的换算发的比例,换了档就快一倍;
//     许可分两头(W09i):SDK 的「往前」走向哪一头,就要哪一头的许可 —— 狗头为前是狗头那头;狗尾为前
//     时 SDK 跟着调头(默认,真机项核)是狗尾那头,不跟就还是狗头那头;头尾不知道:不放。
//   - 分档许可(W47,协议 10):许可带「这一头空到能跑多快」(比例上限),前进分量按它夹;**最新的许可
//     说了算**(障碍近了当拍就降),上限没升时有效期只往后延(晚到的同档许可不截短)。老许可不带速度,
//     按 kLegacyFraction(0.5,老的全档)算。
//   - 前进比例上限(SetMaxForward,旁路参数 --max-fraction,[0.5, 1.0],默认 0.5):超过 0.5 的前进**只在
//     开了净空许可门时**放行(没有第二层就最多 0.5,跟老的一样)。后退、横移、转向一直是 0.5。
// 作废 = 代次加一。目标记着登记时的代次,代次对不上就永远不再生效 —— 急停解除、重新站起
// 、控制权拿回来都不会让旧目标复活;只有之后新登记的 vel 才算数。

#pragma once

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <string>

namespace velgate {

using Clock = std::chrono::steady_clock;

// SDK 枚举码(sdk_type.hpp 的声明顺序,同 agent_frames.py 的 MOTION_BY_CODE / EMERGENCY_BY_CODE)。
constexpr int kMotionUnknown = 0;
constexpr int kMotionLieDown = 2;
constexpr int kMotionLocked = 4;
// EmergencyStatus 是三态:0 Unknown、1 Recover(已解除)、2 Stop。**只有 1 算安全。**
constexpr int kEstopRecover = 1;
// HeadDirection:1 = 狗头为前(装前雷达的那一头)、2 = 狗尾为前。
constexpr int kHeadForward = 1;
constexpr int kHeadTail = 2;
// 净空许可是哪一头的(W09i):狗头那头(前雷达)、狗尾那头(后雷达)。
constexpr int kEndHead = 0;
constexpr int kEndTail = 1;

constexpr double kMaxFraction = 0.5;       // 后退、横移、转向;前进的默认上限
constexpr double kLegacyFraction = 0.5;    // 不带速度的老许可、没开许可门时前进最多这么快
constexpr double kMaxForwardCeiling = 1.0;  // --max-fraction 最多给到这
// SetClearance 的结果:不认、收下、收下而且接管了(调用方关掉之前那条连接)。
constexpr int kPermitRejected = 0;
constexpr int kPermitAccepted = 1;
constexpr int kPermitTookOver = 2;
constexpr int kTtlMinMs = 50;
constexpr int kTtlMaxMs = 1000;

struct Target {
  double fwd = 0, lat = 0, yaw = 0;
  Clock::time_point until{};
  uint64_t epoch = 0;
};

class Gate {
 public:
  /// 登记一条 vel。返回空串 = 收下;否则是拒绝原因。检查与登记在同一把锁里。
  std::string Register(double fwd, double lat, double yaw, double ttl_ms, bool held,
                       Clock::time_point now) {
    if (!std::isfinite(fwd) || !std::isfinite(lat) || !std::isfinite(yaw) ||
        fwd < -kMaxFraction || std::fabs(lat) > kMaxFraction || std::fabs(yaw) > kMaxFraction)
      return "速度分量要在 ±0.5 内(前进见 --max-fraction)";
    if (!std::isfinite(ttl_ms) || ttl_ms < kTtlMinMs || ttl_ms > kTtlMaxMs)
      return "ttl_ms 要在 [50, 1000] 内";
    std::lock_guard<std::mutex> lk(mtx_);
    if (fwd > max_fwd_) return "前进比例超过 --max-fraction";
    if (!held) return "no_control";
    if (estop_latched_) return "急停生效中,拒绝动作";
    if (std::string why = UnsafeLocked(); !why.empty()) return why;
    if (after_check_hook_for_test) after_check_hook_for_test();  // 仍在锁里
    target_ = Target{fwd, lat, yaw,
                     now + std::chrono::milliseconds(static_cast<int>(ttl_ms)), epoch_};
    return "";
  }

  /// halt:作废当前目标。之后新登记的 vel 照常生效。
  void Cancel() {
    std::lock_guard<std::mutex> lk(mtx_);
    InvalidateLocked();
  }

  /// 本地急停闩锁。on:先闩上、作废目标,**再**去跟 SDK 说;off:SDK 确认解除之后才调。
  void LatchEstop(bool on) {
    std::lock_guard<std::mutex> lk(mtx_);
    estop_latched_ = on;
    if (on) InvalidateLocked();
  }

  /// 控制权丢了(SDK OnControlLost):作废目标。旁路进程会自动重新 TakeControl,旧目标不许跟着复活。
  void OnControlLost() {
    std::lock_guard<std::mutex> lk(mtx_);
    InvalidateLocked();
  }

  /// SDK 状态回调。不安全(任一路急停不是明确的 Recover、趴着、锁死、姿态未知)就作废目标。
  void OnState(int motion, int estop_sw, int estop_hw) {
    std::lock_guard<std::mutex> lk(mtx_);
    motion_ = motion;
    estop_sw_ = estop_sw;
    estop_hw_ = estop_hw;
    if (!UnsafeLocked().empty()) InvalidateLocked();
  }

  /// 现在该按哪条目标走;不该走就是空。每次要发 Move 之前都问。
  std::optional<Target> Live(bool held, Clock::time_point now) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!held) {
      InvalidateLocked();  // 看到过一次没控制权就作废:OnControlLost 回调漏了也兜得住
      return std::nullopt;
    }
    if (estop_latched_ || !UnsafeLocked().empty()) return std::nullopt;
    if (speed_want_ > 0 && speed_level_ != speed_want_) return std::nullopt;  // 档位不对:不走
    if (!target_ || target_->epoch != epoch_ || now >= target_->until) return std::nullopt;
    Target t = *target_;
    if (t.fwd > 0) {
      if (require_clearance_) {
        const int lead = LeadEndLocked();
        if (lead < 0 || now >= clear_until_[lead]) t.fwd = 0;  // 往前走向的那一头没有许可:不许往前
        else t.fwd = std::min(t.fwd, clear_cap_[lead]);        // 分档:许可说能多快就最多多快
      } else {
        t.fwd = std::min(t.fwd, kLegacyFraction);              // 没有第二层:老上限
      }
    }
    return t;
  }

  /// 净空许可开关(旁路进程参数 --require-clearance;W11 真机验收之前默认关)。
  void RequireClearance(bool on) {
    std::lock_guard<std::mutex> lk(mtx_);
    require_clearance_ = on;
  }

  /// 头尾方向(SDK 状态回调)。开了净空许可门时,往前走向哪一头就要那一头的许可(W11a、W09i)。
  void OnHead(int head) {
    std::lock_guard<std::mutex> lk(mtx_);
    head_ = head;
  }

  bool ClearanceRequired() {
    std::lock_guard<std::mutex> lk(mtx_);
    return require_clearance_;
  }

  /// 要哪个速度档(旁路进程参数 --speed-level,默认 1 低速;0 = 不管档位)。
  void SetSpeedWant(int level) {
    std::lock_guard<std::mutex> lk(mtx_);
    speed_want_ = level;
  }

  /// SDK 状态回调报的当前档位(0 未知)。
  void OnSpeedLevel(int level) {
    std::lock_guard<std::mutex> lk(mtx_);
    speed_level_ = level;
  }

  /// 调过头尾之后 SDK 的「往前」是不是跟着变成狗尾那头(旁路进程参数 --sdk-follows-head,默认是)。
  void SdkFollowsHead(bool on) {
    std::lock_guard<std::mutex> lk(mtx_);
    follows_head_ = on;
  }

  /// 感知节点的许可:「到 until 为止 end 那一头空到能按比例 cap 往前走」(老许可不带速度:0.5)。
  /// 上限按最新的这条(障碍近了当拍就降);上限没升时有效期只往后延,不往前缩(晚到的同档许可不截短;
  /// 降了档的按老期限也安全 —— 老许可在更快的速度下都够);升了档的只到它自己的期限。
  ///
  /// **按连接认**(#208 首审 G1、复查 H1):``conn`` 是旁路给每条连上来的连接的递增号,``seq`` 是那条连接
  /// 上感知节点的帧序号(老感知节点不带 = 0)。认的是最新那条发过许可的连接(**两头共用一个**,复查第三轮
  /// H1:接管是整条连接退役,不是只退一头):更旧连接的永远不认(哪一头的都不认,过期了也不认、不靠钟);
  /// 同一条连接里同一头带序号的要往上走;更新的连接发许可就接管(回 kPermitTookOver,``*retired`` 填被
  /// 退役的那条,调用方把它关掉)。判断和接管在同一把锁里(原来先问持有者再设,两条新连接能交错)。
  /// ``conn`` 0 = 不分连接(测试、老调用),照旧收。
  int SetClearance(Clock::time_point until, int end = kEndHead, double cap = kLegacyFraction,
                   uint64_t conn = 0, uint64_t seq = 0, Clock::time_point now = Clock::now(),
                   uint64_t* retired = nullptr) {
    (void)now;
    if (retired != nullptr) *retired = 0;
    if (end != kEndHead && end != kEndTail) return kPermitRejected;
    if (!std::isfinite(cap)) return kPermitRejected;
    cap = std::max(0.0, std::min(cap, kMaxForwardCeiling));
    std::lock_guard<std::mutex> lk(mtx_);
    int got = kPermitAccepted;
    if (conn > 0) {
      if (conn < permit_conn_) return kPermitRejected;
      if (conn == permit_conn_ && seq > 0 && seq <= clear_seq_[end]) return kPermitRejected;
      if (conn > permit_conn_) {
        got = kPermitTookOver;
        if (retired != nullptr) *retired = permit_conn_;
        permit_conn_ = conn;
        clear_seq_[kEndHead] = clear_seq_[kEndTail] = 0;
      }
      if (seq > 0) clear_seq_[end] = seq;
    }
    if (cap > clear_cap_[end]) clear_until_[end] = until;
    else if (until > clear_until_[end]) clear_until_[end] = until;
    clear_cap_[end] = cap;
    return got;
  }

  /// 现在认的是哪条连接的许可(0 = 还没有)。
  uint64_t PermitConn() {
    std::lock_guard<std::mutex> lk(mtx_);
    return permit_conn_;
  }

  /// 前进比例上限(旁路参数 --max-fraction):只许 [0.5, 1.0]。超过 0.5 的那段要开许可门才放行。
  bool SetMaxForward(double frac) {
    if (!std::isfinite(frac) || frac < kMaxFraction || frac > kMaxForwardCeiling) return false;
    std::lock_guard<std::mutex> lk(mtx_);
    max_fwd_ = frac;
    return true;
  }

  double MaxForward() {
    std::lock_guard<std::mutex> lk(mtx_);
    return max_fwd_;
  }

  /// 只给测试用:检查过了、还没登记时调(**锁还握着**)。测试在这里起一个线程去 halt,
  /// 证明 halt 插不进检查与登记之间。产品代码不设它。
  std::function<void()> after_check_hook_for_test;

  bool EstopLatched() {
    std::lock_guard<std::mutex> lk(mtx_);
    return estop_latched_;
  }

 private:
  std::string UnsafeLocked() const {
    if (estop_sw_ != kEstopRecover || estop_hw_ != kEstopRecover)
      return "急停生效或状态未知,拒绝动作";
    if (motion_ == kMotionUnknown || motion_ == kMotionLieDown || motion_ == kMotionLocked)
      return "趴着/锁死/姿态未知,走不了,先 stand";
    return "";
  }

  void InvalidateLocked() {
    ++epoch_;
    target_.reset();
  }

  /// SDK 的「往前」走向哪一头;头尾不知道 -1。
  int LeadEndLocked() const {
    if (head_ == kHeadForward) return kEndHead;
    if (head_ == kHeadTail) return follows_head_ ? kEndTail : kEndHead;
    return -1;
  }

  std::mutex mtx_;
  uint64_t epoch_ = 0;
  bool estop_latched_ = false;
  int motion_ = kMotionUnknown;  // 没收到过状态 = 姿态未知 = 不许走
  int estop_sw_ = 0, estop_hw_ = 0;
  std::optional<Target> target_;
  bool require_clearance_ = false;
  int head_ = 0;
  bool follows_head_ = true;
  int speed_want_ = 0;
  int speed_level_ = 0;
  Clock::time_point clear_until_[2]{};
  double clear_cap_[2]{0.0, 0.0};
  uint64_t permit_conn_ = 0;
  uint64_t clear_seq_[2]{0, 0};
  double max_fwd_ = kMaxFraction;
};

/// 速度线程的一拍。Sdk 要有 ``int Gait(int)``(返回 0 = 成功)与 ``void Move(float, float, float)``。
///
/// 「还该不该走」在拿到 SDK 锁之后、Gait 回来之后、发 Move 之前**都现问 Gate**:等锁(可能排在
/// 一整段 walk 后面)或 Gait 阻塞期间被叫停、急停、趴下、过期,就不发那条非零 Move。
/// 已知上限:Gait 是 SDK 的阻塞调用,halt/estop 正好落在里面时要等它回来(最坏 2 s)。
template <class Sdk>
class Driver {
 public:
  Driver(Gate& gate, Sdk& sdk, std::mutex& sdk_mtx, std::function<bool()> held,
         std::function<Clock::time_point()> now, std::function<void(int)> sleep_ms)
      : gate_(gate), sdk_(sdk), sdk_mtx_(sdk_mtx), held_(std::move(held)),
        now_(std::move(now)), sleep_ms_(std::move(sleep_ms)) {}

  void Tick() {
    if (gate_.Live(held_(), now_())) {
      std::lock_guard<std::mutex> lk(sdk_mtx_);
      if (!moving_ && gate_.Live(held_(), now_())) {
        if (sdk_.Gait(2000) == 0) moving_ = true;
      }
      if (!moving_) return;
      if (auto t = gate_.Live(held_(), now_())) {
        sdk_.Move(static_cast<float>(t->lat), static_cast<float>(t->fwd),
                  static_cast<float>(t->yaw));
      } else {
        sdk_.Move(0, 0, 0);  // 这一拍里被叫停/急停/过期了:零速,下一拍走收尾
      }
    } else if (moving_) {
      std::lock_guard<std::mutex> lk(sdk_mtx_);
      for (int i = 0; i < 3; ++i) {  // 连发零速:丢一两包也还是能停下来
        sdk_.Move(0, 0, 0);
        sleep_ms_(50);
      }
      moving_ = false;
    }
  }

  bool moving() const { return moving_; }

 private:
  Gate& gate_;
  Sdk& sdk_;
  std::mutex& sdk_mtx_;
  std::function<bool()> held_;
  std::function<Clock::time_point()> now_;
  std::function<void(int)> sleep_ms_;
  bool moving_ = false;
};

}  // namespace velgate
