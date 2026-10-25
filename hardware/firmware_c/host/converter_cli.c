// Host harness: drive the REAL src/converter.c from stdin, so the whole MQTT chain
// (optimiser schedule -> sender -> receive_data.py relay -> setpoint topic) can be checked on a
// laptop without a Pico or Wokwi. host/pico_stand_in.py is the MQTT subscriber that feeds it.
//
//   C <ms> <json>   call converter_handle_command(json, ms), then converter_update(ms)
//   T <ms>          converter_update(ms) only (a clock tick)
// One status line per input line:  S <ms> <accepted:-1|0|1> <mode> <requested_kw> <actual_kw> <soc>
// (converter.c also prints its own "[converter] ..." lines; readers skip anything not starting "S ")
#include "converter.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    char line[512];
    converter_init(0);
    while (fgets(line, sizeof line, stdin)) {
        line[strcspn(line, "\r\n")] = '\0';
        char kind = 0; unsigned long ms = 0; int n = 0;
        if (sscanf(line, "%c %lu %n", &kind, &ms, &n) < 2) continue;
        int accepted = -1;
        if (kind == 'C') accepted = converter_handle_command(line + n, (uint32_t)ms) ? 1 : 0;
        converter_update((uint32_t)ms, true);                 // MQTT session assumed up
        v2g_status_t s = converter_get_status();
        printf("S %lu %d %s %.3f %.3f %.4f\n", ms, accepted, converter_mode_name(s.mode),
               (double)s.requested_power_kw, (double)s.actual_power_kw, (double)s.battery_soc);
        fflush(stdout);
    }
    return 0;
}
