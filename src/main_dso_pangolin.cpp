/**
* This file is part of DSO.
* 
* Copyright 2016 Technical University of Munich and Intel.
* Developed by Jakob Engel <engelj at in dot tum dot de>,
* for more information see <http://vision.in.tum.de/dso>.
* If you use this code, please cite the respective publications as
* listed on the above website.
*
* DSO is free software: you can redistribute it and/or modify
* it under the terms of the GNU General Public License as published by
* the Free Software Foundation, either version 3 of the License, or
* (at your option) any later version.
*
* DSO is distributed in the hope that it will be useful,
* but WITHOUT ANY WARRANTY; without even the implied warranty of
* MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
* GNU General Public License for more details.
*
* You should have received a copy of the GNU General Public License
* along with DSO. If not, see <http://www.gnu.org/licenses/>.
*/



#include <thread>
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>
#include <vector>
#include <sys/time.h>
#include <unistd.h>

#include "IOWrapper/Output3DWrapper.h"
#include "IOWrapper/ImageDisplay.h"

#include "util/settings.h"
#include "util/globalFuncs.h"
#include "util/DatasetReader.h"
#include "util/globalCalib.h"

#include "util/NumType.h"
#include "FullSystem/FullSystem.h"
#include "OptimizationBackend/MatrixAccumulators.h"
#include "FullSystem/PixelSelector2.h"
#include "util/PlaybackControl.h"


#if HAS_PANGOLIN
#include "IOWrapper/Pangolin/PangolinDSOViewer.h"
#endif
#include "IOWrapper/OutputWrapper/SampleOutputWrapper.h"

namespace dso {
namespace IOWrap {
class PangolinDSOViewer;
}  // namespace IOWrap
}  // namespace dso


namespace {

struct Options {
	std::string vignette;
	std::string gamma_calib;
	std::string source;
	std::string calib;

	double rescale = 1.0;  // kept for CLI compatibility (unused in this file)
	bool reverse = false;
	int start_index = 0;
	int end_index = 100000;
	bool prefetch = false;  // kept for CLI compatibility (unused in this file)
	double playback_speed = 0.0;  // 0 => play as fast as possible
	bool preload = false;
	bool use_sample_output = false;

	int mode = 0;
};

using namespace dso;

struct PlaybackPlan {
	std::vector<int> ids;
	std::vector<double> times_to_play_at;
};

struct PlaybackStats {
	int processed_frame_count = 0;
	int first_processed_id = -1;
	int last_processed_id = -1;
	clock_t started = 0;
	clock_t ended = 0;
	struct timeval tv_start = {};
	struct timeval tv_end = {};
	double initializer_offset_sec = 0.0;
};

void SigIntHandler(int /*signum*/) {
	g_dsoUserQuitRequested.store(true, std::memory_order_relaxed);
}

void InstallSigIntHandlerOrDie() {
	struct sigaction sa;
	std::memset(&sa, 0, sizeof(sa));
	sa.sa_handler = SigIntHandler;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = 0;
	if (sigaction(SIGINT, &sa, nullptr) != 0) {
		std::perror("sigaction(SIGINT) failed");
		std::exit(1);
	}
}

void ApplyModeSettings(int mode) {
	if (mode == 0) {
		printf("PHOTOMETRIC MODE WITH CALIBRATION!\n");
		return;
	}
	if (mode == 1) {
		printf("PHOTOMETRIC MODE WITHOUT CALIBRATION!\n");
		setting_photometricCalibration = 0;
		setting_affineOptModeA = 0;  // -1: fix. >=0: optimize (with prior, if > 0).
		setting_affineOptModeB = 0;  // -1: fix. >=0: optimize (with prior, if > 0).
		return;
	}
	if (mode == 2) {
		printf("PHOTOMETRIC MODE WITH PERFECT IMAGES!\n");
		setting_photometricCalibration = 0;
		setting_affineOptModeA = -1;  // -1: fix. >=0: optimize (with prior, if > 0).
		setting_affineOptModeB = -1;  // -1: fix. >=0: optimize (with prior, if > 0).
		setting_minGradHistAdd = 3;
		return;
	}
	printf("UNKNOWN mode=%d (keeping defaults)\n", mode);
}

std::unique_ptr<ImageFolderReader> CreateReaderOrDie(const Options& options) {
	std::unique_ptr<ImageFolderReader> reader(new ImageFolderReader(
			options.source, options.calib, options.gamma_calib, options.vignette));
	reader->setGlobalCalibration();

	if (setting_photometricCalibration > 0 && reader->getPhotometricGamma() == 0) {
		printf("ERROR: dont't have photometric calibation. Need to use commandline options mode=1 or mode=2 ");
		std::exit(1);
	}

	return reader;
}

void AttachOutputs(FullSystem* full_system,
                   const std::vector<std::unique_ptr<IOWrap::Output3DWrapper>>& outputs) {
	full_system->outputWrapper.clear();
	full_system->outputWrapper.reserve(outputs.size());
	for (const auto& ow : outputs) {
		full_system->outputWrapper.push_back(ow.get());
	}
}

std::vector<std::unique_ptr<IOWrap::Output3DWrapper>> CreateOutputs(
		const Options& options,
		IOWrap::PangolinDSOViewer** viewer_out) {
	std::vector<std::unique_ptr<IOWrap::Output3DWrapper>> outputs;
	outputs.reserve(4);

#if HAS_PANGOLIN
	*viewer_out = nullptr;
	if (!disableAllDisplay) {
		std::unique_ptr<IOWrap::PangolinDSOViewer> v(
				new IOWrap::PangolinDSOViewer(wG[0], hG[0], false));
		*viewer_out = v.get();
		outputs.emplace_back(std::move(v));
	}
#else
	(void)viewer_out;
	if (!disableAllDisplay) {
		printf("WARNING: Pangolin not found at build time; running without GUI / 3D display.\n");
	}
#endif

	if (options.use_sample_output) {
		outputs.emplace_back(new IOWrap::SampleOutputWrapper());
	}

	return outputs;
}

std::unique_ptr<FullSystem> CreateFullSystem(
		ImageFolderReader& reader,
		const Options& options,
		const std::vector<std::unique_ptr<IOWrap::Output3DWrapper>>& outputs) {
	std::unique_ptr<FullSystem> full_system(new FullSystem());
	full_system->setGammaFunction(reader.getPhotometricGamma());
	full_system->linearizeOperation = (options.playback_speed == 0.0);
	AttachOutputs(full_system.get(), outputs);
	return full_system;
}

PlaybackPlan BuildPlaybackPlan(ImageFolderReader& reader, const Options& options) {
	PlaybackPlan plan;

	const int num_images = reader.getNumImages();
	int first_index = options.start_index;
	int last_index_exclusive = options.end_index;
	int index_step = 1;
	if (options.reverse) {
		printf("REVERSE!!!!");
		first_index = options.end_index - 1;
		if (first_index >= num_images) {
			first_index = num_images - 1;
		}
		last_index_exclusive = options.start_index;
		index_step = -1;
	}

	for (int i = first_index;
	     i >= 0 && i < num_images && index_step * i < index_step * last_index_exclusive;
	     i += index_step) {
		plan.ids.push_back(i);
	}

	if (plan.ids.empty() || options.playback_speed <= 0.0) {
		return plan;
	}

	plan.times_to_play_at.reserve(plan.ids.size());
	plan.times_to_play_at.push_back(0.0);
	for (size_t k = 1; k < plan.ids.size(); ++k) {
		const double ts_this = reader.getTimestamp(plan.ids[k]);
		const double ts_prev = reader.getTimestamp(plan.ids[k - 1]);
		plan.times_to_play_at.push_back(
				plan.times_to_play_at.back() + std::fabs(ts_this - ts_prev) / options.playback_speed);
	}

	return plan;
}

bool WaitUntilCanProcessNextFrame() {
	while (g_dsoPlaybackPaused.load(std::memory_order_relaxed) &&
	       !g_dsoPlaybackStepRequested.load(std::memory_order_relaxed) &&
	       !g_dsoUserQuitRequested.load(std::memory_order_relaxed)) {
		usleep(10000);
	}
	return !g_dsoUserQuitRequested.load(std::memory_order_relaxed);
}

void ConsumeStepRequest() {
	(void)g_dsoPlaybackStepRequested.exchange(false, std::memory_order_relaxed);
}

void MaybeResetStartTiming(FullSystem* full_system,
                           const PlaybackPlan& plan,
                           size_t idx,
                           PlaybackStats* stats) {
	if (full_system->initialized) {
		return;
	}
	gettimeofday(&stats->tv_start, NULL);
	stats->started = clock();
	stats->initializer_offset_sec = plan.times_to_play_at.empty() ? 0.0 : plan.times_to_play_at[idx];
}

bool ShouldSkipFrameByPacing(const Options& options,
                             const PlaybackPlan& plan,
                             size_t idx,
                             const PlaybackStats& stats) {
	if (options.playback_speed <= 0.0 || plan.times_to_play_at.empty()) {
		return false;
	}

	struct timeval tv_now;
	gettimeofday(&tv_now, NULL);
	const double since_start_sec =
			stats.initializer_offset_sec +
			((tv_now.tv_sec - stats.tv_start.tv_sec) +
			 (tv_now.tv_usec - stats.tv_start.tv_usec) / (1000.0f * 1000.0f));

	if (since_start_sec < plan.times_to_play_at[idx]) {
		double wait_sec = plan.times_to_play_at[idx] - since_start_sec;
		while (wait_sec > 0.0 && !g_dsoUserQuitRequested.load(std::memory_order_relaxed)) {
			if (!WaitUntilCanProcessNextFrame()) {
				break;
			}
			if (g_dsoPlaybackStepRequested.load(std::memory_order_relaxed)) {
				break;
			}
			const double step = (wait_sec > 0.02) ? 0.02 : wait_sec;
			usleep(static_cast<int>(step * 1000 * 1000));
			wait_sec -= step;
		}
		return false;
	}

	if (since_start_sec > plan.times_to_play_at[idx] + 0.5 + 0.1 * (idx % 2)) {
		printf("SKIPFRAME %zu (play at %f, now it is %f)!\n", idx, plan.times_to_play_at[idx],
		       since_start_sec);
		return true;
	}

	return false;
}

void PrintAndLogStatsIfAvailable(ImageFolderReader& reader, const PlaybackStats& stats) {
	if (stats.processed_frame_count <= 0 || stats.first_processed_id < 0 || stats.last_processed_id < 0) {
		return;
	}

	const double seconds_processed = std::fabs(reader.getTimestamp(stats.first_processed_id) -
	                                           reader.getTimestamp(stats.last_processed_id));
	const double ms_taken_single =
			1000.0f * (stats.ended - stats.started) / static_cast<double>(CLOCKS_PER_SEC);
	const double ms_taken_mt =
			stats.initializer_offset_sec +
			((stats.tv_end.tv_sec - stats.tv_start.tv_sec) * 1000.0f +
			 (stats.tv_end.tv_usec - stats.tv_start.tv_usec) / (1000.0f));

	const double fps = (seconds_processed > 1e-9) ? (stats.processed_frame_count / seconds_processed) : 0.0;

	printf("\n======================"
	       "\n%d Frames (%.1f fps)"
	       "\n%.2fms per frame (single core); "
	       "\n%.2fms per frame (multi core); "
	       "\n%.3fx (single core); "
	       "\n%.3fx (multi core); "
	       "\n======================\n\n",
	       stats.processed_frame_count, fps,
	       ms_taken_single / stats.processed_frame_count,
	       ms_taken_mt / stats.processed_frame_count,
	       (seconds_processed > 1e-9) ? (1000.0 / (ms_taken_single / seconds_processed)) : 0.0,
	       (seconds_processed > 1e-9) ? (1000.0 / (ms_taken_mt / seconds_processed)) : 0.0);

	if (!setting_logStuff) {
		return;
	}

	std::ofstream tmlog;
	tmlog.open("logs/time.txt", std::ios::trunc | std::ios::out);
	tmlog << 1000.0f * (stats.ended - stats.started) /
				 (float)(CLOCKS_PER_SEC * reader.getNumImages()) << " "
	      << ((stats.tv_end.tv_sec - stats.tv_start.tv_sec) * 1000.0f +
	          (stats.tv_end.tv_usec - stats.tv_start.tv_usec) / 1000.0f) /
				 (float)reader.getNumImages() << "\n";
	tmlog.flush();
	tmlog.close();
}

void RunPlaybackLoop(ImageFolderReader* reader,
                     const Options& options,
                     const std::vector<std::unique_ptr<IOWrap::Output3DWrapper>>& outputs,
                     std::unique_ptr<FullSystem>* full_system) {
	const PlaybackPlan plan = BuildPlaybackPlan(*reader, options);
	if (plan.ids.empty()) {
		printf("No frames to play. Check start/end/files.\n");
		g_dsoUserQuitRequested.store(true, std::memory_order_relaxed);
		return;
	}

	std::vector<std::unique_ptr<ImageAndExposure>> preloaded_images;
	if (options.preload) {
		printf("LOADING ALL IMAGES!\n");
		preloaded_images.reserve(plan.ids.size());
		for (int id : plan.ids) {
			preloaded_images.emplace_back(reader->getImage(id));
		}
	}

	PlaybackStats stats;
	gettimeofday(&stats.tv_start, NULL);
	stats.started = clock();

	auto ResetFullSystem = [&]() {
		printf("RESETTING!\n");
		for (const auto& ow : outputs) {
			ow->reset();
		}

		full_system->reset(new FullSystem());
		(*full_system)->setGammaFunction(reader->getPhotometricGamma());
		(*full_system)->linearizeOperation = (options.playback_speed == 0.0);
		AttachOutputs(full_system->get(), outputs);
		setting_fullResetRequested = false;
	};

	for (size_t ii = 0; ii < plan.ids.size(); ++ii) {
		if (!WaitUntilCanProcessNextFrame()) {
			break;
		}
		ConsumeStepRequest();
		MaybeResetStartTiming(full_system->get(), plan, ii, &stats);

		const int id = plan.ids[ii];

		std::unique_ptr<ImageAndExposure> img;
		if (options.preload) {
			img = std::move(preloaded_images[ii]);
		} else {
			img.reset(reader->getImage(id));
		}

		const bool skip_frame = ShouldSkipFrameByPacing(options, plan, ii, stats);
		if (g_dsoUserQuitRequested.load(std::memory_order_relaxed)) {
			break;
		}

		if (!skip_frame) {
			(*full_system)->addActiveFrame(img.get(), id);
			++stats.processed_frame_count;
			if (stats.first_processed_id < 0) {
				stats.first_processed_id = id;
			}
			stats.last_processed_id = id;
		}
		img.reset();

		if ((*full_system)->initFailed || setting_fullResetRequested) {
			if (ii < 250 || setting_fullResetRequested) {
				ResetFullSystem();
			}
		}

		if ((*full_system)->isLost) {
			printf("LOST!!\n");
			break;
		}
	}

	(*full_system)->blockUntilMappingIsFinished();
	stats.ended = clock();
	gettimeofday(&stats.tv_end, NULL);

	(*full_system)->printResult("result.txt");
	PrintAndLogStatsIfAvailable(*reader, stats);
}

void RunGuiIfEnabled(IOWrap::PangolinDSOViewer* viewer) {
#if HAS_PANGOLIN
	if (viewer != nullptr) {
		viewer->run();
	}
#else
	(void)viewer;
#endif
}

void ShutdownOutputs(std::vector<std::unique_ptr<IOWrap::Output3DWrapper>>* outputs) {
	for (auto& ow : *outputs) {
		ow->join();
	}
	outputs->clear();
}


void ApplyPresetSettings(int preset, Options* options) {
	printf("\n=============== PRESET Settings: ===============\n");
	if (preset == 0 || preset == 1) {
		printf("DEFAULT settings:\n"
				"- %s real-time enforcing\n"
				"- 2000 active points\n"
				"- 5-7 active frames\n"
				"- 1-6 LM iteration each KF\n"
				"- original image resolution\n", preset==0 ? "no " : "1x");

		options->playback_speed = (preset==0 ? 0 : 1);
		options->preload = preset==1;
		setting_desiredImmatureDensity = 1500;
		setting_desiredPointDensity = 2000;
		setting_minFrames = 5;
		setting_maxFrames = 7;
		setting_maxOptIterations=6;
		setting_minOptIterations=1;

		setting_logStuff = false;
	}

	if (preset == 2 || preset == 3) {
		printf("FAST settings:\n"
				"- %s real-time enforcing\n"
				"- 800 active points\n"
				"- 4-6 active frames\n"
				"- 1-4 LM iteration each KF\n"
				"- 424 x 320 image resolution\n", preset==0 ? "no " : "5x");

		options->playback_speed = (preset==2 ? 0 : 5);
		options->preload = preset==3;
		setting_desiredImmatureDensity = 600;
		setting_desiredPointDensity = 800;
		setting_minFrames = 4;
		setting_maxFrames = 6;
		setting_maxOptIterations=4;
		setting_minOptIterations=1;

		benchmarkSetting_width = 424;
		benchmarkSetting_height = 320;

		setting_logStuff = false;
	}

	printf("==============================================\n");
}






bool ParseArgument(const char* arg, Options* options) {
	int option_int = 0;
	float option_float = 0.0f;
	char buf[1000];

	if (1 == sscanf(arg, "sampleoutput=%d", &option_int)) {
		if (option_int == 1) {
			options->use_sample_output = true;
			printf("USING SAMPLE OUTPUT WRAPPER!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "quiet=%d", &option_int)) {
		if (option_int == 1) {
			setting_debugout_runquiet = true;
			printf("QUIET MODE, I'll shut up!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "preset=%d", &option_int)) {
		ApplyPresetSettings(option_int, options);
		return true;
	}

	if (1 == sscanf(arg, "rec=%d", &option_int)) {
		if (option_int == 0) {
			disableReconfigure = true;
			printf("DISABLE RECONFIGURE!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "noros=%d", &option_int)) {
		if (option_int == 1) {
			disableReconfigure = true;
			printf("DISABLE ROS (AND RECONFIGURE)!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "nolog=%d", &option_int)) {
		if (option_int == 1) {
			setting_logStuff = false;
			printf("DISABLE LOGGING!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "reverse=%d", &option_int)) {
		if (option_int == 1) {
			options->reverse = true;
			printf("REVERSE!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "nogui=%d", &option_int)) {
		if (option_int == 1) {
			disableAllDisplay = true;
			printf("NO GUI!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "nomt=%d", &option_int)) {
		if (option_int == 1) {
			multiThreading = false;
			printf("NO MultiThreading!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "prefetch=%d", &option_int)) {
		if (option_int == 1) {
			options->prefetch = true;
			printf("PREFETCH!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "start=%d", &option_int)) {
		options->start_index = option_int;
		printf("START AT %d!\n", options->start_index);
		return true;
	}

	if (1 == sscanf(arg, "end=%d", &option_int)) {
		options->end_index = option_int;
		printf("END AT %d!\n", options->end_index);
		return true;
	}

	if (1 == sscanf(arg, "files=%s", buf)) {
		options->source = buf;
		printf("loading data from %s!\n", options->source.c_str());
		return true;
	}

	if (1 == sscanf(arg, "calib=%s", buf)) {
		options->calib = buf;
		printf("loading calibration from %s!\n", options->calib.c_str());
		return true;
	}

	if (1 == sscanf(arg, "vignette=%s", buf)) {
		options->vignette = buf;
		printf("loading vignette from %s!\n", options->vignette.c_str());
		return true;
	}

	if (1 == sscanf(arg, "gamma=%s", buf)) {
		options->gamma_calib = buf;
		printf("loading gammaCalib from %s!\n", options->gamma_calib.c_str());
		return true;
	}

	if (1 == sscanf(arg, "rescale=%f", &option_float)) {
		options->rescale = option_float;
		printf("RESCALE %f!\n", options->rescale);
		return true;
	}

	if (1 == sscanf(arg, "speed=%f", &option_float)) {
		options->playback_speed = option_float;
		printf("PLAYBACK SPEED %f!\n", options->playback_speed);
		return true;
	}

	if (1 == sscanf(arg, "save=%d", &option_int)) {
		if (option_int == 1) {
			debugSaveImages = true;
			const int rm_ret = std::system("rm -rf images_out");
			const int mkdir_ret = std::system("mkdir -p images_out");
			(void)rm_ret;
			(void)mkdir_ret;
			printf("SAVE IMAGES!\n");
		}
		return true;
	}

	if (1 == sscanf(arg, "mode=%d", &option_int)) {
		options->mode = option_int;
		ApplyModeSettings(option_int);
		return true;
	}

	return false;
}

Options ParseArgsOrDie(int argc, char** argv) {
	Options options;
	for (int i = 1; i < argc; ++i) {
		if (!ParseArgument(argv[i], &options)) {
			printf("could not parse argument \"%s\"!!!!\n", argv[i]);
		}
	}
	return options;
}

}  // namespace



int main(int argc, char** argv) {
	const Options options = ParseArgsOrDie(argc, argv);
	InstallSigIntHandlerOrDie();

	std::unique_ptr<ImageFolderReader> reader = CreateReaderOrDie(options);

	IOWrap::PangolinDSOViewer* viewer = nullptr;
	std::vector<std::unique_ptr<IOWrap::Output3DWrapper>> outputs =
			CreateOutputs(options, &viewer);

	std::unique_ptr<FullSystem> full_system = CreateFullSystem(*reader, options, outputs);

	// to make MacOS happy: run this in dedicated thread -- and use this one to run the GUI.
	std::thread playback_thread([&]() {
		RunPlaybackLoop(reader.get(), options, outputs, &full_system);
	});

	RunGuiIfEnabled(viewer);
	playback_thread.join();

	ShutdownOutputs(&outputs);

	printf("DELETE FULLSYSTEM!\n");
	full_system.reset();

	printf("DELETE READER!\n");
	reader.reset();

	printf("EXIT NOW!\n");
	return 0;
}
