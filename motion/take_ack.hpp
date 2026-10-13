// 抢控制权到底成没成(W35)。
//
// SDK 的 TakeControl(timeout) 回「没出错」只说明**收到了回执**,不说明回执是成功:遥控器、别的主控
// 还拿着的时候回执是 FAILURE「another master exists」,TakeControl 照样回没出错(2026-10-13 C40221
// 实测:旁路每 6 秒记一次「自己重抢回了控制权」、把 held 置真,其实一次都没抢到)。成没成只看
// OnTakeControlAck。
//
// 这里只记回执、给调用方等:不链接厂商 SDK,开发机上就能测(test_take_ack.cpp)。
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>

class TakeLedger {
 public:
  /// 发 TakeControl 之前调:记下这时候已经收过几次回执。
  uint64_t Begin() {
    std::lock_guard<std::mutex> lk(m_);
    return n_;
  }

  /// OnTakeControlAck 里调。
  void OnAck(bool ok, const std::string& reason) {
    {
      std::lock_guard<std::mutex> lk(m_);
      ++n_;
      ok_ = ok;
      reason_ = reason;
    }
    cv_.notify_all();
  }

  /// TakeControl 回了没出错之后调:等 ``since`` 之后的回执(最多 ``wait``)。成了回空串;没成回
  /// 回执里的原因;没等到回执也算没成。
  std::string Confirm(uint64_t since, std::chrono::milliseconds wait) {
    std::unique_lock<std::mutex> lk(m_);
    if (!cv_.wait_for(lk, wait, [&] { return n_ != since; }))
      return "没等到 TakeControl 的回执";
    if (ok_) return "";
    return reason_.empty() ? "回执是 FAILURE" : reason_;
  }

 private:
  std::mutex m_;
  std::condition_variable cv_;
  uint64_t n_ = 0;
  bool ok_ = false;
  std::string reason_;
};
