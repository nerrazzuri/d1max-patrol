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
//     许可分两头(W09i):SDK 的「往前」走向哪一头,就要哪一头的许可 —— 狗头为前是狗头那头;狗尾为前
//     时 SDK 跟着调头(默认,真机项核)是狗尾那头,不跟就还是狗头那头;头尾不知道:不放。
// 作废 = 代次加一。目标记着登记时的代次,代次对不上就永远不再生效 —— 急停解除、重新站起
// 、控制权拿回来都不会让旧目标复活;只有之后新登记的 vel 才算数。

#pragma once

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

constexpr double kMaxFraction = 0.5;
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
        std::fabs(fwd) > kMaxFraction || std::fabs(lat) > kMaxFraction ||
        std::fabs(yaw) > kMaxFraction)
      return "速度分量要在 ±0.5 内";
    if (!std::isfinite(ttl_ms) || ttl_ms < kTtlMinMs || ttl_ms > kTtlMaxMs)
      return "ttl_ms 要在 [50, 1000] 内";
    std::lock_guard<std::mutex> lk(mtx_);
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
    if (!target_ || target_->epoch != epoch_ || now >= target_->until) return std::nullopt;
    Target t = *target_;
    if (require_clearance_ && t.fwd > 0) {
      const int lead = LeadEndLocked();
      if (lead < 0 || now >= clear_until_[lead]) t.fwd = 0;  // 往前走向的那一头没有许可:不许往前
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

  /// 调过头尾之后 SDK 的「往前」是不是跟着变成狗尾那头(旁路进程参数 --sdk-follows-head,默认是)。
  void SdkFollowsHead(bool on) {
    std::lock_guard<std::mutex> lk(mtx_);
    follows_head_ = on;
  }

  /// 感知节点的许可:「到 until 为止 end 那一头是空的」。只往后延,不往前缩(晚到的旧许可不会把新的
  /// 截短)。
  void SetClearance(Clock::time_point until, int end = kEndHead) {
    if (end != kEndHead && end != kEndTail) return;
    std::lock_guard<std::mutex> lk(mtx_);
    if (until > clear_until_[end]) clear_until_[end] = until;
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
  Clock::time_point clear_until_[2]{};
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
