# 现场用的 Makefile —— 只用 g++，不走 cmake。
#
# 厂商 example 的 CMakeLists 有个坑：BUILD_RPATH 里用了从没定义过的
# ${ARCH_DIR}，编出来的东西跑不起来（见 docs/真机联调手册.md B4）。这里
# 直接把 rpath 写死，编完就能跑，不用每次 export LD_LIBRARY_PATH。
#
#   make                # 编全部
#   make patrol_agent   # 只编常驻旁路进程
#   make SDK=0.2.0      # 换 SDK 版本（默认 0.1.1，现场结论见清单 #45）
#   make ARCH=aarch64   # 搬到机器上板载编译时

SDK  ?= 0.1.1
ARCH ?= x86_64
SDK_ROOT := ../refs/robot-sdk/RobotSDK-$(SDK)
SDK_LIB  := $(SDK_ROOT)/lib/$(ARCH)

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra -pthread -I$(SDK_ROOT)/include
LDFLAGS  += -L$(SDK_LIB) -Wl,-rpath,'$$ORIGIN/$(SDK_LIB)' -Wl,-rpath,$(abspath $(SDK_LIB))
LDLIBS   += -lrobot_sdk -pthread

# patrol_agent 放第一位：它是产品件，其余是现场留下的一次性侦察程序。
BINS := patrol_agent hold01 hold02 stand01 stand2 walk1 walkonly liedown \
        m0_takecontrol m1_standup

.PHONY: all clean
all: $(BINS)

%: %.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

clean:
	rm -f $(BINS)
