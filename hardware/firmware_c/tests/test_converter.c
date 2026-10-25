#include "converter.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
static void near(float actual, float want) { assert(fabsf(actual - want) < 0.02f); }
int main(void) {
    converter_init(0);
    assert(converter_get_status().mode == V2G_STOPPED);
    assert(!converter_handle_command("SET_POWER 8.0", 0));
    assert(!converter_handle_command("SET_POWER -8.0", 0));
    assert(!converter_handle_command("SET_POWER nan", 0));
    assert(!converter_handle_command("DISCHARGE 3", 0));
    assert(converter_handle_command("{\"command\":\"SET_POWER\",\"mode\":\"CHARGE\",\"power_kw\":3.0}", 0));
    converter_update(100, true);
    near(converter_get_status().actual_power_kw, +0.5f);
    assert(converter_get_status().mode == V2G_CHARGING);
    assert(converter_handle_command("{\"command\":\"SET_POWER\",\"mode\":\"DISCHARGE\",\"power_kw\":2.0}", 100));
    converter_update(200, true);
    near(converter_get_status().actual_power_kw, 0.0f);
    converter_update(300, true);
    near(converter_get_status().actual_power_kw, -0.5f);
    assert(converter_get_status().mode == V2G_DISCHARGING);
    converter_update(5300, true);
    near(converter_get_status().requested_power_kw, 0.0f);
    assert(converter_handle_command("{\"command\":\"SET_POWER\",\"mode\":\"STOP\",\"power_kw\":0.0}", 5400));
    near(converter_get_status().requested_power_kw, 0.0f);
    assert(converter_handle_command("{\"command\":\"SET_POWER\",\"mode\":\"CHARGE\",\"power_kw\":1.0}", 5450));
    converter_update(5550, false);
    near(converter_get_status().requested_power_kw, 0.0f);
    converter_trip_fault();
    assert(converter_get_status().mode == V2G_FAULT);
    assert(!converter_handle_command("{\"command\":\"SET_POWER\",\"mode\":\"CHARGE\",\"power_kw\":1.0}", 5600));
    puts("V2G converter simulation tests passed");
}
