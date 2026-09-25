// vel_gate.hpp —— 持续速度(协议 v3 的 vel)的运动安全门与速度驱动。
//
// 不依赖厂商 SDK:patrol_agent.cpp 用它,test_vel_gate.cpp 拿假 SDK 测它(W00d 外审阻断 1)。
//
// 一把锁管住四件事,互相之间没有缝:
//   - Register:检查(控制权、本地急停闩锁、两路急停、姿态、范围)与登记目标在同一把锁里;
//   - Cancel(halt)/LatchEstop(true):作废目标;
//   - OnState(SDK 状态回调):任一路急停、趴着、锁死、姿态未知 → 作废目标;
//   - Live:速度线程每次要发 Move 之前都问一次,上面这些条件现查。
// 作废 = 代次加一。目标记着登记时的代次,代次对不上就永远不再生效 —— 急停解除、重新站起
// 都不会让旧目标复活;只有之后新登记的 vel 才算数。

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
constexpr int kEstopStop = 2;

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

  /// SDK 状态回调。不安全(任一路急停生效、趴着、锁死、姿态未知)就作废目标。
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
    if (!held || estop_latched_ || !UnsafeLocked().empty()) return std::nullopt;
    if (!target_ || target_->epoch != epoch_ || now >= target_->until) return std::nullopt;
    return target_;
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
    if (estop_sw_ == kEstopStop || estop_hw_ == kEstopStop) return "急停生效中,拒绝动作";
    if (motion_ == kMotionUnknown || motion_ == kMotionLieDown || motion_ == kMotionLocked)
      return "趴着/锁死/姿态未知,走不了,先 stand";
    return "";
  }

  void InvalidateLocked() {
    ++epoch_;
    target_.reset();
  }

  std::mutex mtx_;
  uint64_t epoch_ = 0;
  bool estop_latched_ = false;
  int motion_ = kMotionUnknown;  // 没收到过状态 = 姿态未知 = 不许走
  int estop_sw_ = 0, estop_hw_ = 0;
  std::optional<Target> target_;
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
