// 旁路进程只收代理的运动命令(S11,地图与导航能力对账 2026-10-14)。
//
// 以前 127.0.0.1:8090 和 FIFO 谁都能发 vel/walk:现场脚本、标定脚本、老命令行 —— 全都绕过代理,
// 也就绕过了禁行区、限速区、避障守卫。现在(协议 9):
//
// - **令牌**:旁路进程每次起来(≈ 每次开机抢窗口)从 /dev/urandom 生成一个 256 位的随机令牌,写进
//   --token-file(默认 ~/.d1max/sidecar.token,0600,先写临时文件再改名)。令牌不进命令行参数(ps 看得见)、
//   不进日志。
// - **每条连接认证一次**:连上先发 {"cmd":"auth","token":"…"},对了这条连接才能发运动命令。
// - **停车永远不挡**:halt、estop(按下)谁都能发 —— 令牌丢了、读不了、代理没连上的时候正是最要能停的
//   时候;叫停只会让狗少动、不会让它多动。松开急停(estop on=false)要令牌:松开就是又能动了。
// - **clear(净空许可)不要令牌**:它只在 --require-clearance 时看,只放行**已认证的** vel 的前进分量,
//   自己动不了狗;感知节点是另一个服务,暂时不带令牌(遗留,见 docs/真机联调手册.md)。
// - **FIFO**(给人用的应急通道):它的门就是它的文件权限。属主是旁路进程这个账号、组和别人都不能写的,
//   照旧全收;不是的(谁都能写的管道)只收停车、急停、status。
//
// 谁能读到令牌,谁就能开车:令牌文件属主是起旁路进程的那个账号。狗上代理和旁路进程是同一个账号
// (robot),同账号的进程**故意**去读令牌文件也读得到 —— 挡的是「随手一个脚本就能让狗走」,不是同账号的
// 恶意进程(它本来就能杀代理、写定位桥)。现场工具要开车得**显式**给 --token-file(docs/真机联调手册.md)。
//
// 不链接厂商 SDK,开发机上就能测(test_cmd_auth.cpp)。
#pragma once

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <string>

namespace cmdauth {

// 令牌是多少字节的随机数(十六进制写出来是两倍长)。
constexpr size_t kTokenBytes = 32;

// 拒绝原因都以它开头:代理那头按它认出「是认证的事」。
constexpr const char* kNotAuthorized = "not_authorized";

// 不要令牌就能发的命令。见文件头:停车永远不挡;松开急停要令牌。
inline bool IsOpen(const std::string& cmd, bool estop_on) {
  if (cmd == "halt" || cmd == "auth" || cmd == "clear") return true;
  if (cmd == "estop") return estop_on;
  return false;
}

// FIFO 不可信(见 FifoTrusted)时只收这几样:按下急停(FIFO 的 estop 只按下,松开是 clear)、停车、看状态。
inline bool FifoOpen(const std::string& cmd) {
  return cmd == "halt" || cmd == "estop" || cmd == "status";
}

// FIFO 可不可信:是个管道、属主是旁路进程这个账号、组和别人都不能写。
inline bool FifoTrusted(const struct stat& st, uid_t euid) {
  return S_ISFIFO(st.st_mode) && st.st_uid == euid && (st.st_mode & (S_IWGRP | S_IWOTH)) == 0;
}

// 常数时间比较(比对时长不泄露前几位对不对)。want 为空 = 旁路进程没有令牌,谁都不对。
inline bool SameToken(const std::string& want, const std::string& got) {
  if (want.empty() || want.size() != got.size()) return false;
  unsigned char diff = 0;
  for (size_t i = 0; i < want.size(); ++i)
    diff |= static_cast<unsigned char>(want[i] ^ got[i]);
  return diff == 0;
}

// 一条连接的认证状态。token 指着旁路进程的令牌(空 = 没有令牌,谁也认证不了)。
class ConnAuth {
 public:
  explicit ConnAuth(const std::string* token) : token_(token) {}

  // auth 命令:对了记住、回空串;不对回原因(不说期望的是什么)。
  std::string Auth(const std::string& got) {
    if (token_ == nullptr || token_->empty())
      return std::string(kNotAuthorized) +
             ": 旁路进程没有令牌(--token-file 写不进去),谁都开不了车;停车、急停照常";
    if (!SameToken(*token_, got))
      return std::string(kNotAuthorized) + ": 令牌不对(旁路进程重启过就换了,重新读令牌文件)";
    authed_ = true;
    return "";
  }

  // 这条连接能不能发这条命令:能回空串,不能回原因。
  std::string Check(const std::string& cmd, bool estop_on) const {
    if (authed_ || IsOpen(cmd, estop_on)) return "";
    return std::string(kNotAuthorized) + ": " + cmd +
           " 要先认证(发 auth,令牌在旁路进程 --token-file 那个文件里);停车、急停不用";
  }

  bool authed() const { return authed_; }

 private:
  const std::string* token_;
  bool authed_ = false;
};

// 32 字节随机数的十六进制。读不了 /dev/urandom 回空串、err 里写原因。
inline std::string NewToken(std::string* err) {
  unsigned char b[kTokenBytes];
  std::FILE* f = std::fopen("/dev/urandom", "rb");
  if (f == nullptr) {
    if (err) *err = std::string("打不开 /dev/urandom: ") + std::strerror(errno);
    return "";
  }
  const size_t n = std::fread(b, 1, sizeof(b), f);
  std::fclose(f);
  if (n != sizeof(b)) {
    if (err) *err = "/dev/urandom 读不够";
    return "";
  }
  static const char* kHex = "0123456789abcdef";
  std::string out;
  out.reserve(2 * sizeof(b));
  for (unsigned char c : b) {
    out += kHex[c >> 4];
    out += kHex[c & 0xf];
  }
  return out;
}

// 文件路径的上一级目录("a/b/c" → "a/b";"c" → ".")。
inline std::string ParentDir(const std::string& path) {
  const size_t slash = path.rfind('/');
  if (slash == std::string::npos) return ".";
  if (slash == 0) return "/";
  return path.substr(0, slash);
}

// 把令牌写进 path:上一级目录没有就建(0700);目录谁都能写又不是粘滞的(别人能换掉文件)就不写;先写
// 同目录的临时文件(0600、O_EXCL|O_NOFOLLOW)再改名 —— 读的人要么读到老的、要么读到整个新的。
// 成了回空串,不成回原因。
inline std::string WriteTokenFile(const std::string& path, const std::string& token) {
  const std::string dir = ParentDir(path);
  if (::mkdir(dir.c_str(), 0700) != 0 && errno != EEXIST)
    return "建不了目录 " + dir + ": " + std::strerror(errno);
  struct stat ds {};
  if (::stat(dir.c_str(), &ds) != 0) return "看不了目录 " + dir + ": " + std::strerror(errno);
  if ((ds.st_mode & (S_IWGRP | S_IWOTH)) != 0 && (ds.st_mode & S_ISVTX) == 0)
    return "目录 " + dir + " 组或别人能写(能把令牌文件换掉):换一个只有这个账号能写的目录";
  const std::string tmp = path + ".tmp." + std::to_string(::getpid());
  ::unlink(tmp.c_str());
  const int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0) return "建不了 " + tmp + ": " + std::strerror(errno);
  std::string why;
  if (::fchmod(fd, 0600) != 0) why = std::string("fchmod: ") + std::strerror(errno);
  const std::string body = token + "\n";
  size_t done = 0;
  while (why.empty() && done < body.size()) {
    const ssize_t n = ::write(fd, body.data() + done, body.size() - done);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      why = std::string("写不进去: ") + std::strerror(errno);
      break;
    }
    done += static_cast<size_t>(n);
  }
  if (why.empty() && ::fsync(fd) != 0) why = std::string("fsync: ") + std::strerror(errno);
  ::close(fd);
  if (why.empty() && ::rename(tmp.c_str(), path.c_str()) != 0)
    why = "改不了名 " + path + ": " + std::strerror(errno);
  if (!why.empty()) ::unlink(tmp.c_str());
  return why;
}

}  // namespace cmdauth
