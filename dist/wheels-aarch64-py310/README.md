# 离线 wheel 包（aarch64 / Python 3.10）

**这个目录是给「Orin NX 出不了外网」那种现场准备的。** 能出外网就不用管它，
`install.sh` 自己会去 pypi 上取。

## 什么时候用得上

装机之前先在狗上试一句：

```sh
curl -sS -m 8 https://pypi.org/simple/ >/dev/null && echo 通 || echo 不通
```

报「不通」，就把这个目录整个拷到 U 盘上，然后按下面那一节装。

## 怎么用

```sh
sudo D1MAX_PIP_ARGS='--no-index --find-links=/media/<U盘>/wheels-aarch64-py310' bash deploy/install.sh <包目录>
```

`D1MAX_PIP_ARGS` 会被原样传给 `install.sh` 里**每一处** `pip install`。
**不许只带一半** —— 现场会在没带的那一处停住，症状是「装到一半不动了」，
最难查的那一种。

## 里头是什么，为什么是这六个

三个是运行期真要用的依赖，版本**跟仿真里跑过全部用例的那一套完全一致**：

| 包 | 版本 | 为什么钉这个版本 |
| --- | --- | --- |
| `websockets` | 16.0 | `pyproject.toml` 写的是 `>=13`，但**实际验证过的只有 16.0**。现场不是试新版本的地方。 |
| `PyYAML` | 6.0.3 | 任务包和参数文件都靠它读。 |
| `tzdata` | 2025.2 | 时区必须跟着我们的包走，不能靠系统的 `/usr/share/zoneinfo` —— 刷机、OTA、换主板都会把系统时区库一起抹掉。 |

另外三个是 pip 自己装东西时要用的，少一个都会让 `--no-index` 那条路断在半道：

| 包 | 版本 | 为什么必须在 |
| --- | --- | --- |
| `pip` | 25.3 | `install.sh` 会先 `pip install --upgrade pip`。这一步失败是容错的（脚本打一行提示继续走），但带上省一次心跳。 |
| `setuptools` | 80.9.0 | **这一个是硬的。** 我们的包是 `pip install <本地目录>`，pyproject 的 `build-system.requires = ["setuptools>=68"]`，pip 建隔离构建环境时要现取 setuptools；`--no-index` 之下取不到就直接失败。 |
| `wheel` | 0.45.1 | 新版 setuptools 其实不需要它了，备着不占地方。 |

## 拷到 U 盘之后先对一遍

```sh
cd /media/<U盘>/wheels-aarch64-py310 && sha256sum -c SHA256SUMS.txt
```

六行全是 `OK` 才算数。**U 盘在车上颠一路，坏一个字节 pip 报的错跟「网不通」
长得一模一样**，现场会往错的方向查半小时。

## 这些包是给谁的

- CPU 架构：`aarch64`（Orin NX 是 ARM64，**不是** x86_64）
- Python：`cp310`（Ubuntu 22.04 自带的 3.10；ROS2 humble 也是这一档）

狗上 `uname -m` 报的不是 `aarch64`，或者 `python3 -V` 不是 3.10.x，
**这个目录就不能用** —— 别硬装，`pip` 会报 `not a supported wheel on this
platform`，照着下面重新取一份对的：

```sh
pip download --dest wheels-aarch64-py310 --platform manylinux2014_aarch64 --python-version 310 --implementation cp --abi cp310 --only-binary=:all: 'websockets==16.0' 'PyYAML==6.0.3' 'tzdata==2025.2'
pip download --dest wheels-aarch64-py310 --only-binary=:all: 'pip==25.3' 'setuptools==80.9.0' 'wheel==0.45.1'
```

（后一条不带 `--platform`：这三个是纯 Python 包，一份通吃。）
