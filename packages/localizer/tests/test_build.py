

def test_改名前的老包_合并话题叫老名字_也按前雷达系建():
    from nexwar_localizer.replay import _frame_of
    assert _frame_of("/d1max/merged_lidar") == _frame_of("/nexwar/merged_lidar") == "rslidar_head"
