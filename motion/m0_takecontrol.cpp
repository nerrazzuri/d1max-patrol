/**
 * M0 —— 运动测试第 0 步：只验证能否拿到/交还控制权。
 * 【不发任何运动指令】：没有 StandUp、没有 Move。
 * 流程：Connect → TakeControl → 等 2s → ReleaseControl → Disconnect → 退出。
 * 观察：TakeControlAck 成功/失败、control_source 变化、有无 Fault。
 */
#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <thread>

#include "robot_sdk/sdk_client.hpp"

using namespace robot_sdk;
static std::atomic<bool> g_connected{false};

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& ack) override {
    std::cout << "[CTRL] TakeControlAck: "
              << ((ack.error_code == 0) ? "SUCCESS" : "FAILURE")
              << "  error_code=" << ack.error_code
              << "  reason='" << ack.reason << "'" << std::endl;
  }
};

class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    static int src = -999;
    if ((int)d.control_source != src) {
      src = (int)d.control_source;
      std::cout << "[DATA] control_source = " << src
                << " (0=UNKNOWN 1=APP 2=SDK 3=OTHER)" << std::endl;
    }
  }
  void OnControlLost(const ControlLostInfo&) override {
    std::cout << "[DATA] control LOST" << std::endl;
  }
  void OnControlAvailable(const ControlAvailableInfo&) override {
    std::cout << "[DATA] control AVAILABLE" << std::endl;
  }
  void OnFaultData(const FaultDatas& f) override {
    for (const auto& x : f)
      std::cout << "[FAULT] level=" << (int)x.level << " code=" << (int)x.code
                << " " << x.message << std::endl;
  }
};

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "usage: " << argv[0] << " <ip> <port>   e.g. 192.168.234.1 8082\n";
    return 2;
  }
  std::string ip = argv[1], port = argv[2];

  SDKClient client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout << "[M0] connecting " << ip << ":" << port << " ..." << std::endl;
  auto ec = client.Connect(ip, port, false, [](const std::error_code& e) {
    if (!e) { g_connected = true; std::cout << "[M0] ✓ connected" << std::endl; }
    else std::cerr << "[M0] connect cb error: " << e.message() << std::endl;
  });
  if (ec) { std::cerr << "[M0] Connect() failed: " << ec.message() << std::endl; return 1; }

  for (int i = 0; i < 15 && !g_connected; ++i)
    std::this_thread::sleep_for(std::chrono::seconds(1));
  if (!g_connected) { std::cerr << "[M0] connection timeout\n"; return 1; }

  std::cout << "[M0] >>> TakeControl (NO motion commanded)" << std::endl;
  ec = client.TakeControl(5000);
  std::cout << "[M0] TakeControl() returned: " << (ec ? ec.message() : "ok") << std::endl;

  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout << "[M0] >>> ReleaseControl" << std::endl;
  ec = client.ReleaseControl(5000);
  std::cout << "[M0] ReleaseControl() returned: " << (ec ? ec.message() : "ok") << std::endl;

  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout << "[M0] done. NO MOTION was commanded." << std::endl;
  return 0;
}
