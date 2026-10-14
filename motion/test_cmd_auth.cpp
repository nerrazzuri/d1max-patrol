// cmd_auth.hpp 的行为测试(S11):不链接厂商 SDK。
//   make test_cmd_auth && ./test_cmd_auth
// 任何一条失败就打印并退出码非 0。tests/sim/test_agent_vel.py 编译并运行它。
#include "cmd_auth.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>

using cmdauth::ConnAuth;

static int g_fail = 0;
#define CHECK(c)                                                    \
  do {                                                              \
    if (!(c)) {                                                     \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);      \
      ++g_fail;                                                     \
    }                                                               \
  } while (0)

static bool Denied(const std::string& why) {
  return why.rfind(cmdauth::kNotAuthorized, 0) == 0;
}

static std::string Slurp(const std::string& p) {
  std::ifstream f(p);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

static mode_t ModeOf(const std::string& p) {
  struct stat st {};
  if (::lstat(p.c_str(), &st) != 0) return 0;
  return st.st_mode;
}

int main() {
  {  // 不要令牌的:停车、按下急停、净空许可、auth;别的(包括松开急停)都要
    for (const char* c : {"halt", "auth", "clear"}) CHECK(cmdauth::IsOpen(c, true));
    CHECK(cmdauth::IsOpen("estop", true));
    CHECK(!cmdauth::IsOpen("estop", false));
    for (const char* c : {"vel", "walk", "stand", "lie", "head", "hold", "shutdown", "light",
                          "什么", ""})
      CHECK(!cmdauth::IsOpen(c, true));
  }
  {  // FIFO 不可信时只收停车、急停(按下)、看状态;clear 在 FIFO 里是松开急停,不收
    for (const char* c : {"halt", "estop", "status"}) CHECK(cmdauth::FifoOpen(c));
    for (const char* c : {"stand", "lie", "walk", "clear", "hold", "shutdown", "quit"})
      CHECK(!cmdauth::FifoOpen(c));
  }
  {  // 比较
    CHECK(cmdauth::SameToken("abcd", "abcd"));
    CHECK(!cmdauth::SameToken("abcd", "abce"));
    CHECK(!cmdauth::SameToken("abcd", "abc"));
    CHECK(!cmdauth::SameToken("", ""));  // 没有令牌:空串也不算对
  }
  {  // 没有令牌(写不进去):谁都认证不了,停车照常
    const std::string none;
    ConnAuth a(&none);
    CHECK(Denied(a.Auth("")));
    CHECK(Denied(a.Auth("00")));
    CHECK(!a.authed());
    CHECK(Denied(a.Check("vel", true)));
    CHECK(a.Check("halt", true).empty());
    CHECK(a.Check("estop", true).empty());
    CHECK(Denied(a.Check("estop", false)));
    ConnAuth n(nullptr);
    CHECK(Denied(n.Auth("x")));
  }
  {  // 有令牌:认证之前运动命令拒,令牌不对还是拒,对了这条连接放行;别的连接不跟着放行
    const std::string tok = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    ConnAuth a(&tok);
    CHECK(Denied(a.Check("vel", true)));
    CHECK(Denied(a.Check("walk", true)));
    CHECK(Denied(a.Check("stand", true)));
    CHECK(a.Check("halt", true).empty());
    const std::string wrong = a.Auth(tok.substr(0, 63) + "0");
    CHECK(Denied(wrong));
    CHECK(wrong.find(tok) == std::string::npos);  // 报错里不带期望的令牌
    CHECK(!a.authed());
    CHECK(Denied(a.Check("vel", true)));
    CHECK(a.Auth(tok).empty());
    CHECK(a.authed());
    for (const char* c : {"vel", "walk", "stand", "lie", "head", "hold", "shutdown", "light"})
      CHECK(a.Check(c, true).empty());
    CHECK(a.Check("estop", false).empty());
    ConnAuth b(&tok);
    CHECK(Denied(b.Check("vel", true)));
  }
  {  // 令牌:64 个十六进制字符,两次不一样
    std::string err;
    const std::string t1 = cmdauth::NewToken(&err), t2 = cmdauth::NewToken(&err);
    CHECK(err.empty());
    CHECK(t1.size() == 2 * cmdauth::kTokenBytes);
    CHECK(t1.find_first_not_of("0123456789abcdef") == std::string::npos);
    CHECK(t1 != t2);
  }
  char tmpl[] = "/tmp/test_cmd_auth.XXXXXX";
  const char* made = ::mkdtemp(tmpl);
  CHECK(made != nullptr);
  const std::string root = made ? made : "/tmp";
  {  // 写令牌文件:0600、内容是令牌加换行、没目录就建 0700、重写覆盖、不留临时文件
    const std::string path = root + "/sub/sidecar.token";
    CHECK(cmdauth::WriteTokenFile(path, "aa").empty());
    CHECK(Slurp(path) == "aa\n");
    CHECK((ModeOf(path) & 07777) == 0600);
    CHECK((ModeOf(root + "/sub") & 07777) == 0700);
    CHECK(cmdauth::WriteTokenFile(path, "bb").empty());
    CHECK(Slurp(path) == "bb\n");
    const std::string tmp = path + ".tmp." + std::to_string(::getpid());
    CHECK(ModeOf(tmp) == 0);
  }
  {  // 目录谁都能写、又不是粘滞的:不写(别人能把令牌文件换掉);粘滞的(/tmp 那种)可以
    const std::string open_dir = root + "/open";
    CHECK(::mkdir(open_dir.c_str(), 0700) == 0);
    CHECK(::chmod(open_dir.c_str(), 0777) == 0);
    CHECK(!cmdauth::WriteTokenFile(open_dir + "/t", "x").empty());
    CHECK(ModeOf(open_dir + "/t") == 0);
    CHECK(::chmod(open_dir.c_str(), 01777) == 0);
    CHECK(cmdauth::WriteTokenFile(open_dir + "/t", "x").empty());
  }
  {  // 令牌路径上原来是个符号链接:换掉链接本身,不写进它指着的文件
    const std::string victim = root + "/victim", link = root + "/linked.token";
    std::ofstream(victim) << "keep";
    CHECK(::symlink(victim.c_str(), link.c_str()) == 0);
    CHECK(cmdauth::WriteTokenFile(link, "cc").empty());
    CHECK(Slurp(victim) == "keep");
    CHECK(S_ISREG(ModeOf(link)));
    CHECK(Slurp(link) == "cc\n");
  }
  {  // FIFO 可不可信:自己的、只有自己能写 → 可信;组或别人能写、不是管道、属主不是自己 → 不可信
    const std::string fifo = root + "/cmd.fifo";
    CHECK(::mkfifo(fifo.c_str(), 0600) == 0);
    struct stat st {};
    CHECK(::stat(fifo.c_str(), &st) == 0);
    CHECK(cmdauth::FifoTrusted(st, ::geteuid()));
    CHECK(!cmdauth::FifoTrusted(st, ::geteuid() + 1));
    CHECK(::chmod(fifo.c_str(), 0644) == 0 && ::stat(fifo.c_str(), &st) == 0);
    CHECK(cmdauth::FifoTrusted(st, ::geteuid()));  // mkfifo 的默认(别人只读):可信
    CHECK(::chmod(fifo.c_str(), 0620) == 0 && ::stat(fifo.c_str(), &st) == 0);
    CHECK(!cmdauth::FifoTrusted(st, ::geteuid()));
    CHECK(::chmod(fifo.c_str(), 0602) == 0 && ::stat(fifo.c_str(), &st) == 0);
    CHECK(!cmdauth::FifoTrusted(st, ::geteuid()));
    CHECK(::stat((root + "/victim").c_str(), &st) == 0);
    CHECK(!cmdauth::FifoTrusted(st, ::geteuid()));  // 普通文件
  }
  {  // ParentDir
    CHECK(cmdauth::ParentDir("a/b/c") == "a/b");
    CHECK(cmdauth::ParentDir("c") == ".");
    CHECK(cmdauth::ParentDir("/c") == "/");
  }
  std::string rm = "rm -rf '" + root + "'";
  if (made && std::system(rm.c_str()) != 0) std::printf("(临时目录 %s 没删掉)\n", root.c_str());
  if (g_fail) {
    std::printf("cmd_auth: %d 条失败\n", g_fail);
    return 1;
  }
  std::printf("cmd_auth: ok\n");
  return 0;
}
