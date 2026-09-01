/**
 * hold02 —— 抢开机窗口连上 0.1.1 后【常驻不释放】，并开一个命令管道(FIFO)接收指令，
 * 在同一条会话里执行，握住控制权不让上装收回。0.1.1 遥测正常，持续打印状态。
 *
 * 用法: hold02 <ip> <port> <fifo路径>
 * 管道命令(一行一条)：
 *   stand              SetMode(1)+StandUp 站起
 *   walk <秒> [fwd=0.5]  Gait+Move 前进
 *   lie                LieDown 趴下
 *   estop / clear      软急停 开/关
 *   status             立即打印一次状态
 *   quit               ReleaseControl+Disconnect 退出
 * 不主动释放；SIGINT/quit 才释放。
 */
#include <atomic>
#include <chrono>
#include <csignal>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include "robot_sdk/sdk_client.hpp"
using namespace robot_sdk;
static std::atomic<bool> g_connected{false}, g_connect_err{false}, g_running{true};
static std::string g_last_err;
static std::atomic<int> g_motion{-1};
static std::atomic<double> g_b1{0},g_b2{0};
static SDKClient* g_client=nullptr;
static std::mutex g_mtx;
static const char* M(int s){switch(s){case 1:return"STAND_UP";case 2:return"LIE_DOWN";case 3:return"CRAWL";
  case 4:return"LOCKED";case 5:return"GENERAL";case 6:return"IN_PLACE";default:return"?";}}
static void OnSig(int){ if(g_client){ std::cout<<"\n[hold02] 退出：ReleaseControl+Disconnect\n"<<std::flush;
  g_client->ReleaseControl(2000); g_client->Disconnect(true);} std::_Exit(0); }

class CtrlCb: public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    std::cout<<"[CTRL] TakeControlAck: "<<(a.error_code==0?"SUCCESS":"FAILURE")<<" '"<<a.reason<<"'\n"<<std::flush;}
  void OnStandUp() override { std::cout<<"[CTRL] acked StandUp\n"<<std::flush; }
  void OnLieDown() override { std::cout<<"[CTRL] acked LieDown\n"<<std::flush; }
  void OnMode(int m) override { std::cout<<"[CTRL] acked Mode "<<m<<"\n"<<std::flush; }
};
class DataCb: public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    g_b1=d.battery.power1; g_b2=d.battery.power2;
    int p=(int)d.motion_status;
    if(p!=g_motion.load()){ g_motion=p;
      std::cout<<"[STATE*] motion="<<p<<"("<<M(p)<<")  estop sw="<<(int)d.software_emergency_status
               <<" hw="<<(int)d.hardware_emergency_status<<"  batt="<<d.battery.power1<<"%/"<<d.battery.power2<<"%\n"<<std::flush; }
  }
  void OnFaultData(const FaultDatas& f) override {
    for(const auto& x:f) std::cout<<"[FAULT] level="<<(int)x.level<<" code="<<(int)x.code<<" "<<x.message<<"\n"<<std::flush; }
};

static void handle(SDKClient& c, const std::string& line){
  std::istringstream is(line); std::string cmd; is>>cmd;
  if(cmd.empty()) return;
  std::lock_guard<std::mutex> lk(g_mtx);
  if(cmd=="stand"){
    std::cout<<"[CMD] stand: SetMode(1)+StandUp\n"<<std::flush;
    c.SetMode(1,2000); std::this_thread::sleep_for(std::chrono::milliseconds(500)); c.StandUp(3000);
  } else if(cmd=="walk"){
    double sec=1.0,fwd=0.5; is>>sec; if(!(is>>fwd)) fwd=0.5;
    if(sec>5)sec=5; if(fwd>0.5)fwd=0.5; if(fwd<-0.5)fwd=-0.5;
    std::cout<<"[CMD] walk sec="<<sec<<" fwd="<<fwd<<": Gait+Move\n"<<std::flush;
    c.Gait(2000); std::this_thread::sleep_for(std::chrono::milliseconds(500));
    int n=(int)(sec*20+0.5);
    for(int i=0;i<n;i++){ c.Move(0.0f,(float)fwd,0.0f); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }
    for(int i=0;i<6;i++){ c.Move(0,0,0); std::this_thread::sleep_for(std::chrono::milliseconds(50)); }
    std::cout<<"[CMD] walk done\n"<<std::flush;
  } else if(cmd=="lie"){ std::cout<<"[CMD] lie: LieDown\n"<<std::flush; c.LieDown(3000); }
  else if(cmd=="estop"){ std::cout<<"[CMD] SoftEmergencyStop ON\n"<<std::flush; c.SoftEmergencyStop(true,2000); }
  else if(cmd=="clear"){ std::cout<<"[CMD] SoftEmergencyStop OFF\n"<<std::flush; c.SoftEmergencyStop(false,2000); }
  else if(cmd=="status"){ std::cout<<"[CMD] status: motion="<<M(g_motion.load())<<" batt="<<g_b1.load()<<"%/"<<g_b2.load()<<"%\n"<<std::flush; }
  else if(cmd=="quit"){ std::cout<<"[CMD] quit\n"<<std::flush; c.ReleaseControl(2000); c.Disconnect(true); g_running=false; std::_Exit(0); }
  else std::cout<<"[CMD] 未知命令: "<<cmd<<"\n"<<std::flush;
}

int main(int argc,char**argv){
  if(argc<4){std::cerr<<"usage: "<<argv[0]<<" <ip> <port> <fifo>\n";return 2;}
  std::string ip=argv[1],port=argv[2],fifo=argv[3];
  std::cout<<std::unitbuf;
  std::signal(SIGINT,OnSig); std::signal(SIGTERM,OnSig);
  SDKClient client; g_client=&client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  std::cout<<"[hold02] 抢连 "<<ip<<":"<<port<<" ...\n";
  int attempt=0;
  while(!g_connected.load()){
    attempt++; g_connect_err=false;
    client.Connect(ip,port,false,[](const std::error_code&e){
      if(!e) g_connected=true; else {g_last_err=e.message(); g_connect_err=true;}});
    for(int i=0;i<8 && !g_connected.load() && !g_connect_err.load(); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(250));
    if(!g_connected.load()){ client.Disconnect(true);
      if(attempt%10==0) std::cout<<"[hold02] 第"<<attempt<<"次未抢到，最近:"<<(g_last_err.empty()?"网络":g_last_err)<<"\n";
      std::this_thread::sleep_for(std::chrono::milliseconds(300)); }
    if(attempt>2000){ std::cerr<<"[hold02] 放弃\n"; return 1; }
  }
  std::cout<<"[hold02] ★★★ 抢到窗口，已连上！(第"<<attempt<<"次) ★★★\n";
  auto ec=client.TakeControl(5000);
  std::cout<<"[hold02] TakeControl(): "<<(ec?ec.message():"ok")<<"\n";
  client.SetMcConfig(true,2000); client.SetSpeedReportConfig(true,20,2000); client.SetJointStateConfig(true,2000);

  // 命令管道线程
  std::thread cmdt([&client,fifo](){
    while(g_running.load()){
      std::ifstream f(fifo);           // 阻塞直到有写入者
      if(!f){ std::this_thread::sleep_for(std::chrono::milliseconds(200)); continue; }
      std::string line;
      while(std::getline(f,line)) handle(client,line);
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  });
  cmdt.detach();

  std::cout<<"[hold02] 常驻中：握住会话+读状态，等命令（fifo="<<fifo<<"）\n";
  int hb=0;
  while(g_running.load()){
    std::this_thread::sleep_for(std::chrono::seconds(3));
    if(++hb%5==0){ std::lock_guard<std::mutex> lk(g_mtx); client.TakeControl(0);
      std::cout<<"[hold02] 心跳#"<<hb<<" 持有中 motion="<<M(g_motion.load())<<" batt="<<g_b1.load()<<"%\n"; }
  }
  return 0;
}
