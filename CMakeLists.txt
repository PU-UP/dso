SET(PROJECT_NAME DSO)

PROJECT(${PROJECT_NAME})
CMAKE_MINIMUM_REQUIRED(VERSION 2.6)
#set(CMAKE_VERBOSE_MAKEFILE ON)


set(BUILD_TYPE Release)
#set(BUILD_TYPE RelWithDebInfo)

set(EXECUTABLE_OUTPUT_PATH bin)
set(LIBRARY_OUTPUT_PATH lib)
set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake)

# required libraries
find_package(SuiteParse REQUIRED)
find_package(Eigen3 REQUIRED)
find_package(Boost COMPONENTS system thread)
find_package(Threads REQUIRED)
if(TARGET Threads::Threads)
	set(DSO_THREAD_LIBS Threads::Threads)
else()
	set(DSO_THREAD_LIBS ${CMAKE_THREAD_LIBS_INIT})
endif()

# optional libraries
find_package(LibZip QUIET)

# Pangolin: 优先用 thirdparty（子模块），避免 find_package 误判为已找到但系统实际未安装 libpangolin（链接报 -lpangolin 找不到）
set(DSO_PANGOLIN_TARGET "")
if(EXISTS "${PROJECT_SOURCE_DIR}/thirdparty/Pangolin/CMakeLists.txt")
	message("--- using bundled thirdparty/Pangolin.")
	set(BUILD_TESTS OFF CACHE BOOL "DSO: skip Pangolin tests" FORCE)
	set(BUILD_TOOLS OFF CACHE BOOL "DSO: skip Pangolin tools" FORCE)
	set(BUILD_EXAMPLES OFF CACHE BOOL "DSO: skip Pangolin examples" FORCE)
	set(BUILD_PYPANGOLIN_MODULE OFF CACHE BOOL "DSO: skip Pangolin Python module" FORCE)
	add_subdirectory("${PROJECT_SOURCE_DIR}/thirdparty/Pangolin" "${CMAKE_BINARY_DIR}/thirdparty_pangolin")
	set(Pangolin_FOUND TRUE)
	set(DSO_PANGOLIN_TARGET pangolin)
else()
	find_package(Pangolin 0.2 QUIET)
endif()

find_package(OpenCV QUIET)

# flags
add_definitions("-DENABLE_SSE")
set(CMAKE_CXX_FLAGS
   "${SSE_FLAGS} -O3 -g -std=c++0x -march=native"
#   "${SSE_FLAGS} -O3 -g -std=c++0x -fno-omit-frame-pointer"
)

if (MSVC)
     set (CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} /EHsc")
endif (MSVC)

# Sources files
set(dso_SOURCE_FILES
  ${PROJECT_SOURCE_DIR}/src/FullSystem/FullSystem.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/FullSystemOptimize.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/FullSystemOptPoint.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/FullSystemDebugStuff.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/FullSystemMarginalize.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/Residuals.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/CoarseTracker.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/CoarseInitializer.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/ImmaturePoint.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/HessianBlocks.cpp
  ${PROJECT_SOURCE_DIR}/src/FullSystem/PixelSelector2.cpp
  ${PROJECT_SOURCE_DIR}/src/OptimizationBackend/EnergyFunctional.cpp
  ${PROJECT_SOURCE_DIR}/src/OptimizationBackend/AccumulatedTopHessian.cpp
  ${PROJECT_SOURCE_DIR}/src/OptimizationBackend/AccumulatedSCHessian.cpp
  ${PROJECT_SOURCE_DIR}/src/OptimizationBackend/EnergyFunctionalStructs.cpp
  ${PROJECT_SOURCE_DIR}/src/util/settings.cpp
  ${PROJECT_SOURCE_DIR}/src/util/Undistort.cpp
  ${PROJECT_SOURCE_DIR}/src/util/globalCalib.cpp
  ${PROJECT_SOURCE_DIR}/src/util/PlaybackControl.cpp
)



include_directories(
  ${PROJECT_SOURCE_DIR}/src
  ${PROJECT_SOURCE_DIR}/thirdparty/Sophus
  ${PROJECT_SOURCE_DIR}/thirdparty/sse2neon
  ${EIGEN3_INCLUDE_DIR}
) 


# decide if we have pangolin (system find_package and/or bundled add_subdirectory above)
if (Pangolin_FOUND)
	if(DSO_PANGOLIN_TARGET)
		message("--- using bundled PANGOLIN, compiling dso_pangolin library.")
	else()
		message("--- found system PANGOLIN, compiling dso_pangolin library.")
		include_directories( ${Pangolin_INCLUDE_DIRS} )
	endif()
	set(dso_pangolin_SOURCE_FILES 
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/Pangolin/KeyFrameDisplay.cpp
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/Pangolin/PangolinDSOViewer.cpp)
	set(HAS_PANGOLIN 1)
else ()
	message("--- could not find PANGOLIN (no system package and no thirdparty/Pangolin), not compiling dso_pangolin library.")
	message("    this means there will be no 3D display / GUI available for dso_dataset.")
	set(dso_pangolin_SOURCE_FILES )
	set(HAS_PANGOLIN 0)
endif ()

# decide if we have openCV
if (OpenCV_FOUND)
	message("--- found OpenCV, compiling dso_opencv library.")
	include_directories( ${OpenCV_INCLUDE_DIRS} )
	set(dso_opencv_SOURCE_FILES 
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/OpenCV/ImageDisplay_OpenCV.cpp
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/OpenCV/ImageRW_OpenCV.cpp)
	set(HAS_OPENCV 1)
else ()
	message("--- could not find OpenCV, not compiling dso_opencv library.")
	message("    this means there will be no image display, and image read / load functionality.")
	set(dso_opencv_SOURCE_FILES 
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/ImageDisplay_dummy.cpp
	  ${PROJECT_SOURCE_DIR}/src/IOWrapper/ImageRW_dummy.cpp)
	set(HAS_OPENCV 0)
endif ()

# decide if we have ziplib.
if (LIBZIP_LIBRARY)
	message("--- found ziplib (${LIBZIP_VERSION}), compiling with zip capability.")
	add_definitions(-DHAS_ZIPLIB=1)
	include_directories( ${LIBZIP_INCLUDE_DIR_ZIP} ${LIBZIP_INCLUDE_DIR_ZIPCONF} ) 
else()
	message("--- not found ziplib (${LIBZIP_LIBRARY}), compiling without zip capability.")
	set(LIBZIP_LIBRARY "")
endif()


# compile main library.
include_directories( ${CSPARSE_INCLUDE_DIR} ${CHOLMOD_INCLUDE_DIR}) 
add_library(dso ${dso_SOURCE_FILES} ${dso_opencv_SOURCE_FILES} ${dso_pangolin_SOURCE_FILES})
target_compile_definitions(dso PUBLIC HAS_PANGOLIN=${HAS_PANGOLIN} HAS_OPENCV=${HAS_OPENCV})
if(HAS_PANGOLIN)
	if(DSO_PANGOLIN_TARGET)
		target_link_libraries(dso PRIVATE ${DSO_PANGOLIN_TARGET})
	endif()
endif()

#set_property( TARGET dso APPEND_STRING PROPERTY COMPILE_FLAGS -Wall )


if (${CMAKE_SYSTEM_NAME} MATCHES "Darwin") # OSX
    set(BOOST_THREAD_LIBRARY boost_thread-mt)
else()
    set(BOOST_THREAD_LIBRARY boost_thread)
endif()

# build main executable (requires OpenCV; Pangolin optional)
if (OpenCV_FOUND)
	message("--- compiling dso_dataset (Pangolin optional).")
	add_executable(dso_dataset ${PROJECT_SOURCE_DIR}/src/main_dso_pangolin.cpp )
	if (Pangolin_FOUND)
		if(DSO_PANGOLIN_TARGET)
			target_link_libraries(dso_dataset dso boost_system cxsparse ${BOOST_THREAD_LIBRARY} ${LIBZIP_LIBRARY} ${DSO_PANGOLIN_TARGET} ${OpenCV_LIBS} ${DSO_THREAD_LIBS})
		else()
			target_link_libraries(dso_dataset dso boost_system cxsparse ${BOOST_THREAD_LIBRARY} ${LIBZIP_LIBRARY} ${Pangolin_LIBRARIES} ${OpenCV_LIBS} ${DSO_THREAD_LIBS})
		endif()
	else()
		target_link_libraries(dso_dataset dso boost_system cxsparse ${BOOST_THREAD_LIBRARY} ${LIBZIP_LIBRARY} ${OpenCV_LIBS} ${DSO_THREAD_LIBS})
	endif()
else()
	message("--- not building dso_dataset, since don't have OpenCV.")
endif()

