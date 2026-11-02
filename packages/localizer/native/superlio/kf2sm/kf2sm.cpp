// Super-LIO 建图前端(工单 21):关键帧位姿 + 点云 → MRPT simplemap,接我们现有的出图流程(sm2mm、回环、build.package)。
// 输入目录里 index.txt 每行:t x y z qx qy qz qw vx vy vz wx wy wz 文件名(float32 xyz,雷达系)。
#include <mrpt/maps/CSimpleMap.h>
#include <mrpt/maps/CPointsMapXYZIRT.h>
#include <mrpt/obs/CObservationPointCloud.h>
#include <mrpt/obs/CSensoryFrame.h>
#include <mrpt/poses/CPose3DPDFGaussian.h>
#include <mrpt/poses/CPose3DQuat.h>
#include <mrpt/system/datetime.h>

#include <fstream>
#include <iostream>
#include <sstream>
#include <vector>

int main(int argc, char** argv) {
  if (argc != 3) {
    std::cerr << "用法: kf2sm <关键帧目录> <输出.simplemap>\n";
    return 2;
  }
  const std::string dir = argv[1];
  std::ifstream idx(dir + "/index.txt");
  if (!idx) {
    std::cerr << "读不到 " << dir << "/index.txt\n";
    return 1;
  }
  mrpt::maps::CSimpleMap sm;
  std::string line;
  size_t n = 0, pts_total = 0;
  while (std::getline(idx, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream ss(line);
    double t, x, y, z, qx, qy, qz, qw, vx, vy, vz, wx, wy, wz;
    std::string fname;
    if (!(ss >> t >> x >> y >> z >> qx >> qy >> qz >> qw >> vx >> vy >> vz >> wx >> wy >> wz >> fname)) {
      std::cerr << "坏行: " << line << "\n";
      return 1;
    }
    std::ifstream f(dir + "/" + fname, std::ios::binary | std::ios::ate);
    if (!f) {
      std::cerr << "读不到 " << fname << "\n";
      return 1;
    }
    const auto bytes = static_cast<size_t>(f.tellg());
    f.seekg(0);
    std::vector<float> buf(bytes / sizeof(float));
    f.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(buf.size() * sizeof(float)));
    // 跟 MOLA 建图存的同一种点云(带每点时间):回环流程里的 FilterAdjustTimestamps 要时间字段,
    // 没有就整个失败。关键帧点云不去畸变,时间全填 0。
    auto pts = mrpt::maps::CPointsMapXYZIRT::Create();
    const size_t np = buf.size() / 3;
    pts->resize_XYZIRT(np, false, false, true);
    for (size_t i = 0; i < np; ++i) {
      pts->setPointFast(i, buf[3 * i], buf[3 * i + 1], buf[3 * i + 2]);
      pts->setPointTime(i, 0.0f);
    }
    pts->mark_as_modified();
    pts_total += pts->size();

    auto obs = mrpt::obs::CObservationPointCloud::Create();
    obs->sensorLabel = "lidar";
    obs->timestamp = mrpt::Clock::fromDouble(t);
    obs->sensorPose = mrpt::poses::CPose3D();
    obs->pointcloud = pts;
    auto sf = mrpt::obs::CSensoryFrame::Create();
    sf->insert(obs);

    auto pdf = mrpt::poses::CPose3DPDFGaussian::Create();
    pdf->mean = mrpt::poses::CPose3D(mrpt::poses::CPose3DQuat(x, y, z, mrpt::math::CQuaternionDouble(qw, qx, qy, qz)));
    sm.insert(pdf, sf, mrpt::math::TTwist3D(vx, vy, vz, wx, wy, wz));
    ++n;
  }
  if (!n) {
    std::cerr << "没有关键帧\n";
    return 1;
  }
  if (!sm.saveToFile(argv[2])) {
    std::cerr << "写不出 " << argv[2] << "\n";
    return 1;
  }
  std::cout << "kf2sm: " << n << " 个关键帧, " << pts_total << " 点 → " << argv[2] << "\n";
  return 0;
}
