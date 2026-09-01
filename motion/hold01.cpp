/**
 * hold01 —— 抢开机窗口连上 SDK 0.1.1，抢到后【常驻不释放】，
 * 持续打印机器状态（motion/电量/急停），把控制资源一直握住，让上装收不回去。
 * 连接失败(denial/shakehand/网络)就快速重试，直到抢到为止。
 * 抢到后：TakeControl → 开状态上报 → 每 ~2s 打印一次状态，永不主动释放。
 * Ctrl+C：ReleaseControl + Disconnect 退出。
 * 不发任何运动指令（只握会话+读状态）。
 */
#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include "robot_sdk/sdk_client.hpp"
using namespace robot_sdk;
static std::atomic<bool> g_connected{false};
static std::atomic<bool> g_connect_err{false};
static std::string g_last_err;
static std::atomic<int> g_motion{-1};
static SDKClient* g_client=nullptr;
static const char* M(int s){switch(s){case 1:return"STAND_UP";case 2:return"LIE_DOWN";case 3:return"CRAWL";
  case 4:return"LOCKED";case 5:return"GENERAL";case 6:return"IN_PLACE";default:return"?";}}
static void OnSig(int){ if(g_client){ std::cout<<"\n[hold01] 退出：ReleaseControl+Disconnect\n";
  g_client->ReleaseControl(2000); g_client->Disconnect(true);} std::_Exit(0); }

class CtrlCb: public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n";}
};
class DataCb: public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    int p=(int)d.motion_status;
    if(p!=g_motion.load()){ g_motion=p;
      std::cout<<"[STATE*] motion="<<p<<"("<<M(p)<<")  estop sw="<<(int)d.software_emergency_status
               <<" hw="<<(int)d.hardware_emergency_status
               <<"  batt="<<d.battery.power1<<"%/"<<d.battery.power2<<"%\n"; }
    last=d;
  }
  void OnFaultData(const FaultDatas& f) override {
    for(const auto& x:f) std::cout<<"[FAULT] level="<<(int)x.level<<" code="<<(int)x.code<<" "<<x.message<<"\n"; }
  RobotState last{};
  bool have=false;
};

int main(int argc,char**argv){
  if(argc<3){std::cerr<<"usage: "<<argv[0]<<" <ip> <port>\n";return 2;}
  std::string ip=argv[1],port=argv[2];
  std::cout<<std::unitbuf;                 // 每次输出立即刷新（后台管道下也实时可见）
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);
  SDKClient client; g_client=&client;
  auto ctrl=std::make_shared<CtrlCb>(); auto data=std::make_shared<DataCb>();
  client.SetControlCallback(ctrl); client.SetDataCallback(data);

  std::cout<<"[hold01] 开始抢连接 "<<ip<<":"<<port<<" ...\n";
  int attempt=0; auto t0=std::chrono::steady_clock::now();
  while(!g_connected.load()){
    attempt++;
    g_connect_err=false;
    client.Connect(ip,port,false,[](const std::error_code&e){
      if(!e){g_connected=true;}
      else{g_last_err=e.message(); g_connect_err=true;}});
    for(int i=0;i<8 && !g_connected.load() && !g_connect_err.load(); ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(250));
    if(!g_connected.load()){
      client.Disconnect(true);
      // 每 ~5s 报一次进度
      auto sec=std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now()-t0).count();
      if(attempt%10==0) std::cout<<"[hold01] 第"<<attempt<<"次仍未抢到("<<sec<<"s) 最近: "
                                 <<(g_last_err.empty()?"网络/无响应":g_last_err)<<"\n";
      std::this_thread::sleep_for(std::chrono::milliseconds(300));
    }
    if(attempt>2000){ std::cerr<<"[hold01] 试满仍未抢到，放弃\n"; return 1; }
  }
  std::cout<<"[hold01] ★★★ 抢到窗口，已连上！(第"<<attempt<<"次) ★★★\n";

  std::cout<<"[hold01] >>> TakeControl（握住控制权）\n";
  auto ec=client.TakeControl(5000);
  std::cout<<"[hold01] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  client.SetMcConfig(true,2000); client.SetSpeedReportConfig(true,20,2000); client.SetJointStateConfig(true,2000);

  std::cout<<"[hold01] 进入常驻：持续持有会话+读状态（不释放；Ctrl+C 退出）\n";
  int hb=0;
  while(true){
    std::this_thread::sleep_for(std::chrono::seconds(3));
    // 心跳：定期重申一次控制权，防被动超时（不发运动）
    if(++hb%5==0){
      auto e2=client.TakeControl(0);
      std::cout<<"[hold01] 心跳#"<<hb<<" 仍在持有 (motion="<<M(g_motion.load())
               <<", TakeControl重申:"<<(e2?e2.message():"ok")<<")\n";
    }
  }
  return 0;
}
