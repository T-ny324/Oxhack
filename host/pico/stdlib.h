// Host stand-in for the Pico SDK header: lets the REAL main.c run on a PC
// with a virtual clock. Not used in the Pico/Wokwi build.
#ifndef HOST_PICO_STDLIB_H
#define HOST_PICO_STDLIB_H
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
typedef uint64_t absolute_time_t;
extern uint32_t host_now_ms;
static inline absolute_time_t get_absolute_time(void) { return (uint64_t)host_now_ms * 1000u; }
static inline uint32_t to_ms_since_boot(absolute_time_t t) { return (uint32_t)(t / 1000u); }
void sleep_ms(uint32_t ms);
static inline void stdio_init_all(void) {}
#endif
