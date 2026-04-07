# DSO（Direct Sparse Odometry）

单目直接法稀疏视觉里程计。论文与资源见 [TUM DSO 主页](https://vision.in.tum.de/dso)。

## 获取源码（含子模块）

本仓库将 **Pangolin 固定为 v0.6**，以 [git submodule](https://git-scm.com/book/zh/v2/Git-%E5%B7%A5%E5%85%B7-%E5%AD%90%E6%A8%A1%E5%9D%97) 形式放在 `thirdparty/Pangolin`。

**推荐（一次拉全）：**

```bash
git clone --recursive git@github.com:PU-UP/dso.git
```

**若已克隆但未带子模块：**

```bash
cd dso
git submodule update --init --recursive
```

**确认 Pangolin 在 v0.6（与 CMake 内置编译一致）：**

```bash
cd thirdparty/Pangolin
git fetch --tags
git checkout v0.6
cd ../..
```

子模块会停留在父仓库记录的提交上；一般无需改 tag，除非你在维护依赖版本。

**不推荐：** 随意执行 `git submodule update --remote thirdparty/Pangolin`，会把 Pangolin 拉到远端默认分支，可能与本工程不兼容。

## 依赖（Ubuntu 示例）

```bash
sudo apt install libsuitesparse-dev libeigen3-dev libboost-all-dev libopencv-dev
```

可选：`zlib1g-dev` + libzip（读 `images.zip`）。**GUI：** 优先用上述子模块里的 Pangolin；若系统已安装 Pangolin，CMake 会优先用系统包。子模块存在且系统未安装时，CMake 会编译 `thirdparty/Pangolin` 并链接。

## 编译

```bash
mkdir -p build && cd build
cmake ..
make -j4
```

生成静态库 `lib/libdso.a` 与可执行文件 `bin/dso_dataset`（需检测到 OpenCV）。

## 运行（数据放在 `data/`）

数据格式与 [TUM mono 数据集](https://vision.in.tum.de/mono-dataset) 一致：`files=` 指向**图像目录**或 **`images.zip`**（字母序），并配合 `calib=` 等。

在 **`build/`** 目录下示例：

```bash
./bin/dso_dataset \
  files=../data/你的序列名/images \
  calib=../data/你的序列名/camera.txt \
  gamma=../data/你的序列名/pcalib.txt \
  vignette=../data/你的序列名/vignette.png \
  preset=0 \
  mode=0

# example 
./bin/dso_dataset files=../data/sequence_08/images calib=../data/sequence_08/camera.txt gamma=../data/sequence_08/pcalib.txt vignette=../data/sequence_08/vignette.png preset=0 mode=0
```

- 无光度标定时用 `mode=1` 或 `mode=2`（见 `src/main_dso_pangolin.cpp`）。
- 无 GUI / 未编 Pangolin 时加 `nogui=1`。
- 终端示例输出：`sampleoutput=1`。
- **有 Pangolin 窗口时快捷键**（需先**点击窗口获得焦点**）：**`P` 或空格** 暂停/继续；**`Q` 或 `Esc`** 正常退出（关闭窗口后处理线程会结束并写出 `result.txt` 等，再退出进程）。

标定与全部参数以原项目及源码为准；ROS 示例见 [dso_ros](https://github.com/JakobEngel/dso_ros)。

## 许可

GPLv3，详见原仓库说明与 [官网](https://vision.in.tum.de/dso)。