/**
 * walk1 —— 单会话：激活→站起→前进一小段→停，并测出实际走了多远（标定 Move）。
 * SDK 0.2.0。开机后机器在 IDLE，必须 SwitchRemoteState 激活电机后再动。
 *
 * 参数：argv[3]=前进秒数(默认0.5)  argv[4]=前进量(默认0.11，同官方例子 'w')
 * 流程：Connect → 开状态/速度上报 → TakeControl → SwitchRemoteState → StandUp
 *       → 稳定4s → 记录起点 → 以20Hz发Move前进N秒 → Move(0,0,0)停 → 稳定1.5s
 *       → 记录终点 → 打印位移/里程 → ReleaseControl → Disconnect
 * 只前进，不转向/不平移侧向。Ctrl+C 立即 SoftEmergencyStop(on)+Release。
 */
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <iostream>
#include <memory>
#include <thread>

#include "robot_sdk/sdk_client.hpp"

using namespace robot_sdk;
static std::atomic<bool> g_connected{false};
static std::atomic<int> g_machine{-1}, g_motion{-1};
static std::atomic<double> g_px{0}, g_py{0}, g_mile{0}, g_vx{0}, g_vmax{0};
static std::atomic<bool> g_have_pos{false};
static SDKClient* g_client = nullptr;

static const char* Machine(int s){switch(s){case 1:return"IDLE";case 2:return"REMOTE";default:return"?";}}
static const char* Motion(int s){switch(s){case 1:return"STAND_UP";case 2:return"LIE_DOWN";case 5:return"GENERAL";case 6:return"IN_PLACE";default:return"?";}}

static void OnSig(int){ if(g_client){ std::cout<<"\n[walk1] SIGINT → SoftEmergencyStop(on)+Release\n";
  g_client->SoftEmergencyStop(true,2000); g_client->ReleaseControl(2000);} std::_Exit(130); }

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n";}
  void OnSwitchRemote() override { std::cout<<"[CTRL] ✓ switched to REMOTE\n"; }
  void OnStandUp() override { std::cout<<"[CTRL] robot acked StandUp\n"; }
  void OnSoftEmergencyStop(bool on) override { std::cout<<"[CTRL] SoftEStop ack "<<(on?"ON":"OFF")<<"\n"; }
};
class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    int m=(int)d.machine_status,p=(int)d.motion_status;
    g_mile=d.mile_data;
    if(m!=g_machine.load()||p!=g_motion.load()){ g_machine=m; g_motion=p;
      std::cout<<"[STATE] machine="<<m<<"("<<Machine(m)<<") motion="<<p<<"("<<Motion(p)
               <<") mile="<<d.mile_data<<"m estop sw="<<(int)d.software_emergency_status<<"\n";}
  }
  void OnMcData(const MotionData& d) override {
    g_px=d.position[0]; g_py=d.position[1]; g_have_pos=true;
    g_vx=d.v_world[0];
    double sp=std::fabs(d.v_world[0]); if(sp>g_vmax.load()) g_vmax=sp;
  }
  void OnSpeedData(const SpeedData& s) override {
    std::cout<<"[SPEED] x="<<s.x<<" y="<<s.y<<" yaw="<<s.yaw<<" m/s\n";
  }
  void OnFaultData(const FaultDatas& f) override {
    for(const auto& x:f) std::cout<<"[FAULT] level="<<(int)x.level<<" code="<<(int)x.code<<" "<<x.message<<"\n"; }
};

int main(int argc,char**argv){
  if(argc<3){std::cerr<<"usage: "<<argv[0]<<" <ip> <port> [sec=0.5] [mag=0.11]\n";return 2;}
  std::string ip=argv[1],port=argv[2];
  double sec = (argc>3)?std::stod(argv[3]):0.5;
  double mag = (argc>4)?std::stod(argv[4]):0.11;
  if(sec>3.0) sec=3.0;                 // 硬上限，防手滑
  if(mag>0.15) mag=0.15;               // 前进量硬上限
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);

  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout<<"[walk1] connecting "<<ip<<":"<<port<<" ...\n";
  auto ec=client.Connect(ip,port,false,[](const std::error_code&e){
    if(!e){g_connected=true;std::cout<<"[walk1] ✓ connected\n";}
    else std::cerr<<"[walk1] connect cb error: "<<e.message()<<"\n";});
  if(ec){std::cerr<<"[walk1] Connect() failed: "<<ec.message()<<"\n";return 1;}
  for(int i=0;i<15&&!g_connected;++i) std::this_thread::sleep_for(std::chrono::seconds(1));
  if(!g_connected){std::cerr<<"[walk1] connection timeout\n";return 1;}

  std::cout<<"[walk1] 开启 运控/速度 上报\n";
  client.SetMcConfig(true,2000); client.SetSpeedReportConfig(true,20,2000);
  std::this_thread::sleep_for(std::chrono::seconds(1));

  std::cout<<"[walk1] >>> TakeControl\n";
  ec=client.TakeControl(5000); std::cout<<"[walk1] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  if(ec){client.Disconnect(true);return 1;}

  std::cout<<"[walk1] >>> SwitchRemoteState (激活电机)\n";
  client.SwitchRemoteState(3000);
  for(int i=0;i<10&&g_machine.load()!=2;++i) std::this_thread::sleep_for(std::chrono::milliseconds(500));
  std::cout<<"[walk1] machine="<<Machine(g_machine.load())<<"\n";

  std::cout<<"[walk1] >>> StandUp，稳定 6s（从趴姿站起要给足时间）\n";
  client.StandUp(3000);
  std::this_thread::sleep_for(std::chrono::seconds(6));

  double px0=g_px.load(), py0=g_py.load(), mile0=g_mile.load();
  bool havep=g_have_pos.load(); g_vmax=0;
  std::cout<<"[walk1] 起点 pos=("<<px0<<","<<py0<<") mile="<<mile0<<"m  (pos有效:"<<(havep?"是":"否")<<")\n";

  std::cout<<"[walk1] >>> Gait()（进入步态/行走模式，Move 需通用模式）\n";
  client.Gait(3000);
  std::this_thread::sleep_for(std::chrono::seconds(2));

  std::cout<<"[walk1] >>> 前进 "<<sec<<"s @ mag="<<mag<<"（20Hz 连发 Move(0,"<<mag<<",0)）\n";
  int ticks=(int)(sec*20.0+0.5);
  for(int i=0;i<ticks;++i){ client.Move(0.0f,(float)mag,0.0f); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }
  std::cout<<"[walk1] >>> 停 Move(0,0,0)\n";
  for(int i=0;i<6;++i){ client.Move(0.0f,0.0f,0.0f); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }

  std::this_thread::sleep_for(std::chrono::milliseconds(1500));
  double px1=g_px.load(), py1=g_py.load(), mile1=g_mile.load();
  double disp=std::sqrt((px1-px0)*(px1-px0)+(py1-py0)*(py1-py0));
  std::cout<<"[walk1] 终点 pos=("<<px1<<","<<py1<<") mile="<<mile1<<"m\n";
  std::cout<<"[walk1] === 位移(直线) ≈ "<<disp<<" m ;  里程增量 ≈ "<<(mile1-mile0)
           <<" m ;  峰值前向速度 ≈ "<<g_vmax.load()<<" m/s ===\n";
  std::cout<<"[walk1] （据此:走 1 米约需 "<<(disp>0.01? (sec*1.0/disp):-1)<<" s，仅供参考）\n";

  std::cout<<"[walk1] >>> ReleaseControl\n";
  client.ReleaseControl(3000);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  client.Disconnect(true);
  std::cout<<"[walk1] done.\n";
  return 0;
}
