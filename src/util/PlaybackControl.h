#pragma once
#include <atomic>

namespace dso
{

/** Pangolin 窗口按键写入；处理线程（main_dso_pangolin 中 runthread）轮询 */
extern std::atomic<bool> g_dsoPlaybackPaused;
extern std::atomic<bool> g_dsoUserQuitRequested;

}
