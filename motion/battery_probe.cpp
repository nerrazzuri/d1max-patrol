/**
 * battery_probe —— 只读探针(W73 现场 B4b):看厂家怎么报两块电池的「在位」和电量。
 *
 * 只订阅状态:不 TakeControl、不发任何运动 / 模式 / 急停命令;Ctrl+C 只断开连接。
 * 1 Hz 打印 present1/2、power1/2、voltage1/2、current1/2、temperature1/2、power_supply_status1/2,
 * 另外 present 或电量一变就立刻打一行(拔、插那一下看得到)。
 *
 * 要回答的两件事:拔出时 present 是不是变 false;快没电(接近 0%)时 present 是不是仍是 true。
 * 结论决定旁路要不要把 present 发给站点(现在旁路不在位就压成 0,站点分不出「拔了」和「真 0%」)。
 *
 * 编:在机器人 Orin 上 `make battery_probe ARCH=aarch64`(单文件,几秒)。
 * 跑:./battery_probe 192.168.168.168 8082 | tee battery-probe-$(date +%H%M%S).log
 */
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <memory>
#include <mutex>
#include <thread>
#include "robot_sdk/sdk_client.hpp"
using namespace robot_sdk;

static std::atomic<bool> g_stop{false};
static std::atomic<bool> g_connected{false};
static std::atomic<bool> g_connect_err{false};
static std::mutex g_mu;
static BatteryData g_last{};
static bool g_have = false;
static long g_frames = 0;

static void OnSig(int) { g_stop = true; }

static double Now() {
  using namespace std::chrono;
  return duration<double>(system_clock::now().time_since_epoch()).count();
}

static void Print(const char* tag, const BatteryData& b, long frames) {
  std::printf("%.3f %s frames=%ld | 1: present=%d power=%.1f%% V=%.2f I=%.2f T=%.1f status=%d"
              " | 2: present=%d power=%.1f%% V=%.2f I=%.2f T=%.1f status=%d\n",
              Now(), tag, frames,
              (int)b.present1, b.power1, b.voltage1, b.current1, b.temperature1,
              (int)b.power_supply_status1,
              (int)b.present2, b.power2, b.voltage2, b.current2, b.temperature2,
              (int)b.power_supply_status2);
  std::fflush(stdout);
}

class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    const BatteryData& b = d.battery;
    bool changed;
    long n;
    {
      std::lock_guard<std::mutex> lk(g_mu);
      changed = g_have && (b.present1 != g_last.present1 || b.present2 != g_last.present2 ||
                           (int)b.power1 != (int)g_last.power1 ||
                           (int)b.power2 != (int)g_last.power2);
      g_last = b;
      g_have = true;
      n = ++g_frames;
    }
    if (changed) Print("CHANGE", b, n);
  }
};

int main(int argc, char** argv) {
  if (argc < 3) {
    std::fprintf(stderr, "usage: %s <robot-ip> <port>   (只读:不接管、不发命令)\n", argv[0]);
    return 2;
  }
  std::signal(SIGINT, OnSig);
  std::signal(SIGTERM, OnSig);
  SDKClient client;
  client.SetDataCallback(std::make_shared<DataCb>());
  std::fprintf(stderr, "[battery_probe] connecting %s:%s (read-only)\n", argv[1], argv[2]);
  auto ec = client.Connect(argv[1], argv[2], false, [](const std::error_code& e) {
    if (!e) g_connected = true; else g_connect_err = true;
  });
  if (ec) {
    std::fprintf(stderr, "[battery_probe] Connect() failed: %s\n", ec.message().c_str());
    return 1;
  }
  for (int i = 0; i < 20 && !g_connected && !g_connect_err; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
  if (!g_connected) {
    std::fprintf(stderr, "[battery_probe] 没连上\n");
    return 1;
  }
  std::fprintf(stderr, "[battery_probe] connected; Ctrl+C 退出(只断开连接)\n");
  while (!g_stop) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    BatteryData b;
    long n;
    bool have;
    {
      std::lock_guard<std::mutex> lk(g_mu);
      b = g_last;
      n = g_frames;
      have = g_have;
    }
    if (have) Print("TICK", b, n);
    else std::fprintf(stderr, "[battery_probe] 还没收到状态帧\n");
  }
  client.Disconnect(true);
  std::fprintf(stderr, "[battery_probe] disconnected\n");
  return 0;
}
