// 里程、IMU 断流就重订(11-02 工单 1)。
//
// 整机上电后旁路进程起得比运控板早:Set*Config 订阅那会儿运控板还没准备好,订失败不重试 —— 只有心跳
// 状态帧,没有里程、IMU,代理起不来、开机守卫把好包退回(2026-11-02 C40221 两次)。重启旁路进程马上好。
// 这里只判「该不该重订」:多久没收到算断、多久重试一次。不链接厂商 SDK(test_data_watch.cpp)。
#pragma once

#include <cstdint>
#include <mutex>

class DataWatch {
 public:
  /// stale_ms:这么久没收到算断流;retry_ms:断着的时候隔多久重订一次。start_ms:开始等的时刻。
  DataWatch(int64_t stale_ms, int64_t retry_ms, int64_t start_ms)
      : stale_(stale_ms), retry_(retry_ms), last_seen_(start_ms), last_try_(start_ms) {}

  /// 回调里每收到一帧调一次。
  void Seen(int64_t now_ms) {
    std::lock_guard<std::mutex> lk(m_);
    last_seen_ = now_ms;
    ever_ = true;
  }

  /// 这一刻该不该重订;该的话记下这次重订(下一次隔 retry_ms)。
  bool Due(int64_t now_ms) {
    std::lock_guard<std::mutex> lk(m_);
    if (now_ms - last_seen_ < stale_ || now_ms - last_try_ < retry_) return false;
    last_try_ = now_ms;
    ++tries_;
    return true;
  }

  /// 断了多久(毫秒)。
  int64_t StaleFor(int64_t now_ms) {
    std::lock_guard<std::mutex> lk(m_);
    return now_ms - last_seen_;
  }

  bool Ever() {
    std::lock_guard<std::mutex> lk(m_);
    return ever_;
  }

  int Tries() {
    std::lock_guard<std::mutex> lk(m_);
    return tries_;
  }

 private:
  std::mutex m_;
  int64_t stale_, retry_, last_seen_, last_try_;
  bool ever_ = false;
  int tries_ = 0;
};
