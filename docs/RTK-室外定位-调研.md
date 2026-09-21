# RTK 室外定位调研(2026-09,槟城庄园安防巡检)

场景从"室内展厅"转为**室外庄园**(马来西亚槟城,法式城堡+草坪+大树)。室外定位主力应是
**RTK-GNSS**,不是纯 LiDAR。本文汇总硬件、改正源、覆盖、成本与落地路径。

> 背景:展厅玻璃幕墙导致纯 LiDAR scan-match 跳假位,那是**测试环境的坑**,不是真实部署。
> 真实庄园环境:室外有 RTK、几何特征丰富(城堡/树干/灌木)、无玻璃幕墙污染 —— 适合工业级定位。
> 定位漂移的根治靠 RTK 这个绝对参考(四足腿式里程 ~2m/9m 会漂,需 RTK 周期纠回)。

## 1. 硬件(厂商文档实锤,见 `refs/hardware/D1Max-SDK开发指南...txt` §4.5)

- **RTK 模组**:六分科技 **LK1220D**(工规级全频、1040 通道、**支持双天线**、内置 NRTK & **定向算法**)
- **推荐天线**:北天 **BT560**,**SMA 接头**,约 ¥150~400/根(双天线可做朝向)
- **精度**:单点 1.5m / 差分 2~3cm / RTK **1cm+1ppm**(条件:高精度天线、空旷、距基站 1km 内*)
- **话题**:`/rtk_pvh`;需外接天线(SMA)+ 开通改正服务账号 + 4G 联网
- 现状:接收机在(有 topic),**但没天线 → 空包无数据**。第一步是补天线。
  \* "距基站 1km" 是**单基站 RTK**的限制;用**网络 RTK/VRS**(MyRTKnet、RTKdata)不受此限,槟城全境 cm 级。

## 2. 改正源(RTCM):三选一

| 来源 | 槟城覆盖 | 费用 | 备注 |
|---|---|---|---|
| **六分科技(厂商默认)** | ❌ 几乎不覆盖 | 会员制(sixents.com,AK/AS) | 地基网在中国大陆;马来西亚要问其海外/星基 |
| **MyRTKnet(JUPEM 政府)** | ✅ 全覆盖 | 免费 | ~78 站/65km;要审批、外企未必好批;`pxy.myrtknet.gov.my:2101`(VRS) |
| **RTKdata(商业)** | ✅ 全覆盖 | **$40/月 或 $400/年,30 天免费试** | 270+ 站/35km;自助注册;AUTO mountpoint;**任何 RTCM3 设备**;GPS/北斗/Galileo/GLONASS |

来源:[RTKdata Malaysia](https://rtkdata.com/my/)、[MyRTKnet(FIG 论文)](http://fig.net/resources/proceedings/fig_proceedings/fig2010/papers/ts08f/ts08f_hasan_azhari_et_al_4742.pdf)。

**验证阶段建议直接用 RTKdata 30 天免费试用**(零成本、自助、标准 RTCM3);生产再选 MyRTKnet(免费但麻烦)或 RTKdata(省事)。

## 3. 唯一命门:LK1220D 开不开放标准 NTRIP / RTCM3

马来西亚改正源既有又便宜又是标准 RTCM3。整条链只剩一个未知:
**六分 LK1220D 模组能否接标准 NTRIP caster / 外部 RTCM3(而非锁死六分 SDK)。**

- 看竞品**天线没用**:天线只被动收卫星,与"用哪个改正服务"无关;换服务不换天线。
- 验证方法(比研究竞品直接):**SSH 进 D1 Max 看它现在怎么拿改正**
  ```bash
  ssh robot@192.168.234.1   # 密码 bot
  ps aux | grep -Ei "rtk|ntrip|gnss|sixents|cors"
  grep -RniE "ntrip|caster|rtcm|sixents|cors|2101|mountpoint" /ota /opt /etc /agibot 2>/dev/null | head -50
  cat /ota/sixents_config.ini
  ```
  - 找到 caster/port/mountpoint 字段 → 走标准 NTRIP,换成 RTKdata/MyRTKnet 即可(甚至免自架基站)
  - 只有六分 AK/AS、无 caster 字段 → 锁在六分 SDK,须问智元能否开放 NTRIP

## 4. 落地路径(按依赖、省钱排序)

1. 买北天 BT560 天线插上,空旷处测**单点定位**(不需服务)→ 验证天线+模组+`/rtk_pvh` 链路
2. SSH 看 RTK 配置 → 判断能否换 NTRIP 源
3. 能换 → 开 **RTKdata 30 天免费试用**,填 caster,测 **fixed** → 零成本验 cm 级
4. 锁死六分 → 问智元"能否开放 NTRIP" + 问六分"马来西亚覆盖"

## 5. 融合(达到工业级、抗遮挡)

庄园大树/城堡会间歇遮挡 GPS → **不能纯 RTK**,标准做法:
**RTK + IMU + 3D LiDAR-inertial 紧耦合(GNSS-LIO 因子图)**
- 开阔草坪:RTK 主导(cm、无漂)
- 树下/城堡边 RTK 失锁:LiDAR-inertial(城堡/树干/灌木特征)+ IMU 撑住,RTK 恢复纠回
- LiDAR 用低高度带点云(避开飘动树冠)

## 6. 待厂商/服务商确认(问题清单)

**智元(命门):** ① LK1220D 能否配六分之外的标准 NTRIP?② 能否接外部 RTCM3?③ 双天线定向怎么配、`/rtk_pvh` 有无 heading?④ `/rtk_pvh` 字段(fix 状态/精度/坐标系)?
**六分:** 马来西亚有无覆盖(地基/星基)?海外开通与价格?
**JUPEM/MyRTKnet:** 外企能否注册?流程/时长?槟城 mountpoint?
