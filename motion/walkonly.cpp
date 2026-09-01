/**
 * walkonly —— 只负责“走动”。假设机器【已经站着】。
 * 不重启、不 StandUp（StandUp 会把模式重置回原地，反而走不动）。
 * 流程：Connect → 开上报 → TakeControl → SwitchRemoteState → Gait()
 *       → 以 20Hz 连发 Move(lat, fwd, yaw) 共 sec 秒 → Move(0,0,0) 停
 *       → ReleaseControl → Disconnect
 * 参数：ip port [sec=1.0] [fwd=0.11] [lat=0.0] [yaw=0.0]
 *   fwd>0 前进 / <0 后退；lat>0 左移 / <0 右移；yaw>0 左转 / <0 右转（同官方例子符号）
 * Move 只在“通用模式”下平移；本机在 0.2.0 下读不到模式，走不动多半是落在原地模式。
 * Ctrl+C 立即 SoftEmergencyStop(on)+ReleaseControl。
 */
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <thread>

#include "robot_sdk/sdk_client.hpp"

using namespace robot_sdk;
static std::atomic<bool> g_connected{false};
static SDKClient* g_client = nullptr;

static void OnSig(int){ if(g_client){ std::cout<<"\n[walkonly] SIGINT → SoftEmergencyStop(on)+Release\n";
  g_client->SoftEmergencyStop(true,2000); g_client->ReleaseControl(2000);} std::_Exit(130); }

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n";}
  void OnSwitchRemote() override { std::cout<<"[CTRL] ✓ switched to REMOTE\n"; }
  void OnGait() override { std::cout<<"[CTRL] ✓ Gait acked\n"; }
  void OnSoftEmergencyStop(bool on) override { std::cout<<"[CTRL] SoftEStop "<<(on?"ON":"OFF")<<"\n"; }
};
class DataCb : public IDataCallback {
 public:
  void OnSpeedData(const SpeedData& s) override {
    std::cout<<"[SPEED] x="<<s.x<<" y="<<s.y<<" yaw="<<s.yaw<<"\n"; }
  void OnFaultData(const FaultDatas& f) override {
    for(const auto& x:f) std::cout<<"[FAULT] level="<<(int)x.level<<" code="<<(int)x.code<<" "<<x.message<<"\n"; }
};

int main(int argc,char**argv){
  if(argc<3){std::cerr<<"usage: "<<argv[0]<<" <ip> <port> [sec=1.0] [fwd=0.11] [lat=0.0] [yaw=0.0]\n";return 2;}
  std::string ip=argv[1],port=argv[2];
  double sec=(argc>3)?std::atof(argv[3]):1.0;
  double fwd=(argc>4)?std::atof(argv[4]):0.5;   // 0.11 太慢几乎不动，0.5 才明显走（同官方例子）
  double lat=(argc>5)?std::atof(argv[5]):0.0;
  double yaw=(argc>6)?std::atof(argv[6]):0.0;
  if(sec>5.0)sec=5.0;                                  // 时长硬上限
  auto cap=[](double v,double m){return v> m?m:(v<-m?-m:v);};
  fwd=cap(fwd,0.5); lat=cap(lat,0.5); yaw=cap(yaw,0.4); // 量级硬上限
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);

  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout<<"[walkonly] connecting "<<ip<<":"<<port<<" ...\n";
  auto ec=client.Connect(ip,port,false,[](const std::error_code&e){
    if(!e){g_connected=true;std::cout<<"[walkonly] ✓ connected\n";}
    else std::cerr<<"[walkonly] connect cb error: "<<e.message()<<"\n";});
  if(ec){std::cerr<<"[walkonly] Connect() failed: "<<ec.message()<<"\n";return 1;}
  for(int i=0;i<15&&!g_connected;++i) std::this_thread::sleep_for(std::chrono::seconds(1));
  if(!g_connected){std::cerr<<"[walkonly] connection timeout\n";return 1;}

  client.SetSpeedReportConfig(true,20,2000);           // 打开速度上报（0.1.1能出，0.2.0未必）

  std::cout<<"[walkonly] >>> TakeControl\n";
  ec=client.TakeControl(5000); std::cout<<"[walkonly] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  if(ec){client.Disconnect(true);return 1;}

  std::cout<<"[walkonly] >>> SwitchRemoteState\n";
  client.SwitchRemoteState(3000);
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout<<"[walkonly] >>> Gait()（进入步态/通用行走模式）\n";
  client.Gait(3000);
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout<<"[walkonly] >>> Move("<<lat<<","<<fwd<<","<<yaw<<") 连发 "<<sec<<"s（20Hz）\n";
  int ticks=(int)(sec*20.0+0.5);
  for(int i=0;i<ticks;++i){ client.Move((float)lat,(float)fwd,(float)yaw); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }
  std::cout<<"[walkonly] >>> 停 Move(0,0,0)\n";
  for(int i=0;i<6;++i){ client.Move(0,0,0); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }

  std::this_thread::sleep_for(std::chrono::milliseconds(800));
  std::cout<<"[walkonly] >>> ReleaseControl\n";
  client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout<<"[walkonly] done.\n";
  return 0;
}
