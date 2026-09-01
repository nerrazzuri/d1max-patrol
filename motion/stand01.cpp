/**
 * stand01 —— 测 SDK 0.1.1 能否【自激活】把机器站起来（不碰遥控）。
 * 0.1.1 无 SwitchRemoteState；本程序穷尽 0.1.1 自己的路径：
 *   TakeControl → SetMode(1 通用) → StandUp   （必要时再试 Locked/解锁）
 * 0.1.1 遥测正常，打印 motion_status（趴/站）、电量、急停，直接看有没有真站起。
 * Ctrl+C 立即 SoftEmergencyStop(on)+Release。
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
static std::atomic<bool> g_connect_err{false};
static std::atomic<int> g_motion{-1};
static SDKClient* g_client=nullptr;
static const char* M(int s){switch(s){case 1:return"STAND_UP";case 2:return"LIE_DOWN";case 3:return"CRAWL";
  case 4:return"LOCKED";case 5:return"GENERAL";case 6:return"IN_PLACE";default:return"?";}}
static void OnSig(int){ if(g_client){ std::cout<<"\n[stand01] SIGINT → SoftEmergencyStop+Release\n";
  g_client->SoftEmergencyStop(true,2000); g_client->ReleaseControl(2000);} std::_Exit(130); }

class CtrlCb: public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n";}
  void OnMode(int m) override { std::cout<<"[CTRL] OnMode ack: "<<m<<"\n"; }
  void OnStandUp() override { std::cout<<"[CTRL] robot acked StandUp\n"; }
  void OnLocked() override { std::cout<<"[CTRL] robot acked Locked\n"; }
};
class DataCb: public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    int p=(int)d.motion_status;
    if(p!=g_motion.load()){ g_motion=p;
      std::cout<<"[STATE] motion="<<p<<"("<<M(p)<<")  estop sw="<<(int)d.software_emergency_status
               <<" hw="<<(int)d.hardware_emergency_status
               <<"  batt="<<d.battery.power1<<"%/"<<d.battery.power2<<"%"
               <<" chg="<<(int)d.battery.power_supply_status1<<"\n"; }
  }
  void OnFaultData(const FaultDatas& f) override {
    for(const auto& x:f) std::cout<<"[FAULT] level="<<(int)x.level<<" code="<<(int)x.code<<" "<<x.message<<"\n"; }
};

int main(int argc,char**argv){
  if(argc<3){std::cerr<<"usage: "<<argv[0]<<" <ip> <port>\n";return 2;}
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);
  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());
  std::cout<<"[stand01] connecting "<<argv[1]<<":"<<argv[2]<<" (SDK 0.1.1) ...\n";
  auto ec=client.Connect(argv[1],argv[2],false,[](const std::error_code&e){
    if(!e){g_connected=true;std::cout<<"[stand01] ✓ connected\n";}
    else {std::cerr<<"[stand01] connect err: "<<e.message()<<"\n"; g_connect_err=true;}});
  if(ec){std::cerr<<"[stand01] Connect() failed: "<<ec.message()<<"\n";return 1;}
  for(int i=0;i<15&&!g_connected&&!g_connect_err;++i) std::this_thread::sleep_for(std::chrono::milliseconds(300));
  if(!g_connected){std::cerr<<"[stand01] 未连上(秒退,便于重试)\n";return 1;}

  std::cout<<"[stand01] 开状态上报，读初始状态 2s\n";
  client.SetMcConfig(true,2000); client.SetSpeedReportConfig(true,20,2000);
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout<<"[stand01] >>> TakeControl\n";
  ec=client.TakeControl(5000); std::cout<<"[stand01] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  if(ec){client.Disconnect(true);return 1;}

  std::cout<<"[stand01] >>> SetMode(1) 通用模式\n";
  ec=client.SetMode(1,3000); std::cout<<"[stand01] SetMode(1): "<<(ec?ec.message():"ok")<<"\n";
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout<<"[stand01] >>> StandUp\n";
  ec=client.StandUp(3000); std::cout<<"[stand01] StandUp(): "<<(ec?ec.message():"ok")<<"\n";
  std::cout<<"[stand01] 观察 8s，看 motion 是否变 STAND_UP...\n";
  std::this_thread::sleep_for(std::chrono::seconds(8));

  std::cout<<"[stand01] 最终 motion="<<M(g_motion.load())<<"\n";
  std::cout<<"[stand01] >>> ReleaseControl\n";
  client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout<<"[stand01] done. （若 motion 仍 LIE_DOWN，则 0.1.1 自激活不成立）\n";
  return 0;
}
