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

### Occupancy method (commercial-style vs quick)
By default the PGM is built by **ray-tracing** from each sensor pose (`render_floormap.py --traj traj.tum`):
rays are cast to the walls and every traversed cell is marked free → **thin walls + filled interior**,
matching commercial nav maps; cells never observed stay unknown (honest). Without `--traj` it falls
back to a floor-point heuristic (more gray holes, thicker walls).

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
