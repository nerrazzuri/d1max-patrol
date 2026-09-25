// test_vel_gate.cpp —— vel_gate.hpp 的行为测试(W00d 外审阻断 1)。不链接厂商 SDK:
//   make test_vel_gate && ./test_vel_gate
// 任何一条失败就打印并退出码非 0。tests/sim/test_vel_gate_cpp.py 编译并运行它。

#include "vel_gate.hpp"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

using velgate::Clock;
using velgate::Driver;
using velgate::Gate;

static int g_failed = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (!(cond)) {                                                      \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      ++g_failed;                                                       \
    }                                                                   \
  } while (0)

constexpr int kGeneral = 5, kLieDown = 2, kLocked = 4, kRecover = 1, kStop = 2, kUnknown = 0;

struct FakeSdk {
  struct Call { std::string what; float lat, fwd, yaw; };
  std::vector<Call> calls;
  int gait_rc = 0;
  std::function<void()> during_gait;  // 模拟 Gait(2000) 阻塞期间别的线程干的事
  int Gait(int) {
    calls.push_back({"gait", 0, 0, 0});
    if (during_gait) during_gait();
    return gait_rc;
  }
  void Move(float lat, float fwd, float yaw) { calls.push_back({"move", lat, fwd, yaw}); }
  int nonzero_moves() const {
    int n = 0;
    for (auto& c : calls)
      if (c.what == "move" && (c.lat != 0 || c.fwd != 0 || c.yaw != 0)) ++n;
    return n;
  }
};

struct Rig {
  Gate gate;
  FakeSdk sdk;
  std::mutex sdk_mtx;
  bool held = true;
  Clock::time_point now = Clock::now();
  Driver<FakeSdk> drv{gate, sdk, sdk_mtx, [this] { return held; }, [this] { return now; },
                      [](int) {}};
  Rig() { gate.OnState(kGeneral, kRecover, kRecover); }
  std::string vel(double fwd = 0.3, double ttl = 300) {
    return gate.Register(fwd, 0, 0, ttl, held, now);
  }
};

static void 收下就走_到期自停() {
  Rig r;
  CHECK(r.vel() == "");
  r.drv.Tick();
  CHECK(r.sdk.calls.size() == 2 && r.sdk.calls[0].what == "gait");
  CHECK(r.sdk.nonzero_moves() == 1 && r.sdk.calls[1].fwd == 0.3f);
  r.now += std::chrono::milliseconds(301);
  r.drv.Tick();
  CHECK(!r.drv.moving());
  CHECK(r.sdk.nonzero_moves() == 1);  // 过期之后只有零速
}

static void 没收到过状态_不许走() {
  Gate g;
  CHECK(g.Register(0.3, 0, 0, 300, true, Clock::now()) != "");
}

static void 急停与姿态_登记时就拒() {
  for (auto [m, sw, hw] : {std::tuple{kLieDown, kRecover, kRecover},
                           std::tuple{kLocked, kRecover, kRecover},
                           std::tuple{kUnknown, kRecover, kRecover},
                           std::tuple{kGeneral, kStop, kRecover},
                           std::tuple{kGeneral, kRecover, kStop}}) {
    Rig r;
    r.gate.OnState(m, sw, hw);
    CHECK(r.vel() != "");
  }
  Rig r;
  r.held = false;
  CHECK(r.vel() == "no_control");
  Rig r2;
  CHECK(r2.vel(0.6) != "" && r2.vel(0.3, 10) != "" && r2.vel(0.3, 5000) != "");
}

static void halt之后旧目标不复活_新目标照常() {
  Rig r;
  CHECK(r.vel() == "");
  r.gate.Cancel();
  CHECK(!r.gate.Live(true, r.now));
  CHECK(r.vel() == "");  // halt 之后新来的 vel 是合法的新命令
  CHECK(r.gate.Live(true, r.now).has_value());
}

static void 急停闩锁_解除后旧目标不复活() {
  Rig r;
  CHECK(r.vel() == "");
  r.gate.LatchEstop(true);
  CHECK(!r.gate.Live(true, r.now));
  CHECK(r.vel() != "");  // 闩着的时候登记不了
  r.gate.LatchEstop(false);
  CHECK(!r.gate.Live(true, r.now));  // 急停前收下的那条不许在急停解除后生效
}

static void 状态变坏_目标作废_状态恢复也不复活() {
  for (auto [m, sw, hw] : {std::tuple{kLieDown, kRecover, kRecover},
                           std::tuple{kLocked, kRecover, kRecover},
                           std::tuple{kUnknown, kRecover, kRecover},
                           std::tuple{kGeneral, kRecover, kStop},
                           std::tuple{kGeneral, kStop, kRecover}}) {
    Rig r;
    CHECK(r.vel() == "");
    r.gate.OnState(m, sw, hw);
    CHECK(!r.gate.Live(true, r.now));
    r.gate.OnState(kGeneral, kRecover, kRecover);
    CHECK(!r.gate.Live(true, r.now));
  }
}

static void Gait阻塞期间被急停_趴下_叫停_不发非零Move() {
  const std::vector<std::function<void(Gate&)>> 期间 = {
      [](Gate& g) { g.OnState(kGeneral, kRecover, kStop); },  // 硬急停按下
      [](Gate& g) { g.OnState(kLieDown, kRecover, kRecover); },
      [](Gate& g) { g.LatchEstop(true); },
      [](Gate& g) { g.Cancel(); },
  };
  for (auto& f : 期间) {
    Rig r;
    CHECK(r.vel() == "");
    r.sdk.during_gait = [&] { f(r.gate); };
    r.drv.Tick();
    CHECK(r.sdk.nonzero_moves() == 0);
    r.drv.Tick();
    r.drv.Tick();
    CHECK(r.sdk.nonzero_moves() == 0 && !r.drv.moving());
  }
}

static void Gait阻塞期间控制权丢了_不发非零Move() {
  Rig r;
  CHECK(r.vel() == "");
  r.sdk.during_gait = [&] { r.held = false; };
  r.drv.Tick();
  CHECK(r.sdk.nonzero_moves() == 0);
}

static void Gait失败_不发Move() {
  Rig r;
  CHECK(r.vel() == "");
  r.sdk.gait_rc = 1;
  r.drv.Tick();
  CHECK(r.sdk.calls.size() == 1 && r.sdk.calls[0].what == "gait");
}

static void 等SDK锁期间被叫停_拿到锁后不发非零Move() {
  Rig r;
  CHECK(r.vel() == "");
  std::unique_lock<std::mutex> busy(r.sdk_mtx);  // 模拟一整段 walk 握着 SDK 锁
  std::thread t([&] { r.drv.Tick(); });
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  r.gate.Cancel();
  busy.unlock();
  t.join();
  CHECK(r.sdk.nonzero_moves() == 0);
  CHECK(r.sdk.calls.empty());  // 拿到锁后先问 Gate:连 Gait 都不起
}

static void 走着走着过期或急停_本拍零速_下一拍收尾() {
  Rig r;
  CHECK(r.vel() == "");
  r.drv.Tick();
  CHECK(r.sdk.nonzero_moves() == 1);
  r.gate.OnState(kGeneral, kStop, kRecover);
  r.drv.Tick();
  CHECK(r.sdk.nonzero_moves() == 1 && !r.drv.moving());
  int zeros = 0;
  for (auto& c : r.sdk.calls)
    if (c.what == "move" && c.fwd == 0) ++zeros;
  CHECK(zeros == 3);
}

// 外审描述的交错:vel 过了检查、halt/estop 加代次、vel 再登记 → 目标越过停车存活。
// 检查与登记在同一把锁里之后,这个交错不存在:登记要么整个在作废之前(被作废),要么整个在
// 之后(那就是停车之后的新命令)。压测:闩上急停之后,任何时刻都不许有活目标、也登记不进来。
static void 并发压测_急停闩上之后没有活目标() {
  for (int round = 0; round < 200; ++round) {
    Rig r;
    std::atomic<bool> latched{false}, stop{false};
    std::atomic<int> alive_after{0}, accepted_after{0};
    std::vector<std::thread> ts;
    for (int i = 0; i < 3; ++i) {
      ts.emplace_back([&] {
        while (!stop.load()) {
          const bool was = latched.load();
          const bool ok = r.gate.Register(0.3, 0, 0, 300, true, r.now).empty();
          if (was && ok) ++accepted_after;
          if (latched.load() && r.gate.Live(true, r.now)) ++alive_after;
        }
      });
    }
    std::this_thread::sleep_for(std::chrono::microseconds(200));
    r.gate.LatchEstop(true);
    latched = true;
    std::this_thread::sleep_for(std::chrono::microseconds(200));
    stop = true;
    for (auto& t : ts) t.join();
    CHECK(accepted_after.load() == 0);
    CHECK(alive_after.load() == 0);
  }
}

// 外审描述的交错,用钩子确定性地造出来:vel 刚过检查,另一个线程(另一个客户端)发 halt。
// halt 必须等 vel 登记完才能进门,进门就作废它 —— 登记下来的这条不许活。
static void halt插不进检查与登记之间() {
  for (int round = 0; round < 20; ++round) {
    Rig r;
    std::thread halter;
    std::atomic<bool> halted{false};
    r.gate.after_check_hook_for_test = [&] {
      halter = std::thread([&] {
        r.gate.Cancel();
        halted = true;
      });
      std::this_thread::sleep_for(std::chrono::milliseconds(5));  // 给 halt 充分的机会插队
    };
    CHECK(r.vel() == "");
    halter.join();
    CHECK(halted.load());
    CHECK(!r.gate.Live(true, r.now));
  }
}

// 复审阻断 1:急停是三态。两路都**明确** Recover 才算安全,Unknown 与 Stop 一样拒。
static void 急停Unknown_登记就拒() {
  for (auto [sw, hw] : {std::pair{kUnknown, kRecover}, std::pair{kRecover, kUnknown},
                        std::pair{kUnknown, kUnknown}, std::pair{7, kRecover}}) {
    Rig r;
    r.gate.OnState(kGeneral, sw, hw);
    CHECK(r.vel() != "");
  }
}

static void 走着时一路急停变Unknown_目标作废_恢复Recover也不复活() {
  for (auto [sw, hw] : {std::pair{kUnknown, kRecover}, std::pair{kRecover, kUnknown}}) {
    Rig r;
    CHECK(r.vel() == "");
    r.drv.Tick();
    CHECK(r.sdk.nonzero_moves() == 1);
    r.gate.OnState(kGeneral, sw, hw);
    CHECK(!r.gate.Live(true, r.now));
    r.gate.OnState(kGeneral, kRecover, kRecover);
    CHECK(!r.gate.Live(true, r.now));
    r.drv.Tick();
    CHECK(r.sdk.nonzero_moves() == 1 && !r.drv.moving());
  }
}

// 复审阻断 2:控制权丢了是安全状态转换 —— 作废目标,不是暂停。重新拿到控制权之后,
// 要一条新的 vel 才动。
static void 控制权丢了_目标作废_拿回来也不复活_新目标照常() {
  Rig r;
  CHECK(r.vel(0.3, 1000) == "");
  r.gate.OnControlLost();                      // 两拍之间丢了又拿回来:速度线程没看见 held=false
  r.held = true;                               // OnControlAvailable 自动重新 TakeControl
  CHECK(!r.gate.Live(true, r.now));
  r.drv.Tick();
  CHECK(r.sdk.nonzero_moves() == 0);
  CHECK(r.vel() == "");
  CHECK(r.gate.Live(true, r.now).has_value());
}

static void 两拍之间控制权短暂丢失_也不复活() {
  // 回调漏了也兜得住:Live 看到过一次 held=false 就作废目标。
  Rig r;
  CHECK(r.vel(0.3, 1000) == "");
  CHECK(!r.gate.Live(false, r.now));
  CHECK(!r.gate.Live(true, r.now));
}

int main() {
  收下就走_到期自停();
  没收到过状态_不许走();
  急停与姿态_登记时就拒();
  halt之后旧目标不复活_新目标照常();
  急停闩锁_解除后旧目标不复活();
  状态变坏_目标作废_状态恢复也不复活();
  Gait阻塞期间被急停_趴下_叫停_不发非零Move();
  Gait阻塞期间控制权丢了_不发非零Move();
  Gait失败_不发Move();
  等SDK锁期间被叫停_拿到锁后不发非零Move();
  走着走着过期或急停_本拍零速_下一拍收尾();
  并发压测_急停闩上之后没有活目标();
  halt插不进检查与登记之间();
  急停Unknown_登记就拒();
  走着时一路急停变Unknown_目标作废_恢复Recover也不复活();
  控制权丢了_目标作废_拿回来也不复活_新目标照常();
  两拍之间控制权短暂丢失_也不复活();
  if (g_failed) {
    std::printf("%d 条失败\n", g_failed);
    return 1;
  }
  std::printf("vel_gate: 全部通过\n");
  return 0;
}
