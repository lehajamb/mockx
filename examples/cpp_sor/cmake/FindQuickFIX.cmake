include(FindPackageHandleStandardArgs)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(PC_QUICKFIX QUIET quickfix)
endif()

find_path(
    QuickFIX_INCLUDE_DIR
    NAMES quickfix/Application.h
    HINTS ${QUICKFIX_ROOT} ENV QUICKFIX_ROOT ${PC_QUICKFIX_INCLUDE_DIRS}
    PATH_SUFFIXES include
)

find_library(
    QuickFIX_LIBRARY_RELEASE
    NAMES quickfix
    HINTS ${QUICKFIX_ROOT} ENV QUICKFIX_ROOT ${PC_QUICKFIX_LIBRARY_DIRS}
    PATH_SUFFIXES lib
)

find_library(
    QuickFIX_LIBRARY_DEBUG
    NAMES quickfixd quickfix
    HINTS ${QUICKFIX_ROOT} ENV QUICKFIX_ROOT ${PC_QUICKFIX_LIBRARY_DIRS}
    PATH_SUFFIXES lib debug/lib lib/debug
)

if(NOT QuickFIX_LIBRARY_RELEASE)
    set(QuickFIX_LIBRARY_RELEASE ${QuickFIX_LIBRARY_DEBUG})
endif()

find_package_handle_standard_args(
    QuickFIX
    REQUIRED_VARS QuickFIX_INCLUDE_DIR QuickFIX_LIBRARY_RELEASE
    FAIL_MESSAGE
        "QuickFIX C++ was not found. Install it with vcpkg or from quickfix/quickfix, use the vcpkg toolchain, or set QUICKFIX_ROOT."
)

if(QuickFIX_FOUND AND NOT TARGET QuickFIX::QuickFIX)
    add_library(QuickFIX::QuickFIX UNKNOWN IMPORTED)
    set_target_properties(
        QuickFIX::QuickFIX
        PROPERTIES
            IMPORTED_LOCATION "${QuickFIX_LIBRARY_RELEASE}"
            INTERFACE_INCLUDE_DIRECTORIES "${QuickFIX_INCLUDE_DIR}"
            MAP_IMPORTED_CONFIG_MINSIZEREL RELEASE
            MAP_IMPORTED_CONFIG_RELWITHDEBINFO RELEASE
    )
    if(QuickFIX_LIBRARY_DEBUG)
        set_property(TARGET QuickFIX::QuickFIX PROPERTY IMPORTED_CONFIGURATIONS "DEBUG;RELEASE")
        set_target_properties(
            QuickFIX::QuickFIX
            PROPERTIES
                IMPORTED_LOCATION_DEBUG "${QuickFIX_LIBRARY_DEBUG}"
                IMPORTED_LOCATION_RELEASE "${QuickFIX_LIBRARY_RELEASE}"
        )
    endif()
    if(PC_QUICKFIX_LINK_LIBRARIES)
        set_property(
            TARGET QuickFIX::QuickFIX
            PROPERTY INTERFACE_LINK_LIBRARIES "${PC_QUICKFIX_LINK_LIBRARIES}"
        )
    elseif(WIN32)
        set_property(
            TARGET QuickFIX::QuickFIX
            PROPERTY INTERFACE_LINK_LIBRARIES "ws2_32;crypt32"
        )
    else()
        find_package(Threads REQUIRED)
        set_property(
            TARGET QuickFIX::QuickFIX
            PROPERTY INTERFACE_LINK_LIBRARIES Threads::Threads
        )
    endif()
endif()

mark_as_advanced(
    QuickFIX_INCLUDE_DIR
    QuickFIX_LIBRARY_RELEASE
    QuickFIX_LIBRARY_DEBUG
)
