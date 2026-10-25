cmake_minimum_required(VERSION 3.15)
set(PICO_BOARD pico2_w CACHE STRING "Pico 2 W")
if(NOT DEFINED PICO_SDK_PATH AND EXISTS "${CMAKE_CURRENT_LIST_DIR}/pico/pico-sdk/external/pico_sdk_import.cmake")
    set(PICO_SDK_PATH "${CMAKE_CURRENT_LIST_DIR}/pico/pico-sdk")
endif()
include(pico_sdk_import.cmake OPTIONAL RESULT_VARIABLE PICO_SDK_IMPORT_LOCAL)
if(NOT PICO_SDK_IMPORT_LOCAL)
    if(NOT DEFINED PICO_SDK_PATH)
        message(FATAL_ERROR "Set PICO_SDK_PATH to your pico-sdk directory in CLion CMake options")
    endif()
    include("${PICO_SDK_PATH}/external/pico_sdk_import.cmake")
endif()
project(oxhack C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()
add_executable(oxhack main.c src/wifi.c src/mqtt_client.c src/converter.c src/power_stage.c
    )
target_include_directories(oxhack PRIVATE "${CMAKE_CURRENT_LIST_DIR}" "${CMAKE_CURRENT_LIST_DIR}/include")
target_link_libraries(oxhack PRIVATE pico_stdlib pico_cyw43_arch_lwip_poll pico_lwip_mqtt hardware_pwm hardware_clocks)
target_compile_definitions(oxhack PRIVATE
    MQTT_BROKER_HOST=\"broker.emqx.io\"
    V2G_TOPIC_PREFIX=\"v2g/oxhack-4000\"
    MQTT_CLIENT_ID=\"oxhack-pico2w-4000\"
)
pico_enable_stdio_usb(oxhack 1)
pico_enable_stdio_uart(oxhack 0)
pico_add_extra_outputs(oxhack)
