# MOLA LiDAR-Odometry floor mapping (D1 Max / RS-Airy)

One-click pipeline to turn a ROS2 bag into a clean floor map + ROS occupancy grid,
using **MOLA LiDAR-Odometry** (chosen over GLIM/FAST-LIO after a 2026-09-18 bake-off:
cleaner, geometrically truer, glitch-free).

## Install (Ubuntu 22.04 + ROS2 Humble)
```bash
sudo apt install ros-humble-mola ros-humble-mola-lidar-odometry ros-humble-mola-input-rosbag2
```

## Run
```bash
./make_floor_map.sh <bag.mcap|bag_dir> [out_dir] [lidar_topic] [base_frame]
# defaults: out_dir=./mola_out  lidar_topic=/front_lidar  base_frame=rslidar_head
```
Outputs in `out_dir`:
- `traj.tum` — trajectory (TUM format)
- `map.ply_raw.ply` — full point cloud
- `floor_wallplan.png` — gravity-aligned wall-slab top-down floor plan (thin ~1 m slab = crisp walls)
- `floor.pgm` + `floor.yaml` — ROS `map_server` occupancy grid (black=occupied, white=free, gray=unknown)

Load the grid: `ros2 run nav2_map_server map_server --ros-args -p yaml_filename:=out_dir/floor.yaml`

### Occupancy method (log-odds ray-tracing)
With `--traj traj.tum` the PGM uses **log-odds occupancy**: each cell counts wall-point "hits" vs
ray "pass-throughs"; a cell is occupied only if hits are significant relative to pass-throughs
(`occ>=5 & hits/(hits+passes)>0.25`). This gives **thin walls + filled free interior** AND removes
**dynamic objects / transient clutter** (people, glass returns) that a raw point-count would wrongly
mark occupied. Cells never observed stay unknown. Without `--traj` it falls back to a floor-point
heuristic (more gray holes, thicker walls, no dynamic removal).

### unified_occ.py — density-independent renderer for FAIR SLAM comparison
`unified_occ.py <cloud.ply> <out.png>` renders occupancy that is **independent of point density**
(3D voxel-dedup at `--vox` before rasterizing, occupied = ≥1 dedup point). Use it to compare two
SLAM outputs fairly (same `--vox --res --zlo --zhi`), or as a **decoupled map generator** (feed it
any SLAM's registered cloud). Lesson learned: raw-PGM occupancy-cell counts differ ~5× between GLIM
and MOLA purely from each framework's internal downsampling — NOT a geometry difference; unified_occ
shows they are actually comparable (~230k cells each on coverage2).

Remaining gap to a polished commercial map is **coverage** (the robot must actually traverse the whole
floor with continuous motion — no algorithm fills unvisited area; ray "fans" in the PGM mark wall gaps)
and an optional final hand-clean of the PGM in an image editor (close walls, remove noise/fans).

## Notes / gotchas (learned the hard way)
- **LiDAR-only** here. IMU is intentionally not fused: the RS-Airy IMU-LiDAR extrinsic has
  weakly-observable yaw on low-excitation bags, and tight fusion collapses XY. If you do fuse
  IMU, the correct GLIM `T_lidar_imu` is the FAST-LIO `extrinsic_R/T` used **directly, not
  inverted** (proven by gravity: inverting flips gravity upside-down).
- `base_frame` must be the sensor frame (`rslidar_head`): the bag has no base→sensor static tf,
  only FAST-LIO's `map→odom→base_link`.
- The RS-Airy raw frame is **X-up** (gravity ≈ -X), not Z-up. `render_floormap.py` estimates the
  up direction from the map's floor plane (RANSAC), so it self-adapts per bag.
- **Coverage = motion.** The map only covers where the robot actually walked. For a whole-floor
  map you need a bag with continuous full-floor traversal — no algorithm can fill unvisited area.
- RS-Airy per-point `timestamp` is clean (absolute seconds, ~100 ms/frame); not a deskew problem.
