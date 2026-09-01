/**
 * liedown —— 让 D1 Max 从站姿平稳趴下（准备充电/收工）。SDK 0.2.0。
 * Connect → TakeControl → SwitchRemoteState → LieDown → 等5s → ReleaseControl → Disconnect
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
static SDKClient* g_client=nullptr;
static void OnSig(int){ if(g_client){ std::cout<<"\n[liedown] SIGINT → SoftEmergencyStop+Release\n";
  g_client->SoftEmergencyStop(true,2000); g_client->ReleaseControl(2000);} std::_Exit(130); }
class CtrlCb: public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n";}
  void OnSwitchRemote() override { std::cout<<"[CTRL] ✓ REMOTE\n"; }
  void OnLieDown() override { std::cout<<"[CTRL] robot acked LieDown\n"; }
};
int main(int argc,char**argv){
  if(argc<3){std::cerr<<"usage: "<<argv[0]<<" <ip> <port>\n";return 2;}
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);
  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  std::cout<<"[liedown] connecting "<<argv[1]<<":"<<argv[2]<<" ...\n";
  auto ec=client.Connect(argv[1],argv[2],false,[](const std::error_code&e){
    if(!e){g_connected=true;std::cout<<"[liedown] ✓ connected\n";}
    else std::cerr<<"[liedown] connect err: "<<e.message()<<"\n";});
  if(ec){std::cerr<<"[liedown] Connect() failed: "<<ec.message()<<"\n";return 1;}
  for(int i=0;i<15&&!g_connected;++i) std::this_thread::sleep_for(std::chrono::seconds(1));
  if(!g_connected){std::cerr<<"[liedown] timeout\n";return 1;}
  std::cout<<"[liedown] >>> TakeControl\n"; ec=client.TakeControl(5000);
  std::cout<<"[liedown] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  if(ec){client.Disconnect(true);return 1;}
  std::cout<<"[liedown] >>> SwitchRemoteState\n"; client.SwitchRemoteState(3000);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  std::cout<<"[liedown] >>> LieDown（缓慢趴下）\n"; client.LieDown(3000);
  std::this_thread::sleep_for(std::chrono::seconds(5));
  std::cout<<"[liedown] >>> ReleaseControl\n"; client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout<<"[liedown] done. 已趴下，可以充电了。\n";
  return 0;
}
