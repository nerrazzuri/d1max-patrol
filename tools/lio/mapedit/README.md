# MOLA 占据栅格地图编辑器

清理 + 手动编辑 MOLA 出的 ROS `map_server` 占据栅格(PGM+YAML),产出可直接用于机器狗导航的地图。

像素值:`OCC=0`(黑,墙/障碍) · `UNKNOWN=205`(灰,未知) · `FREE=254`(白,可通行)。

## 依赖
PyQt5 / numpy / scipy / PyYAML / Pillow(本机均已装)。需要图形显示(`DISPLAY`)。

## 启动 GUI
```bash
python3 tools/lio/mapedit/map_editor.py runs/mola/coverage2/floor.pgm
# 不带参数默认打开 coverage2/floor.pgm
```
自动读取同名 `.yaml`(继承 resolution / origin)。

## 自动清理(Auto-clean 菜单,可撤销)
- **激进档**:去噪(移除 <12px 的孤立占据飞点→free)+ 闭运算封墙缝 + **所有 unknown→free**。整图变可通行,边角/灰色不确定都当可移动。
- **保守档**:同上,但只把「不接触图像边界」的 unknown→free,外圈 unknown 保留(避免把地图外也判为可走)。
- 单项:Denoise only / Fill ALL unknown。

⚠️ 激进档会把地图边界外也变成可通行 —— 之后用「虚拟墙」工具圈定真实可行区域边界。

## 手动工具(工具栏)
- **Wall pen / Free pen / Unknown pen**:画墙/擦成可通行/标未知,笔刷大小可调
- **Virtual wall**:点起点、点终点,画一条占据直线(不可通行边界)
- **Rect fill**:拖矩形,按右侧下拉框填成 free/occ/unknown(批量清理大块噪声)
- **Undo / Redo**:Ctrl+Z / Ctrl+Y
- 中键拖动 = 平移,滚轮 = 缩放

## 导出(File 菜单)
- **Export (pgm+yaml)** Ctrl+S → `<name>_edited.pgm` + `<name>_edited.yaml`(ROS 导航直接可用)
- **Export PNG** → `<name>_edited.png`(展示用)

## 无界面自检
```bash
python3 tools/lio/mapedit/map_editor.py --selftest runs/mola/coverage2/floor.pgm
```
验证 PGM 往返无损 + 打印清理前后 occ/unknown/free 占比。
