#!/bin/bash
set -e

# setup ros environment
source "/opt/ros/$ROS_DISTRO/setup.bash"
source "/rosbridge_ws/devel/setup.bash" || true
source "/catkin_install/devel/setup.bash" || true
source "/catkin_install/install_isolated/setup.bash" || true
export ROS_HOSTNAME=localhost
export ROS_MASTER_URL=http://localhost:11311
export ROS_PACKAGE_PATH=$ROS_PACKAGE_PATH:/catkin_install/devel/src
export ROS_PACKAGE_PATH=$ROS_PACKAGE_PATH:/catkin_install/install_isolated/src
export ROS_PACKAGE_PATH=$ROS_PACKAGE_PATH:/catkin_install/src


{% for path in external_lib_paths %}
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/catkin_install/src/{{ path }}
{% endfor %}


nohup roslaunch rosbridge_server rosbridge_websocket.launch > /dev/null &

sleep 1

# roslaunch compression.launch &  George do not know what it the use.

{% for cmd in start_cmds %}
{{ cmd }}
{% endfor %}

