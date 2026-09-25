/**
 * patrol_agent —— 常驻运控旁路进程。hold02 的产品化版本。
 *
 * ============================ 为什么需要它 ============================
 *
 * 2026-09-01 现场实测(docs/真机待验证清单.md #46/#47)得出一条硬约束：
 *
 *     SDK 资源(控制**和**数据读取)是独占的，开机后默认被上装占住。
 *     目前唯一已验证的进入方式是"抢开机窗口"——在上装占住之前连上去。
 *     一旦释放，上装立刻收回，连只读都再也进不去，只能重启 RK3588。
 *
 * 所以这条 SDK 会话必须**先于巡检程序启动、比它活得久**。Python 主体会
 * 重启、会崩、会被 Ctrl+C——那些都不能把会话带走。于是：
 *
 *     patrol_agent(常驻，握着 SDK)  ←TCP+JSONL→  Python 巡检程序(随连随断)
 *
 * 线协议见 src/d1max_patrol/protocol/agent_frames.py，
 * 可执行契约见 src/d1max_sim/agent_server.py（改这个文件前先看那两个）。
 *
 * ============================== 用法 ==============================
 *
 *   patrol_agent <ip> <port> [--listen HOST:PORT] [--fifo PATH]
 *
 *   patrol_agent 192.168.168.168 8082 --listen 127.0.0.1:8090 --fifo /tmp/d1max.cmd
 *
 * **先起它，再给机器上电。** 它会一直刷"第 N 次未抢到"，直到抢到窗口。
 * 端口是 8082/UDP，不是文档里写的 8081（清单 #34）。
 *
 * FIFO 是给人用的应急通道，收的是 hold02 那套空格分隔的命令；TCP 那条
 * 才是给 Python 用的。两条走同一把锁、同一条会话。
 *
 * **它不会自己 ReleaseControl。** SIGINT 也不会——那正是要防的事。真要
 * 交还控制权，从 TCP 发 {"cmd":"shutdown"}，或者 FIFO 里敲 shutdown。
 *
 * 编译： cd motion && make patrol_agent
 */
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "robot_sdk/sdk_client.hpp"

using namespace robot_sdk;

// 与 agent_frames.PROTO_VERSION 一致。两边对不上时 Python 拒绝连接——
// 这是唯一挡得住"新旁路进程配旧巡检程序"的东西，改了协议就必须一起加。
//
// 2:加了 halt,并且 halt/estop 改成**插队**执行(见 ServeClient)。旧的 1 号
// 旁路进程会把 halt 回成「不认识的命令」、把急停排在几拍 walk 后面 ——
// 新巡检程序配旧旁路进程时,停车会**无声地**不灵,所以必须在握手时就拒绝。
//
// 3(W00d):加了 vel —— 带有效期的持续速度。立刻回执;速度线程在有效期内每 50ms 发一次
// Move,到期连发零速自停;新的 vel 覆盖旧的并续期;halt/estop 作废。机器人代理的速度环
// (每拍一条、ttl 300ms)就靠它。2 号旁路进程不认识 vel,握手时就得拒。
static const int kProtoVersion = 3;

// 一次 Move 在机器上维持约 1s(清单 #38)，靠 50ms 连续下发维持行走。
static const int kMoveIntervalMs = 50;
// 安全阀，不是调参项：现场旁边站着人。与 Python 侧 MAX_WALK_* 对齐。
static const double kMaxWalkSeconds = 10.0;
static const double kMaxWalkSpeed = 0.5;
// vel 的有效期上下限:短于一拍没意义,长于 1s 断链时狗会多走一截。
static const int kVelTtlMinMs = 50;
static const int kVelTtlMaxMs = 1000;
// 里程 50Hz 太密，降到 20Hz 再往外发，省得把链路和日志都灌满。
static const int64_t kOdomMinIntervalMs = 50;

// ===================================================================
// 一点点 JSON —— 刻意不引第三方库：现场机器上装不了包，编译环境只有 g++。
// 编码这头必须正确(Python 那边是严格解析的)；解码这头只认我们自己发的
// 那几种形状(见 encode_command)，不追求通用。
// ===================================================================

static std::string JsonEscape(const std::string& s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

static std::string JsonNum(double v) {
  // NaN/Inf 不是合法 JSON，会让 Python 那头整帧报错。传感器给出脏值时
  // 宁可发 0 —— 丢一帧的精度，好过丢一条链路。
  if (!std::isfinite(v)) return "0";
  char buf[40];
  std::snprintf(buf, sizeof(buf), "%.6g", v);
  return buf;
}

/// 从我们自己发的那种平坦对象里取一个字段。找不到返回 false。
static bool JsonField(const std::string& line, const std::string& key,
                      std::string* out) {
  const std::string needle = "\"" + key + "\"";
  size_t at = line.find(needle);
  if (at == std::string::npos) return false;
  at = line.find(':', at + needle.size());
  if (at == std::string::npos) return false;
  ++at;
  while (at < line.size() && std::isspace(static_cast<unsigned char>(line[at]))) ++at;
  if (at >= line.size()) return false;
  if (line[at] == '"') {
    ++at;
    std::string value;
    while (at < line.size() && line[at] != '"') {
      if (line[at] == '\\' && at + 1 < line.size()) ++at;
      value += line[at++];
    }
    *out = value;
    return true;
  }
  size_t end = at;
  while (end < line.size() && line[end] != ',' && line[end] != '}') ++end;
  std::string value = line.substr(at, end - at);
  while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back())))
    value.pop_back();
  if (value.empty()) return false;
  *out = value;
  return true;
}

static double JsonNumField(const std::string& line, const std::string& key,
                           double fallback) {
  std::string raw;
  if (!JsonField(line, key, &raw)) return fallback;
  try {
    return std::stod(raw);
  } catch (...) {
    return fallback;
  }
}

static bool JsonBoolField(const std::string& line, const std::string& key,
                          bool fallback) {
  std::string raw;
  if (!JsonField(line, key, &raw)) return fallback;
  return raw == "true" || raw == "1";
}

static int64_t NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// ===================================================================
// 客户端集合
// ===================================================================

static std::mutex g_clients_mtx;
static std::vector<int> g_clients;
// 写 socket 串行化。命令回执(读线程/工作线程)和遥测广播(SDK 回调线程)
// 会同时往同一个 fd 写，不锁的话两行字节会交错，Python 那头整帧解不开。
static std::mutex g_send_mtx;

static void SendTo(int fd, const std::string& obj) {
  std::string line = obj + "\n";
  std::lock_guard<std::mutex> lk(g_send_mtx);
  size_t sent = 0;
  while (sent < line.size()) {
    // MSG_NOSIGNAL：客户端半路死掉时不要把 SIGPIPE 打到整个进程上——
    // 那会把还握着控制权的会话一起带走。
    ssize_t n = ::send(fd, line.data() + sent, line.size() - sent, MSG_NOSIGNAL);
    if (n <= 0) return;
    sent += static_cast<size_t>(n);
  }
}

static void Broadcast(const std::string& obj) {
  std::lock_guard<std::mutex> lk(g_clients_mtx);
  for (int fd : g_clients) SendTo(fd, obj);
}

// ===================================================================
// 机身状态缓存 —— 新客户端一连上就能立刻拿到一帧，不用干等下一次变化
// ===================================================================

static std::mutex g_state_mtx;
static int g_motion = 0;
static double g_batt1 = 0.0, g_batt2 = 0.0;
static int g_estop_sw = 0, g_estop_hw = 0;
static std::atomic<bool> g_held{false};
static std::string g_robot_addr;
static SDKClient* g_client = nullptr;
static std::mutex g_sdk_mtx;   // 所有 SDK 调用串行化
static std::atomic<bool> g_running{true};
static std::atomic<int64_t> g_last_odom_ms{0};
// 停车代数。halt 和 estop(on) 把它加一；每条 walk 开始时记下当时的代数，
// 走的过程中每一拍都看一眼 —— 代数变了就是「有人叫停了」，立刻收手。
// 排在队里还没轮到的 walk 也靠它作废：它们记下的代数早于那次叫停。
static std::atomic<uint64_t> g_cancel_gen{0};

// vel 的当前目标(比例值)、到期时刻、下达时的停车代数。速度线程每拍读一次。
struct VelTarget {
  double fwd = 0, lat = 0, yaw = 0;
  std::chrono::steady_clock::time_point until{};
  uint64_t gen = 0;
  bool set = false;
};
static std::mutex g_vel_mtx;
static VelTarget g_vel;

static std::string StateFrame() {
  std::lock_guard<std::mutex> lk(g_state_mtx);
  std::ostringstream os;
  os << "{\"t\":\"state\",\"motion\":" << g_motion
     << ",\"battery1\":" << JsonNum(g_batt1)
     << ",\"battery2\":" << JsonNum(g_batt2)
     << ",\"estop_sw\":" << g_estop_sw
     << ",\"estop_hw\":" << g_estop_hw
     << ",\"ts_ms\":" << NowMs() << "}";
  return os.str();
}

// ===================================================================
// SDK 回调
// ===================================================================

class DataCb : public IDataCallback {
 public:
  void OnRobotStateData(const RobotState& d) override {
    {
      std::lock_guard<std::mutex> lk(g_state_mtx);
      g_motion = static_cast<int>(d.motion_status);
      // 不在位的电池 power 读数没意义，直接报 0，让 Python 那头的
      // "取两块里低的那块"跳过它(StateFrame.battery 只看 >0 的)。
      g_batt1 = d.battery.present1 ? d.battery.power1 : 0.0f;
      g_batt2 = d.battery.present2 ? d.battery.power2 : 0.0f;
      g_estop_sw = static_cast<int>(d.software_emergency_status);
      g_estop_hw = static_cast<int>(d.hardware_emergency_status);
    }
    Broadcast(StateFrame());
  }

  void OnMcData(const MotionData& d) override {
    int64_t now = NowMs();
    if (now - g_last_odom_ms.load() < kOdomMinIntervalMs) return;
    g_last_odom_ms = now;
    // quat 是 [w, x, y, z]（头文件明写的顺序，别照抄 ImuData 那套 xyzw）。
    const double w = d.quat[0], x = d.quat[1], y = d.quat[2], z = d.quat[3];
    const double yaw = std::atan2(2.0 * (w * z + x * y),
                                  1.0 - 2.0 * (y * y + z * z));
    std::ostringstream os;
    os << "{\"t\":\"odom\",\"x\":" << JsonNum(d.position[0])
       << ",\"y\":" << JsonNum(d.position[1])
       << ",\"yaw\":" << JsonNum(yaw)
       << ",\"vx\":" << JsonNum(d.v_body[0])
       << ",\"vy\":" << JsonNum(d.v_body[1])
       << ",\"vyaw\":" << JsonNum(d.omega_body[2])
       << ",\"ts_ms\":" << static_cast<long long>(d.time_stamp / 1000000ULL)
       << "}";
    Broadcast(os.str());
  }

  void OnFaultData(const FaultDatas& f) override {
    for (const auto& x : f) {
      std::ostringstream os;
      os << "{\"t\":\"fault\",\"level\":" << static_cast<int>(x.level)
         << ",\"code\":" << static_cast<int>(x.code)
         << ",\"message\":\"" << JsonEscape(x.message) << "\"}";
      Broadcast(os.str());
      std::cout << "[FAULT] level=" << static_cast<int>(x.level)
                << " code=" << static_cast<int>(x.code) << " " << x.message
                << "\n";
    }
  }

  void OnControlLost(const ControlLostInfo&) override {
    // ControlLostInfo 是空结构体 —— 厂商没给原因字段，只能自己凑一句。
    g_held = false;
    std::cout << "[!!] 控制权丢了。多半是上装收回去了(清单 #47)，"
                 "这条会话大概率救不回来，要重启 RK3588 重抢开机窗口。\n";
    Broadcast("{\"t\":\"control_lost\",\"reason\":"
              "\"SDK OnControlLost(厂商未给原因)\"}");
  }

  void OnControlAvailable(const ControlAvailableInfo&) override {
    std::cout << "[i] SDK 报告控制权可用，尝试重新 TakeControl\n";
    std::lock_guard<std::mutex> lk(g_sdk_mtx);
    if (g_client && !g_client->TakeControl(5000)) g_held = true;
  }
};

class CtrlCb : public IControlCallback {
 public:
  void OnTakeControlAck(const TakeControlAck& a) override {
    g_held = (a.error_code == 0);
    std::cout << "[CTRL] TakeControlAck: "
              << (a.error_code == 0 ? "SUCCESS" : "FAILURE") << " '" << a.reason
              << "'\n";
  }
  void OnStandUp() override { std::cout << "[CTRL] acked StandUp\n"; }
  void OnLieDown() override { std::cout << "[CTRL] acked LieDown\n"; }
  void OnMode(int m) override { std::cout << "[CTRL] acked Mode " << m << "\n"; }
  void OnGait() override { std::cout << "[CTRL] acked Gait\n"; }
};

// ===================================================================
// 命令执行 —— TCP 和 FIFO 共用同一条实现、同一把 SDK 锁
// ===================================================================

struct Outcome {
  bool ok = true;
  std::string error;
};

static Outcome Reject(const std::string& why) { return Outcome{false, why}; }

/// 没握着控制权时的统一拒绝话术。原文照抄真机打出来的那句(#40/#47)，
/// 好让现场对着日志能一眼认出是同一件事。
static Outcome DenyControl(const std::string& what) {
  return Reject("Controlled denial of service (" + what + ")");
}

static Outcome DoStand() {
  if (!g_held.load()) return DenyControl("stand");
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  // 只 StandUp 不 SetMode 是站不起来的(清单 #45)。这个时序封在这里，
  // Python 那头不重复。
  g_client->SetMode(1, 2000);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  g_client->StandUp(3000);
  return Outcome{};
}

static Outcome DoLie() {
  if (!g_held.load()) return DenyControl("lie");
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  g_client->LieDown(3000);
  return Outcome{};
}

// 「这一拍被叫停了」的统一话术。Python 那头把它当普通拒绝处理。
static const char* kWalkCancelled = "行走被停车/急停打断";

/// 走一拍。stop() 返回 true 表示有人叫停(停车代数变了，或者发它的客户端
/// 已经断开)，这时立刻发一次零速并返回 —— **后续的停法由叫停的那一方负责**
/// (halt 连发零速，estop 走 SoftEmergencyStop)，这里不再自己补 6 次零速，
/// 免得多占 300ms 的 SDK 锁、让急停在锁外面干等。
///
/// 已知的上限：Gait(2000) 本身是阻塞调用，叫停正好落在它里面时要等它回来。
static Outcome DoWalk(double seconds, double fwd, double lat, double yaw,
                      const std::function<bool()>& stop) {
  if (!g_held.load()) return DenyControl("walk");
  if (!(seconds > 0.0) || seconds > kMaxWalkSeconds)
    return Reject("行走时长要在 (0, 10] 秒内");
  if (!std::isfinite(fwd) || !std::isfinite(lat) || !std::isfinite(yaw) ||
      std::fabs(fwd) > kMaxWalkSpeed || std::fabs(lat) > kMaxWalkSpeed ||
      std::fabs(yaw) > kMaxWalkSpeed)
    return Reject("速度分量要在 ±0.5 内");

  if (stop()) return Reject(kWalkCancelled);
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  if (stop()) return Reject(kWalkCancelled);
  g_client->Gait(2000);
  // 起步那 500ms 也切成小段看叫停，不然急停最坏要多等半秒。
  for (int i = 0; i < 500 / kMoveIntervalMs; ++i) {
    if (stop()) {
      g_client->Move(0, 0, 0);
      return Reject(kWalkCancelled);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
  }
  const int ticks = static_cast<int>(seconds * 1000.0 / kMoveIntervalMs + 0.5);
  for (int i = 0; i < ticks && g_running.load(); ++i) {
    if (stop()) {
      g_client->Move(0, 0, 0);
      return Reject(kWalkCancelled);
    }
    // 走到一半控制权被收走就立刻停 —— 不然剩下的 Move 全打空，
    // 而机器可能还在按最后一条指令滑行。
    if (!g_held.load()) break;
    g_client->Move(static_cast<float>(lat), static_cast<float>(fwd),
                   static_cast<float>(yaw));
    std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
  }
  // 收尾多发几次零速：丢一两包也还是能停下来。
  for (int i = 0; i < 6; ++i) {
    g_client->Move(0, 0, 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
  }
  return Outcome{};
}

// SDK 枚举码(sdk_type.hpp 的声明顺序,同 agent_frames.py 的 MOTION_BY_CODE / EMERGENCY_BY_CODE)。
static const int kMotionUnknown = 0, kMotionLieDown = 2, kMotionLocked = 4;
static const int kEstopStop = 2;

/// 持续速度(协议 v3)。**立刻回执**:真正发 Move 的是 VelLoop。
///
/// **要控制权**(同 walk)。比例值与 walk 同一个上限 ±0.5;有效期 [50, 1000] ms。
/// 趴着/锁死/姿态未知、任一路急停生效都拒 —— 急停期间收下的速度不许在急停解除后生效。
static Outcome DoVel(double fwd, double lat, double yaw, double ttl_ms) {
  if (!g_held.load()) return DenyControl("vel");
  {
    std::lock_guard<std::mutex> lk(g_state_mtx);
    if (g_estop_sw == kEstopStop || g_estop_hw == kEstopStop)
      return Reject("急停生效中,拒绝动作");
    if (g_motion == kMotionUnknown || g_motion == kMotionLieDown || g_motion == kMotionLocked)
      return Reject("趴着/锁死/姿态未知,走不了,先 stand");
  }
  if (!std::isfinite(fwd) || !std::isfinite(lat) || !std::isfinite(yaw) ||
      std::fabs(fwd) > kMaxWalkSpeed || std::fabs(lat) > kMaxWalkSpeed ||
      std::fabs(yaw) > kMaxWalkSpeed)
    return Reject("速度分量要在 ±0.5 内");
  if (!std::isfinite(ttl_ms) || ttl_ms < kVelTtlMinMs || ttl_ms > kVelTtlMaxMs)
    return Reject("ttl_ms 要在 [50, 1000] 内");
  std::lock_guard<std::mutex> lk(g_vel_mtx);
  g_vel.fwd = fwd;
  g_vel.lat = lat;
  g_vel.yaw = yaw;
  g_vel.until = std::chrono::steady_clock::now() +
                std::chrono::milliseconds(static_cast<int>(ttl_ms));
  g_vel.gen = g_cancel_gen.load();
  g_vel.set = true;
  return Outcome{};
}

/// 速度线程:有效期内、代数没变、还握着控制权 → 每 50ms 发一次 Move;从静止起步先 Gait
/// 一次;过期、被叫停或控制权丢了 → 连发零速一次性收住,然后等下一条 vel。
///
/// 「还该不该走」在拿到 SDK 锁之后、Gait 回来之后**各再看一次**:等锁(可能排在一整段 walk
/// 后面)或 Gait(2000) 阻塞期间有人 halt/estop、或者有效期过了,就不再发那条过期的 Move。
/// 已知上限(同 walk):Gait(2000) 是 SDK 的阻塞调用,halt/estop 正好落在里面时要等它回来。
static void VelLoop() {
  bool moving = false;
  auto live_now = [](const VelTarget& t) {
    return t.set && std::chrono::steady_clock::now() < t.until &&
           g_cancel_gen.load() == t.gen && g_held.load();
  };
  while (g_running.load()) {
    VelTarget t;
    {
      std::lock_guard<std::mutex> lk(g_vel_mtx);
      t = g_vel;
    }
    if (live_now(t)) {
      std::lock_guard<std::mutex> lk(g_sdk_mtx);
      {
        std::lock_guard<std::mutex> vk(g_vel_mtx);
        t = g_vel;                       // 等锁期间可能来了新的 vel(续期或改速度)
      }
      if (live_now(t) && !moving) {
        if (g_client->Gait(2000)) {
          std::cerr << "[VEL] Gait 失败,这一拍不发 Move\n";
        } else {
          moving = true;
        }
      }
      if (moving && live_now(t)) {
        g_client->Move(static_cast<float>(t.lat), static_cast<float>(t.fwd),
                       static_cast<float>(t.yaw));
      } else if (moving) {
        g_client->Move(0, 0, 0);          // 这一拍里被叫停/过期了:零速,下一拍走收尾
      }
    } else if (moving) {
      {
        std::lock_guard<std::mutex> lk(g_sdk_mtx);
        for (int i = 0; i < 3; ++i) {
          g_client->Move(0, 0, 0);
          std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
        }
      }
      moving = false;
      // 只清掉「就是这条」过期/被叫停的目标:收尾那 150ms 里来的新 vel 已经回执成功,不能吞掉。
      std::lock_guard<std::mutex> lk(g_vel_mtx);
      if (g_vel.until == t.until && g_vel.gen == t.gen) g_vel.set = false;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
  }
}

/// 停车：作废正在走和排着队的 walk，再连发几次零速。
///
/// **不要控制权。** 没握着控制权时本来也发不出 Move，而这时也不会有 walk
/// 在走(DoWalk 进门就要控制权)，所以只作废队列、回 ok。
static Outcome DoHalt() {
  g_cancel_gen.fetch_add(1);
  if (!g_held.load()) return Outcome{};
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  // 多发几次：丢一两包也还是能停下来。
  for (int i = 0; i < 3; ++i) {
    g_client->Move(0, 0, 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(kMoveIntervalMs));
  }
  return Outcome{};
}

static Outcome DoEstop(bool on) {
  // 急停不要控制权就能发 —— 它是安全动作，任何时候都得能出去。
  // 按下时先作废所有 walk：正在走的那一拍会在 50ms 内松开 SDK 锁。
  if (on) g_cancel_gen.fetch_add(1);
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  // 返回值必须看。以前丢掉了，SDK 没发出去也回 ok，页面上写着「已急停」。
  auto ec = g_client->SoftEmergencyStop(on, 2000);
  if (ec) return Reject("SoftEmergencyStop 失败: " + ec.message());
  return Outcome{};
}

static Outcome DoLight(const std::string& which, bool on) {
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  if (which == "front") {
    g_client->FrontLight(on);
  } else if (which == "back") {
    g_client->BackLight(on);
  } else if (which == "both") {
    g_client->FrontLight(on);
    g_client->BackLight(on);
  } else {
    return Reject("未知的灯位: " + which);
  }
  return Outcome{};
}

static Outcome DoHead(double yaw, double pitch) {
  if (!g_held.load()) return DenyControl("head");
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  // 假设(待真机验证)：yaw → left_right、pitch → up_down，单位厂商没写。
  // 真机上第一件事是发一个小角度看它往哪转、转多少。
  g_client->ControlHead(static_cast<float>(yaw), static_cast<float>(pitch));
  return Outcome{};
}

static Outcome DoHold() {
  std::lock_guard<std::mutex> lk(g_sdk_mtx);
  auto ec = g_client->TakeControl(5000);
  if (ec) return DenyControl(ec.message());
  g_held = true;
  return Outcome{};
}

/// **不可逆。** 执行完要重启 RK3588 才能再抢一次控制权(清单 #47)。
static Outcome DoShutdown() {
  std::cout << "[!!] 收到 shutdown：ReleaseControl + Disconnect。"
               "控制权会被上装收回，今天不重启 RK3588 就再也进不来了。\n";
  Broadcast("{\"t\":\"control_lost\",\"reason\":\"旁路进程按要求退出\"}");
  {
    std::lock_guard<std::mutex> lk(g_sdk_mtx);
    g_client->ReleaseControl(2000);
    g_client->Disconnect(true);
  }
  g_held = false;
  g_running = false;
  return Outcome{};
}

/// 执行一条 JSONL 命令。cmd 已经解出来了，其余字段现从 line 里取。
/// stop 只给 walk 用，见 DoWalk。
static Outcome RunCommand(const std::string& cmd, const std::string& line,
                          const std::function<bool()>& stop) {
  if (cmd == "hold") return DoHold();
  if (cmd == "shutdown") return DoShutdown();
  if (cmd == "halt") return DoHalt();
  if (cmd == "estop") return DoEstop(JsonBoolField(line, "on", true));
  if (cmd == "light") {
    std::string which = "both";
    JsonField(line, "which", &which);
    return DoLight(which, JsonBoolField(line, "on", true));
  }
  if (cmd == "stand") return DoStand();
  if (cmd == "lie") return DoLie();
  if (cmd == "head")
    return DoHead(JsonNumField(line, "yaw", 0.0), JsonNumField(line, "pitch", 0.0));
  if (cmd == "vel")
    return DoVel(JsonNumField(line, "fwd", 0.0), JsonNumField(line, "lat", 0.0),
                 JsonNumField(line, "yaw", 0.0), JsonNumField(line, "ttl_ms", 0.0));
  if (cmd == "walk")
    return DoWalk(JsonNumField(line, "seconds", 0.0),
                  JsonNumField(line, "fwd", 0.0),
                  JsonNumField(line, "lat", 0.0),
                  JsonNumField(line, "yaw", 0.0), stop);
  return Reject("不认识的命令: " + cmd);
}

// ===================================================================
// TCP 服务
// ===================================================================

/// 插队命令：读线程当场执行，不进队列。
///
/// **为什么必须插队。** 以前一条连接上的命令是读一行、做完、回执、再读下一
/// 行。手机每 300ms 发一拍，一拍在这里要占 Gait + 500ms 起步 + 走 + 收尾，
/// 于是 walk 在 socket 里越积越多 —— 人按急停的时候，急停排在好几拍 walk
/// 后面，要等它们一拍一拍走完才轮得到。
static bool IsUrgent(const std::string& cmd) {
  // vel 也插队:它立刻回执,排在一拍 walk 后面的话速度环就断了。
  return cmd == "halt" || cmd == "estop" || cmd == "vel";
}

static std::string AckFrame(int id, const Outcome& out) {
  std::ostringstream ack;
  ack << "{\"t\":\"ack\",\"id\":" << id << ",\"ok\":"
      << (out.ok ? "true" : "false");
  if (!out.ok) ack << ",\"error\":\"" << JsonEscape(out.error) << "\"";
  ack << "}";
  return ack.str();
}

struct Job {
  int id = 0;
  std::string cmd;
  std::string line;
  uint64_t gen = 0;  // 入队时的停车代数
};

struct Conn {
  int fd = -1;
  std::mutex mtx;
  std::condition_variable cv;
  std::deque<Job> queue;
  std::atomic<bool> closing{false};
};

/// 工作线程：按顺序执行普通命令。一次只做一条 —— 跟以前的串行语义一样，
/// 只是急停/停车不再排在它后面。
static void ConnWorker(Conn* conn) {
  while (true) {
    Job job;
    {
      std::unique_lock<std::mutex> lk(conn->mtx);
      conn->cv.wait(lk, [conn] {
        return conn->closing.load() || !conn->queue.empty();
      });
      if (conn->closing.load()) return;  // 客户端走了，队里剩下的一律不做
      job = std::move(conn->queue.front());
      conn->queue.pop_front();
    }
    const uint64_t gen = job.gen;
    // 发它的客户端断开也算叫停：没人等这一拍的回执了，也没人看着狗了。
    auto stop = [conn, gen] {
      return g_cancel_gen.load() != gen || conn->closing.load();
    };
    Outcome out;
    if (job.cmd == "walk" && stop()) {
      out = Reject(kWalkCancelled);  // 排队的时候已经被叫停了
    } else {
      out = RunCommand(job.cmd, job.line, stop);
    }
    SendTo(conn->fd, AckFrame(job.id, out));
  }
}

static void ServeClient(int fd) {
  {
    std::lock_guard<std::mutex> lk(g_clients_mtx);
    g_clients.push_back(fd);
  }
  std::ostringstream hello;
  hello << "{\"t\":\"hello\",\"proto\":" << kProtoVersion
        << ",\"sdk\":\"0.1.1\",\"held\":" << (g_held.load() ? "true" : "false")
        << ",\"robot\":\"" << JsonEscape(g_robot_addr) << "\"}";
  SendTo(fd, hello.str());
  // 立刻补一帧状态：客户端刚连上就该能读到电量和运动状态，不必干等到
  // 下一次状态变化 —— OnRobotStateData 只在变化时来。
  SendTo(fd, StateFrame());

  Conn conn;
  conn.fd = fd;
  std::thread worker(ConnWorker, &conn);

  std::string buffer;
  char chunk[4096];
  while (g_running.load()) {
    ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
    if (n <= 0) break;
    buffer.append(chunk, static_cast<size_t>(n));
    size_t nl;
    while ((nl = buffer.find('\n')) != std::string::npos) {
      std::string line = buffer.substr(0, nl);
      buffer.erase(0, nl + 1);
      if (line.empty()) continue;

      std::string cmd;
      if (!JsonField(line, "cmd", &cmd)) {
        // 解不出 cmd 就回不了对号入座的 ack(id 也未必解得出)。只记日志，
        // 客户端靠自己的超时兜底。
        std::cout << "[WARN] 看不懂的命令行: " << line << "\n";
        continue;
      }
      const int id = static_cast<int>(JsonNumField(line, "id", 0));
      if (IsUrgent(cmd)) {
        SendTo(fd, AckFrame(id, RunCommand(cmd, line, [] { return false; })));
        continue;
      }
      {
        std::lock_guard<std::mutex> lk(conn.mtx);
        conn.queue.push_back(Job{id, cmd, line, g_cancel_gen.load()});
      }
      conn.cv.notify_one();
    }
  }

  {
    std::lock_guard<std::mutex> lk(conn.mtx);
    conn.closing = true;
  }
  conn.cv.notify_one();
  worker.join();  // 正在走的那一拍会在 50ms 内看见 closing 收手

  {
    std::lock_guard<std::mutex> lk(g_clients_mtx);
    for (size_t i = 0; i < g_clients.size(); ++i) {
      if (g_clients[i] == fd) {
        g_clients.erase(g_clients.begin() + static_cast<long>(i));
        break;
      }
    }
  }
  ::close(fd);
  // 客户端断开**不动 SDK 会话**。巡检程序重启是常态，不是事故。
  std::cout << "[i] 一个客户端断开，会话继续持有\n";
}

static int Listen(const std::string& host, int port) {
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  int yes = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(static_cast<uint16_t>(port));
  if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
    ::close(fd);
    return -1;
  }
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0 ||
      ::listen(fd, 8) < 0) {
    ::close(fd);
    return -1;
  }
  return fd;
}

// ===================================================================
// FIFO —— 给人用的应急通道，命令格式沿用 hold02
// ===================================================================

static void HandleFifoLine(const std::string& line) {
  std::istringstream is(line);
  std::string cmd;
  is >> cmd;
  if (cmd.empty()) return;
  Outcome out;
  if (cmd == "stand") {
    out = DoStand();
  } else if (cmd == "lie") {
    out = DoLie();
  } else if (cmd == "walk") {
    double sec = 1.0, fwd = 0.5;
    is >> sec;
    if (!(is >> fwd)) fwd = 0.5;
    const uint64_t gen = g_cancel_gen.load();
    out = DoWalk(sec, fwd, 0.0, 0.0,
                 [gen] { return g_cancel_gen.load() != gen; });
  } else if (cmd == "halt") {
    out = DoHalt();
  } else if (cmd == "estop") {
    out = DoEstop(true);
  } else if (cmd == "clear") {
    out = DoEstop(false);
  } else if (cmd == "hold") {
    out = DoHold();
  } else if (cmd == "shutdown") {
    out = DoShutdown();
  } else if (cmd == "status") {
    std::cout << "[STATUS] " << StateFrame() << " held=" << g_held.load()
              << " clients=" << g_clients.size() << "\n";
    return;
  } else if (cmd == "quit") {
    // hold02 的 quit 会 ReleaseControl —— 那是当天最贵的一次误操作路径。
    // 这里改名成 shutdown 并且明说，免得手指记忆把会话送走。
    std::cout << "[!] quit 已停用。真要交还控制权请敲 shutdown"
                 "（不可逆，之后要重启 RK3588）\n";
    return;
  } else {
    out = Reject("不认识的命令: " + cmd);
  }
  std::cout << "[FIFO] " << cmd << " -> " << (out.ok ? "ok" : out.error) << "\n";
}

static void FifoLoop(const std::string& path) {
  while (g_running.load()) {
    std::ifstream f(path);   // 阻塞到有写入者
    if (!f) {
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      continue;
    }
    std::string line;
    while (std::getline(f, line)) HandleFifoLine(line);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
}

// ===================================================================
// main
// ===================================================================

static void OnSig(int) {
  // **故意不 ReleaseControl。** Ctrl+C 是运维手滑的常态，让它把控制权
  // 交出去，等于把一次误按升级成"整台 RK3588 要重启"(清单 #47)。
  std::cout << "\n[i] 收到信号：只退进程，不释放控制权。\n"
               "    真要交还请发 shutdown 命令。\n";
  std::_Exit(0);
}

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "usage: " << argv[0]
              << " <ip> <port> [--listen HOST:PORT] [--fifo PATH]\n"
              << "  例: " << argv[0]
              << " 192.168.168.168 8082 --listen 127.0.0.1:8090"
                 " --fifo /tmp/d1max.cmd\n";
    return 2;
  }
  const std::string ip = argv[1], port = argv[2];
  std::string listen_host = "127.0.0.1", fifo;
  int listen_port = 8090;
  for (int i = 3; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--listen" && i + 1 < argc) {
      const std::string spec = argv[++i];
      const size_t colon = spec.rfind(':');
      if (colon == std::string::npos) {
        listen_port = std::atoi(spec.c_str());
      } else {
        listen_host = spec.substr(0, colon);
        listen_port = std::atoi(spec.c_str() + colon + 1);
      }
    } else if (arg == "--fifo" && i + 1 < argc) {
      fifo = argv[++i];
    } else {
      std::cerr << "未知参数: " << arg << "\n";
      return 2;
    }
  }
  g_robot_addr = ip + ":" + port;
  std::cout << std::unitbuf;
  std::signal(SIGINT, OnSig);
  std::signal(SIGTERM, OnSig);
  std::signal(SIGPIPE, SIG_IGN);

  SDKClient client;
  g_client = &client;
  client.SetControlCallback(std::make_shared<CtrlCb>());
  client.SetDataCallback(std::make_shared<DataCb>());

  // ---- 抢开机窗口 ----
  std::cout << "[agent] 抢连 " << g_robot_addr
            << " …（**现在就该给机器上电**，它会一直重试）\n";
  std::atomic<bool> connected{false}, failed{false};
  std::string last_err;
  int attempt = 0;
  while (!connected.load()) {
    ++attempt;
    failed = false;
    client.Connect(ip, port, false, [&](const std::error_code& e) {
      if (!e) {
        connected = true;
      } else {
        last_err = e.message();
        failed = true;
      }
    });
    for (int i = 0; i < 8 && !connected.load() && !failed.load(); ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(250));
    if (!connected.load()) {
      client.Disconnect(true);
      if (attempt % 10 == 0)
        std::cout << "[agent] 第 " << attempt << " 次未抢到，最近: "
                  << (last_err.empty() ? "网络不通" : last_err) << "\n";
      std::this_thread::sleep_for(std::chrono::milliseconds(300));
    }
    if (attempt > 2000) {
      std::cerr << "[agent] 放弃。ShakeHand failed 一直不断？端口是 8082/UDP，"
                   "不是文档里的 8081(清单 #34)\n";
      return 1;
    }
  }
  std::cout << "[agent] ★★★ 抢到窗口，已连上（第 " << attempt << " 次）★★★\n";

  auto ec = client.TakeControl(5000);
  g_held = !ec;
  std::cout << "[agent] TakeControl(): " << (ec ? ec.message() : "ok") << "\n";
  if (ec)
    std::cout << "[!] 没拿到控制权。遥测大概还能读，但动不了。"
                 "重启 RK3588 再抢一次(清单 #46/#47)\n";
  client.SetMcConfig(true, 2000);
  client.SetSpeedReportConfig(true, 20, 2000);
  client.SetJointStateConfig(true, 2000);

  // ---- 开门 ----
  const int server = Listen(listen_host, listen_port);
  if (server < 0) {
    std::cerr << "[agent] 监听 " << listen_host << ":" << listen_port
              << " 失败: " << std::strerror(errno) << "\n";
    std::cerr << "        端口被上一个 agent 占着？先确认那个还活着——"
                 "它握着控制权，别随手 kill。\n";
    return 1;
  }
  std::cout << "[agent] 监听 " << listen_host << ":" << listen_port
            << "，等巡检程序接进来\n";

  if (!fifo.empty()) {
    std::thread(FifoLoop, fifo).detach();
    std::cout << "[agent] 人工通道: echo stand > " << fifo << "\n";
  }

  std::thread(VelLoop).detach();

  std::thread([]() {
    // 心跳：定期续一次 TakeControl，并把状态刷进日志。上装那头一旦松手，
    // OnControlAvailable 会来，续控就在那条回调里。
    int beat = 0;
    while (g_running.load()) {
      std::this_thread::sleep_for(std::chrono::seconds(3));
      if (++beat % 5) continue;
      {
        std::lock_guard<std::mutex> lk(g_sdk_mtx);
        if (g_held.load()) g_client->TakeControl(0);
      }
      std::lock_guard<std::mutex> lk(g_state_mtx);
      std::cout << "[agent] 心跳#" << beat << " held=" << g_held.load()
                << " motion=" << g_motion << " batt=" << g_batt1 << "%/"
                << g_batt2 << "%\n";
    }
  }).detach();

  while (g_running.load()) {
    // 先 poll 再 accept。直接 accept 会一直阻塞在那儿,g_running 变 false
    // 也没人看见 —— shutdown 之后进程会带着已经交还的控制权继续挂着,
    // 而 field-agent.sh 正指望它退出好回去重抢。500ms 一轮足够灵敏。
    pollfd pfd{server, POLLIN, 0};
    const int ready = ::poll(&pfd, 1, 500);
    if (ready <= 0) continue;  // 超时或被信号打断,回去重看 g_running

    sockaddr_in peer{};
    socklen_t len = sizeof(peer);
    const int fd = ::accept(server, reinterpret_cast<sockaddr*>(&peer), &len);
    if (fd < 0) {
      if (errno == EINTR) continue;
      break;
    }
    int yes = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
    std::cout << "[i] 客户端接入 " << ::inet_ntoa(peer.sin_addr) << "\n";
    std::thread(ServeClient, fd).detach();
  }
  ::close(server);
  return 0;
}
