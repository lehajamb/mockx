cmake_minimum_required(VERSION 3.20)

project(mockx_sor VERSION 0.1.0 LANGUAGES CXX)

if(POLICY CMP0144)
    cmake_policy(SET CMP0144 NEW)
endif()

option(MOCKX_SOR_ENABLE_SANITIZERS "Enable Address/Undefined sanitizers" OFF)
option(
    MOCKX_SOR_ENABLE_LIVE_TEST
    "Add an end-to-end test that requires the MockX Python environment"
    OFF
)

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

find_package(quickfix CONFIG QUIET)
foreach(quickfix_target IN ITEMS quickfix quickfix::quickfix QuickFIX::quickfix)
    if(TARGET ${quickfix_target} AND NOT TARGET QuickFIX::QuickFIX)
        add_library(QuickFIX::QuickFIX ALIAS ${quickfix_target})
    endif()
endforeach()
if(NOT TARGET QuickFIX::QuickFIX)
    find_package(QuickFIX REQUIRED MODULE)
endif()

if(MOCKX_SOR_ENABLE_SANITIZERS)
    set(MOCKX_FEED_ENABLE_SANITIZERS ON CACHE BOOL "" FORCE)
endif()
add_subdirectory(
    "${CMAKE_CURRENT_SOURCE_DIR}/../cpp_market_feed"
    "${CMAKE_CURRENT_BINARY_DIR}/cpp_market_feed"
)

add_library(mockx_sor_core
    src/decimal.cpp
    src/router.cpp
)
target_include_directories(mockx_sor_core PUBLIC include)
target_compile_features(mockx_sor_core PUBLIC cxx_std_20)
target_link_libraries(mockx_sor_core PUBLIC mockx_feed_core)

add_library(mockx_sor_fix
    src/app.cpp
    src/config.cpp
    src/fix_gateway.cpp
)
target_include_directories(mockx_sor_fix PUBLIC include)
target_compile_features(mockx_sor_fix PUBLIC cxx_std_20)
target_link_libraries(mockx_sor_fix PUBLIC mockx_sor_core QuickFIX::QuickFIX)

if(MSVC)
    target_compile_definitions(mockx_sor_fix PUBLIC WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(mockx_sor_core PRIVATE /W4 /wd4324 /permissive- /EHsc)
    target_compile_options(mockx_sor_fix PRIVATE /W4 /wd4324 /permissive- /EHsc)
else()
    target_compile_options(mockx_sor_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
    target_compile_options(mockx_sor_fix PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
    if(MOCKX_SOR_ENABLE_SANITIZERS)
        target_compile_options(mockx_sor_core PUBLIC -fsanitize=address,undefined)
        target_link_options(mockx_sor_core PUBLIC -fsanitize=address,undefined)
        target_compile_options(mockx_sor_fix PUBLIC -fsanitize=address,undefined)
        target_link_options(mockx_sor_fix PUBLIC -fsanitize=address,undefined)
    endif()
endif()

add_executable(mockx_sor src/main.cpp)
target_link_libraries(mockx_sor PRIVATE mockx_sor_fix)

include(CTest)
if(BUILD_TESTING)
    add_executable(mockx_sor_core_tests tests/test_core.cpp)
    target_link_libraries(mockx_sor_core_tests PRIVATE mockx_sor_core)
    add_test(NAME mockx_sor_core_tests COMMAND mockx_sor_core_tests)

    add_executable(mockx_sor_fix_tests tests/test_fix.cpp)
    target_link_libraries(mockx_sor_fix_tests PRIVATE mockx_sor_fix)
    target_compile_definitions(
        mockx_sor_fix_tests
        PRIVATE MOCKX_SOR_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
    )
    add_test(NAME mockx_sor_fix_tests COMMAND mockx_sor_fix_tests)

    if(MSVC)
        target_compile_options(mockx_sor_core_tests PRIVATE /W4 /wd4324 /permissive- /EHsc)
        target_compile_options(mockx_sor_fix_tests PRIVATE /W4 /wd4324 /permissive- /EHsc)
    else()
        target_compile_options(mockx_sor_core_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
        target_compile_options(mockx_sor_fix_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
    endif()

    if(MOCKX_SOR_ENABLE_LIVE_TEST)
        find_package(Python3 REQUIRED COMPONENTS Interpreter)
        add_test(
            NAME mockx_sor_live_smoke
            COMMAND
                ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/tests/live_smoke.py
                $<TARGET_FILE:mockx_sor>
        )
        set_tests_properties(mockx_sor_live_smoke PROPERTIES TIMEOUT 30)
    endif()
endif()
