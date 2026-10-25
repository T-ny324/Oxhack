#include "power_stage.h"

#include <math.h>
#include <stdio.h>

#include "pico/stdlib.h"
#include "hardware/pwm.h"
#include "hardware/clocks.h"

// Logic-level demonstration: 1 kHz PWM through LEDs/logic analyser.
// The 7.4 kW number is a SOFTWARE NORMALISATION ONLY, not physical power.
#define DEMO_POWER_BASE_KW 7.4f
#define DEMO_PWM_HZ 1000u
#define PWM_WRAP 999u
#define NEUTRAL_MS 30u
#define DUTY_MAX_PERCENT 80.0f
#define SMALL_POWER_KW 0.001f

static bool started;
static int output_direction;      // -1 discharge, 0 neutral, +1 charge
static int requested_direction;
static uint32_t neutral_since_ms;

static void pwm_channel_init(uint gpio) {
    gpio_set_function(gpio, GPIO_FUNC_PWM);
    uint slice = pwm_gpio_to_slice_num(gpio);
    pwm_config cfg = pwm_get_default_config();
    pwm_config_set_wrap(&cfg, PWM_WRAP);
    float divider = (float)clock_get_hz(clk_sys) /
                    ((float)DEMO_PWM_HZ * (PWM_WRAP + 1u));
    // RP2350 default clock should yield a legal divider; bound defensively.
    if (divider < 1.0f) divider = 1.0f;
    if (divider > 255.0f) divider = 255.0f;
    pwm_config_set_clkdiv(&cfg, divider);
    pwm_init(slice, &cfg, false);
    pwm_set_gpio_level(gpio, 0);
    pwm_set_enabled(slice, true);
}

static void outputs_off(void) {
    pwm_set_gpio_level(PIN_CHARGE_PWM, 0);
    pwm_set_gpio_level(PIN_DISCHARGE_PWM, 0);
    gpio_put(PIN_RUN_LED, false);
    gpio_put(PIN_DRIVER_INHIBIT, false); // permanently inhibited
}

void power_stage_init(void) {
    // Inhibit pin is driven LOW before any PWM pin is configured.
    gpio_init(PIN_DRIVER_INHIBIT);
    gpio_put(PIN_DRIVER_INHIBIT, false);
    gpio_set_dir(PIN_DRIVER_INHIBIT, GPIO_OUT);

    gpio_init(PIN_RUN_LED);
    gpio_set_dir(PIN_RUN_LED, GPIO_OUT);
    gpio_put(PIN_RUN_LED, false);

    gpio_init(PIN_FAULT_N);
    gpio_set_dir(PIN_FAULT_N, GPIO_IN);
    gpio_pull_up(PIN_FAULT_N);

    pwm_channel_init(PIN_CHARGE_PWM);
    pwm_channel_init(PIN_DISCHARGE_PWM);
    started = true;
    output_direction = requested_direction = 0;
    neutral_since_ms = 0;
    outputs_off();
    printf("[power-stage] PWM indicator pins GP%u and GP%u; bridge INHIBITED on GP%u\n",
        PIN_CHARGE_PWM, PIN_DISCHARGE_PWM, PIN_DRIVER_INHIBIT);
}

bool power_stage_fault_input_ok(void) {
    return started && gpio_get(PIN_FAULT_N) != 0;
}

void power_stage_update(v2g_status_t s, uint32_t now_ms, bool mqtt_ready) {
    if (!started) return;
    outputs_off(); // clear both outputs before recalculating either

    // This check inhibits *indicator PWM only*. A real converter requires an
    // independent hardware gate-driver interlock and overcurrent protection.
    if (!mqtt_ready || s.fault_latched || !power_stage_fault_input_ok()) {
        output_direction = requested_direction = 0;
        neutral_since_ms = now_ms;
        return;
    }

    int next = s.actual_power_kw > SMALL_POWER_KW ? +1 :
               s.actual_power_kw < -SMALL_POWER_KW ? -1 : 0;

    // On a requested direction change, go neutral first.
    if (next != requested_direction) {
        requested_direction = next;
        output_direction = 0;
        neutral_since_ms = now_ms;
    }
    if (next == 0) {
        output_direction = 0;
        return;
    }
    if (output_direction != next) {
        if ((uint32_t)(now_ms - neutral_since_ms) < NEUTRAL_MS)
            return;
        output_direction = next;
    }

    float fraction = fabsf(s.actual_power_kw) / DEMO_POWER_BASE_KW;
    if (fraction > 1.0f) fraction = 1.0f;
    float duty_percent = fraction * DUTY_MAX_PERCENT;
    uint16_t compare = (uint16_t)lroundf(duty_percent * (PWM_WRAP + 1u) / 100.0f);

    if (output_direction > 0) {
        pwm_set_gpio_level(PIN_CHARGE_PWM, compare);
    } else {
        pwm_set_gpio_level(PIN_DISCHARGE_PWM, compare);
    }
    gpio_put(PIN_RUN_LED, true);
}

void power_stage_shutdown(void) {
    if (!started) return;
    outputs_off();
    pwm_set_enabled(pwm_gpio_to_slice_num(PIN_CHARGE_PWM), false);
    pwm_set_enabled(pwm_gpio_to_slice_num(PIN_DISCHARGE_PWM), false);
    gpio_set_function(PIN_CHARGE_PWM, GPIO_FUNC_SIO);
    gpio_set_function(PIN_DISCHARGE_PWM, GPIO_FUNC_SIO);
    gpio_set_dir(PIN_CHARGE_PWM, GPIO_OUT);
    gpio_set_dir(PIN_DISCHARGE_PWM, GPIO_OUT);
    gpio_put(PIN_CHARGE_PWM, false);
    gpio_put(PIN_DISCHARGE_PWM, false);
    started = false;
}
