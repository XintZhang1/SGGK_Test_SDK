cmake_minimum_required(VERSION 3.20)

project(SGGKTestHarness LANGUAGES CXX)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(SGGK_SDK_DIR "$ENV{SGGK_SDK_DIR}" CACHE PATH "Path to the SGGK SDK root")

if(NOT EXISTS "${SGGK_SDK_DIR}/include/Foundation/init.h")
    message(FATAL_ERROR
        "SGGK_SDK_DIR does not point to a valid SGGK SDK: '${SGGK_SDK_DIR}'. "
        "Set the SGGK_SDK_DIR environment variable or pass -DSGGK_SDK_DIR=<path>."
    )
endif()

set(SGGK_PLUGIN_GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated_plugins")
file(GLOB_RECURSE SGGK_PLUGIN_INPUTS CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/api_plugins/*"
)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/tools/generate_plugin_registry.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/tools/plugin_catalog.py"
)
execute_process(
    COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/tools/generate_plugin_registry.py"
        --plugin-root "${CMAKE_CURRENT_SOURCE_DIR}/api_plugins"
        --out "${SGGK_PLUGIN_GENERATED_DIR}"
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/.."
    RESULT_VARIABLE SGGK_PLUGIN_GENERATOR_RESULT
    OUTPUT_VARIABLE SGGK_PLUGIN_GENERATOR_STDOUT
    ERROR_VARIABLE SGGK_PLUGIN_GENERATOR_STDERR
)
if(NOT SGGK_PLUGIN_GENERATOR_RESULT EQUAL 0)
    message(FATAL_ERROR
        "SGGK plugin registry generation failed (${SGGK_PLUGIN_GENERATOR_RESULT}).\n"
        "stdout: ${SGGK_PLUGIN_GENERATOR_STDOUT}\n"
        "stderr: ${SGGK_PLUGIN_GENERATOR_STDERR}"
    )
endif()
include("${SGGK_PLUGIN_GENERATED_DIR}/generated_plugins.cmake")

add_executable(sggk_case_runner
    src/sggk_case_runner.cpp
)

add_executable(sggk_topology_extract
    src/sggk_topology_extract.cpp
)

add_executable(sggk_mesh_dump
    src/sggk_mesh_dump.cpp
)

target_include_directories(sggk_case_runner PRIVATE
    "${SGGK_SDK_DIR}/include"
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${SGGK_PLUGIN_GENERATED_DIR}"
)

target_include_directories(sggk_topology_extract PRIVATE
    "${SGGK_SDK_DIR}/include"
)

target_include_directories(sggk_mesh_dump PRIVATE
    "${SGGK_SDK_DIR}/include"
)

target_compile_options(sggk_case_runner PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:/utf-8>
)

target_compile_options(sggk_topology_extract PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:/utf-8>
)

target_compile_options(sggk_mesh_dump PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:/utf-8>
)

target_link_directories(sggk_case_runner PRIVATE
    "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/libd,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/lib,${SGGK_SDK_DIR}/x64-win/libw>>"
)

target_link_directories(sggk_topology_extract PRIVATE
    "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/libd,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/lib,${SGGK_SDK_DIR}/x64-win/libw>>"
)

target_link_directories(sggk_mesh_dump PRIVATE
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
    ${SGGK_PLUGIN_LIBRARIES}
)

target_link_libraries(sggk_topology_extract PRIVATE
    SGGK_Foundation
    SGGK_Math
    SGGK_GeomBase
    SGGK_Geometry
    SGGK_Topology
)

target_link_libraries(sggk_mesh_dump PRIVATE
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

add_custom_command(TARGET sggk_mesh_dump POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "$<IF:$<CONFIG:Debug>,${SGGK_SDK_DIR}/x64-win/bind,$<IF:$<CONFIG:Release>,${SGGK_SDK_DIR}/x64-win/bin,${SGGK_SDK_DIR}/x64-win/binw>>"
        "$<TARGET_FILE_DIR:sggk_mesh_dump>"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SGGK_SDK_DIR}/sggk.lic"
        "$<TARGET_FILE_DIR:sggk_mesh_dump>/sggk.lic"
)
