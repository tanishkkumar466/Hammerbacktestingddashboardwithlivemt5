# Hammer headless MT5 engine (Windows / C++)
#
# Build (Developer Command Prompt for VS):
#   cd API\engine
#   cmake -B build -G "Visual Studio 17 2022" -A x64
#   cmake --build build --config Release
#   .\build\Release\hammer_mt5_engine.exe
#
# Listens on 127.0.0.1:17101 — Python UI only talks here (no Python MT5 launch).

cmake_minimum_required(VERSION 3.16)
project(hammer_mt5_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(hammer_mt5_engine
    main.cpp
    mt5_path.cpp
    launcher.cpp
    http_mini.cpp
    orders.cpp
)

target_include_directories(hammer_mt5_engine PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

if(WIN32)
    target_compile_definitions(hammer_mt5_engine PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_link_libraries(hammer_mt5_engine PRIVATE ws2_32 advapi32 shell32)
endif()

find_package(Threads REQUIRED)
target_link_libraries(hammer_mt5_engine PRIVATE Threads::Threads)
