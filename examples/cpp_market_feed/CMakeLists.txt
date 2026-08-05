cmake_minimum_required(VERSION 3.20)

project(mockx_market_feed VERSION 0.1.0 LANGUAGES CXX)

option(MOCKX_FEED_ENABLE_SANITIZERS "Enable Address/Undefined sanitizers" OFF)
option(
    MOCKX_FEED_ENABLE_LIVE_TEST
    "Add an end-to-end test that requires the MockX Python environment"
    OFF
)

find_package(Threads REQUIRED)

add_library(mockx_feed_core
    src/app.cpp
    src/decoder.cpp
    src/feed_handler.cpp
    src/glimpse.cpp
    src/net.cpp
    src/order_book.cpp
    src/renderer.cpp
    src/soup.cpp
)
target_include_directories(mockx_feed_core PUBLIC include)
target_compile_features(mockx_feed_core PUBLIC cxx_std_20)
target_link_libraries(mockx_feed_core PUBLIC Threads::Threads)

if(WIN32)
    target_compile_definitions(
        mockx_feed_core
        PUBLIC WIN32_LEAN_AND_MEAN NOMINMAX
        PRIVATE _CRT_SECURE_NO_WARNINGS
    )
    target_link_libraries(mockx_feed_core PUBLIC ws2_32)
endif()

if(MSVC)
    # C4324 reports the intentional cache-line padding on ring cursors.
    target_compile_options(mockx_feed_core PRIVATE /W4 /wd4324 /permissive- /EHsc)
else()
    target_compile_options(mockx_feed_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
    if(MOCKX_FEED_ENABLE_SANITIZERS)
        target_compile_options(mockx_feed_core PUBLIC -fsanitize=address,undefined)
        target_link_options(mockx_feed_core PUBLIC -fsanitize=address,undefined)
    endif()
endif()

if(PROJECT_IS_TOP_LEVEL)
    add_executable(mockx_market_feed src/main.cpp)
    target_link_libraries(mockx_market_feed PRIVATE mockx_feed_core)

    include(CTest)
    if(BUILD_TESTING)
        add_executable(mockx_feed_tests tests/test_main.cpp)
        target_link_libraries(mockx_feed_tests PRIVATE mockx_feed_core)
        if(MSVC)
            target_compile_options(mockx_feed_tests PRIVATE /W4 /wd4324 /permissive- /EHsc)
        else()
            target_compile_options(mockx_feed_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
        endif()
        add_test(NAME mockx_feed_tests COMMAND mockx_feed_tests)

        if(MOCKX_FEED_ENABLE_LIVE_TEST)
            find_package(Python3 REQUIRED COMPONENTS Interpreter)
            add_test(
                NAME mockx_feed_live_smoke
                COMMAND
                    ${Python3_EXECUTABLE}
                    ${CMAKE_CURRENT_SOURCE_DIR}/tests/live_smoke.py
                    $<TARGET_FILE:mockx_market_feed>
            )
            set_tests_properties(mockx_feed_live_smoke PROPERTIES TIMEOUT 20)
        endif()
    endif()
endif()
