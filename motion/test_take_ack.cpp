// take_ack.hpp 的行为测试(W35):不链接厂商 SDK。
//   g++ -std=c++17 -pthread -o test_take_ack test_take_ack.cpp && ./test_take_ack
#include <cstdio>
#include <thread>

#include "take_ack.hpp"

static int g_fail = 0;
#define CHECK(c)                                                    \
  do {                                                              \
    if (!(c)) {                                                     \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);      \
      ++g_fail;                                                     \
    }                                                               \
  } while (0)

using std::chrono::milliseconds;

int main() {
  {  // 回执 FAILURE:没成,原因照回执的(2026-10-13 现场那一种)
    TakeLedger l;
    auto s = l.Begin();
    l.OnAck(false, "Failed to take control, another master exists.");
    CHECK(l.Confirm(s, milliseconds(50)) == "Failed to take control, another master exists.");
  }
  {  // 回执 SUCCESS:成
    TakeLedger l;
    auto s = l.Begin();
    l.OnAck(true, "OK");
    CHECK(l.Confirm(s, milliseconds(50)).empty());
  }
  {  // 回执比 TakeControl 返回晚一点到:等得到
    TakeLedger l;
    auto s = l.Begin();
    std::thread t([&] {
      std::this_thread::sleep_for(milliseconds(30));
      l.OnAck(true, "OK");
    });
    CHECK(l.Confirm(s, milliseconds(1000)).empty());
    t.join();
  }
  {  // 一直没回执:没成;以前成过的那一次不算数
    TakeLedger l;
    l.OnAck(true, "OK");
    auto s = l.Begin();
    CHECK(!l.Confirm(s, milliseconds(30)).empty());
  }
  {  // 以前成过、这次 FAILURE:没成
    TakeLedger l;
    l.OnAck(true, "OK");
    auto s = l.Begin();
    l.OnAck(false, "");
    CHECK(!l.Confirm(s, milliseconds(30)).empty());
  }
  if (g_fail) return 1;
  std::printf("take_ack: ok\n");
  return 0;
}
