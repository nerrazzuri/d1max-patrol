# mobile —— D1 Max 巡检手机 app

Flutter，只做 Android。**手机只连站点，不直连狗**（W00c5e 删掉了过渡用的「现场直连」）。
**说明看 `../docs/手机app.md`**：有哪几屏、站点列表存什么、怎么在这台机器上跑、踩过哪些坑。

```bash
export PATH="<Flutter 解压目录>/bin:$PATH"   # Flutter 不在 PATH 里，每次显式加
flutter analyze
flutter test --concurrency=1                 # 不许裸跑：裸跑会无声吞掉最后一个测试文件
```
