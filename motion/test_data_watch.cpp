// data_watch.hpp 的行为测试(11-02 工单 1):不链接厂商 SDK。
//   g++ -std=c++17 -pthread -o test_data_watch test_data_watch.cpp && ./test_data_watch
#include <cstdio>

#include "data_watch.hpp"

static int g_fail = 0;
#define CHECK(c)                                                    \
  do {                                                              \
    if (!(c)) {                                                     \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);      \
      ++g_fail;                                                     \
    }                                                               \
  } while (0)

int main() {
  {  // 开机一直没数据:过了 stale 就重订,之后每 retry 一次
    DataWatch w(3000, 5000, 0);
    CHECK(!w.Due(2999));
    CHECK(!w.Due(4999));
    CHECK(w.Due(5000));
    CHECK(!w.Due(9999));
    CHECK(w.Due(10000));
    CHECK(w.Tries() == 2 && !w.Ever());
  }
  {  // 数据一直来:不重订
    DataWatch w(3000, 5000, 0);
    for (int64_t t = 0; t < 60000; t += 100) {
      w.Seen(t);
      CHECK(!w.Due(t));
    }
    CHECK(w.Tries() == 0 && w.Ever());
  }
  {  // 来过又断:断满 stale 才重订;重订后来了就停
    DataWatch w(3000, 5000, 0);
    w.Seen(20000);
    CHECK(!w.Due(22000));
    CHECK(w.Due(23000));
    CHECK(w.StaleFor(23000) == 3000);
    w.Seen(29000);
    CHECK(!w.Due(30000));
    CHECK(!w.Due(31999));
    CHECK(w.Due(32000));
  }
  if (g_fail) return 1;
  std::printf("data_watch: ok\n");
  return 0;
}
