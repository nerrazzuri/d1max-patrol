/**
 * M1 —— 运动测试第 1 步：原地站立（StandUp）。
 * 【只发一个离散姿态指令，不平移】。
 * 流程：Connect → TakeControl → StandUp → 观察 4s → ReleaseControl → Disconnect。
 * Ctrl+C 会立即发 SoftEmergencyStop(on) 再退出（软急停兜底；硬急停仍以现场按钮为准）。
 */
#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <memory>
#include <thread>

#include "robot_sdk/sdk_client.hpp"

using namespace robot_sdk;
static std::atomic<bool> g_connected{false};
static SDKClient* g_client = nullptr;

static void OnSig(int) {
  if (g_client) {
    std::cout << "\n[M1] SIGINT → SoftEmergencyStop(on) + ReleaseControl" << std::endl;
    g_client->SoftEmergencyStop(true, 2000);
    g_client->ReleaseControl(2000);
  }
  std::_Exit(130);
}

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout << "[CTRL] TakeControlAck: " << (a.error_code == 0 ? "SUCCESS" : "FAILURE")
              << " reason='" << a.reason << "'" << std::endl;
  }
  void OnStandUp() override { std::cout << "[CTRL] robot acked StandUp" << std::endl; }
  void OnSoftEmergencyStop(bool on) override {
    std::cout << "[CTRL] SoftEmergencyStop ack: " << (on ? "ON" : "OFF") << std::endl;
  }
};

class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    static int src = -999;
    if ((int)d.control_source != src) {
      src = (int)d.control_source;
      std::cout << "[DATA] control_source = " << src << " (2=SDK)" << std::endl;
    }
  }
  void OnFaultData(const FaultDatas& f) override {
    for (const auto& x : f)
      std::cout << "[FAULT] level=" << (int)x.level << " code=" << (int)x.code
                << " " << x.message << std::endl;
  }
};

int main(int argc, char** argv) {
  if (argc < 3) { std::cerr << "usage: " << argv[0] << " <ip> <port>\n"; return 2; }
  std::string ip = argv[1], port = argv[2];
  std::signal(SIGINT, OnSig);
  std::signal(SIGTERM, OnSig);

  SDKClient client;
  g_client = &client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout << "[M1] connecting " << ip << ":" << port << " ..." << std::endl;
  auto ec = client.Connect(ip, port, false, [](const std::error_code& e) {
    if (!e) { g_connected = true; std::cout << "[M1] ✓ connected" << std::endl; }
    else std::cerr << "[M1] connect cb error: " << e.message() << std::endl;
  });
  if (ec) { std::cerr << "[M1] Connect() failed: " << ec.message() << std::endl; return 1; }
  for (int i = 0; i < 15 && !g_connected; ++i)
    std::this_thread::sleep_for(std::chrono::seconds(1));
  if (!g_connected) { std::cerr << "[M1] connection timeout\n"; return 1; }

  std::cout << "[M1] >>> TakeControl" << std::endl;
  ec = client.TakeControl(5000);
  std::cout << "[M1] TakeControl() returned: " << (ec ? ec.message() : "ok") << std::endl;
  if (ec) { client.Disconnect(true); return 1; }

  std::this_thread::sleep_for(std::chrono::milliseconds(500));

  std::cout << "[M1] >>> StandUp (原地站立，不平移)" << std::endl;
  ec = client.StandUp(3000);
  std::cout << "[M1] StandUp() returned: " << (ec ? ec.message() : "ok") << std::endl;

  std::cout << "[M1] 观察 4 秒..." << std::endl;
  std::this_thread::sleep_for(std::chrono::seconds(4));

  std::cout << "[M1] >>> ReleaseControl" << std::endl;
  client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout << "[M1] done. 只做了 StandUp，未平移。" << std::endl;
  return 0;
}
