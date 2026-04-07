#include "util/PlaybackControl.h"

namespace dso
{

std::atomic<bool> g_dsoPlaybackPaused(false);
std::atomic<bool> g_dsoUserQuitRequested(false);

}
