#pragma once
#include <atomic>

namespace dso
{

/** Pangolin 窗口按键写入；处理线程（main_dso_pangolin 中 runthread）轮询 */
extern std::atomic<bool> g_dsoPlaybackPaused;
/** 单步播放：请求放行 1 帧（通常在暂停状态下使用）。 */
extern std::atomic<bool> g_dsoPlaybackStepRequested;
extern std::atomic<bool> g_dsoUserQuitRequested;

}
