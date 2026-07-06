cmake_minimum_required(VERSION 3.20)

project(SGGKTestHarness LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(SGGK_SDK_DIR "${CMAKE_SOURCE_DIR}/../SGK1.4.10/SGGK" CACHE PATH "Path to the SGGK SDK root")

if(NOT EXISTS "${SGGK_SDK_DIR}/include/Foundation/init.h")
    message(FATAL_ERROR "SGGK_SDK_DIR does not point to a valid SGGK SDK: ${SGGK_SDK_DIR}")
endif()

add_executable(sggk_case_runner
    src/sggk_case_runner.cpp
)

add_executable(sggk_topology_extract
    src/sggk_topology_extract.cpp
)

target_include_directories(sggk_case_runner PRIVATE
    "${SGGK_SDK_DIR}/include"
)

target_include_directories(sggk_topology_extract PRIVATE
    "${SGGK_SDK_DIR}/include"
)

target_compile_options(sggk_case_runner PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:/utf-8>
)

target_compile_options(sggk_topology_extract PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:/utf-8>
)

target_link_directories(sggk_case_runner PRIVATE
    "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/libd,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/lib,${SGGK_SDK_DIR}/x64-win/libw>>"
)

target_link_directories(sggk_topology_extract PRIVATE
    "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/libd,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/lib,${SGGK_SDK_DIR}/x64-win/libw>>"
)

target_link_libraries(sggk_case_runner PRIVATE
    SGGK_Foundation
    SGGK_Math
    SGGK_GeomBase
    SGGK_Geometry
    SGGK_GeomAlgo
    SGGK_BSplineAlgo
    SGGK_GeomInt
    SGGK_GeomProject
    SGGK_Topology
    SGGK_ModelAnalysis
    SGGK_ModelingBase
    SGGK_ModelingPrim
    SGGK_Heal
    SGGK_StepExchange
    SGGK_IgesExchange
    SGGK_Boolean
    SGGK_Offset
    SGGK_HLR
)

target_link_libraries(sggk_topology_extract PRIVATE
    SGGK_Foundation
    SGGK_Math
    SGGK_GeomBase
    SGGK_Geometry
    SGGK_Topology
)

add_custom_command(TARGET sggk_case_runner POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/bind,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/bin,${SGGK_SDK_DIR}/x64-win/binw>>"
        "$<TARGET_FILE_DIR:sggk_case_runner>"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SGGK_SDK_DIR}/sggk.lic"
        "$<TARGET_FILE_DIR:sggk_case_runner>/sggk.lic"
)

add_custom_command(TARGET sggk_topology_extract POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/bind,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/bin,${SGGK_SDK_DIR}/x64-win/binw>>"
        "$<TARGET_FILE_DIR:sggk_topology_extract>"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SGGK_SDK_DIR}/sggk.lic"
        "$<TARGET_FILE_DIR:sggk_topology_extract>/sggk.lic"
)
