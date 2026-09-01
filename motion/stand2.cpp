/**
 * stand2 —— 让 D1 Max 站起来（修正版，SDK 0.2.0）。
 * 关键：开机后机器处于 MachineStatus::IDLE（电机泄力、趴地），
 * 必须先 SwitchRemoteState() 切到 REMOTE，姿态命令才会真正执行。
 * 流程：Connect → TakeControl → 打印初始状态 → SwitchRemoteState
 *       → StandUp → 观察 8s（看 motion_status 是否变 STAND_UP）
 *       → ReleaseControl → Disconnect。
 * 只做站立，不平移。Ctrl+C 会发 SoftEmergencyStop(on)。
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
static std::atomic<int> g_machine{-1};
static std::atomic<int> g_motion{-1};
static SDKClient* g_client = nullptr;

static const char* Machine(int s) {
  switch (s) { case 0:return "UNKNOWN";case 1:return "IDLE";case 2:return "REMOTE";
    case 3:return "OTA";case 4:return "RECHARGE";case 5:return "MAPPING";
    case 6:return "NAVIGATION";case 7:return "SAFETY";case 8:return "SELFTEST";
    default:return "other"; }
}
static const char* Motion(int s) {
  switch (s) { case 0:return "UNKNOWN";case 1:return "STAND_UP";case 2:return "LIE_DOWN";
    case 3:return "CRAWL";case 4:return "LOCKED";case 5:return "GENERAL";
    case 6:return "IN_PLACE";case 7:return "STAIR";case 8:return "CLIMB";
    case 9:return "SLIM";case 10:return "GAIT";default:return "other"; }
}

static void OnSig(int) {
  if (g_client) { std::cout << "\n[stand2] SIGINT → SoftEmergencyStop(on)+Release\n";
    g_client->SoftEmergencyStop(true, 2000); g_client->ReleaseControl(2000); }
  std::_Exit(130);
}

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout << "[CTRL] TakeControlAck: " << (a.error_code==0?"SUCCESS":"FAILURE")
              << " reason='" << a.reason << "'\n"; }
  void OnSwitchRemote() override { std::cout << "[CTRL] ✓ switched to REMOTE\n"; }
  void OnStandUp() override { std::cout << "[CTRL] robot acked StandUp\n"; }
  void OnSoftEmergencyStop(bool on) override {
    std::cout << "[CTRL] SoftEmergencyStop ack: " << (on?"ON":"OFF") << "\n"; }
};

class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    int m=(int)d.machine_status, p=(int)d.motion_status;
    if (m!=g_machine.load() || p!=g_motion.load()) {
      g_machine=m; g_motion=p;
      std::cout << "[STATE] machine=" << m << "(" << Machine(m) << ")"
                << "  motion=" << p << "(" << Motion(p) << ")"
                << "  estop sw=" << (int)d.software_emergency_status
                << " hw=" << (int)d.hardware_emergency_status << "\n";
    }
  }
  void OnFaultData(const FaultDatas& f) override {
    for (const auto& x : f)
      std::cout << "[FAULT] level=" << (int)x.level << " code=" << (int)x.code
                << " " << x.message << "\n";
  }
};

int main(int argc, char** argv) {
  if (argc < 3) { std::cerr << "usage: " << argv[0] << " <ip> <port>\n"; return 2; }
  std::string ip=argv[1], port=argv[2];
  std::signal(SIGINT, OnSig); std::signal(SIGTERM, OnSig);

  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout << "[stand2] connecting " << ip << ":" << port << " ...\n";
  auto ec = client.Connect(ip, port, false, [](const std::error_code& e){
    if (!e){ g_connected=true; std::cout << "[stand2] ✓ connected\n"; }
    else std::cerr << "[stand2] connect cb error: " << e.message() << "\n"; });
  if (ec) { std::cerr << "[stand2] Connect() failed: " << ec.message() << "\n"; return 1; }
  for (int i=0;i<15&&!g_connected;++i) std::this_thread::sleep_for(std::chrono::seconds(1));
  if (!g_connected){ std::cerr << "[stand2] connection timeout\n"; return 1; }

  std::cout << "[stand2] >>> TakeControl\n";
  ec = client.TakeControl(5000);
  std::cout << "[stand2] TakeControl(): " << (ec?ec.message():"ok") << "\n";
  if (ec) { client.Disconnect(true); return 1; }

  std::cout << "[stand2] 读初始状态 2s（期望 machine=IDLE, motion=LIE_DOWN）...\n";
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout << "[stand2] >>> SwitchRemoteState (IDLE→REMOTE，激活电机)\n";
  ec = client.SwitchRemoteState(3000);
  std::cout << "[stand2] SwitchRemoteState(): " << (ec?ec.message():"ok") << "\n";
  // 等切到 REMOTE，最多 5s
  for (int i=0;i<10 && g_machine.load()!=2; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(500));
  std::cout << "[stand2] 现在 machine=" << Machine(g_machine.load()) << "\n";

  std::cout << "[stand2] >>> StandUp（站起，不平移）\n";
  ec = client.StandUp(3000);
  std::cout << "[stand2] StandUp(): " << (ec?ec.message():"ok") << "\n";

  std::cout << "[stand2] 观察 8 秒，看是否变 STAND_UP...\n";
  std::this_thread::sleep_for(std::chrono::seconds(8));

  std::cout << "[stand2] 最终: machine=" << Machine(g_machine.load())
            << " motion=" << Motion(g_motion.load()) << "\n";
  std::cout << "[stand2] >>> ReleaseControl\n";
  client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout << "[stand2] done. 只做了 StandUp，未平移。\n";
  return 0;
}
