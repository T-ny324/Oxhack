CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -g -fsanitize=address,undefined
INC      = -I../include -I..
SRC_CORE = ../src/dummy_data.c ../src/scheduler.c ../src/headline.c
OUT      = ../build-host

all: test

$(OUT):
	mkdir -p $(OUT)

test: $(OUT)
	$(CC) $(CFLAGS) $(INC) ../src/converter.c ../tests/test_converter.c -lm -o $(OUT)/test_converter && $(OUT)/test_converter
	$(CC) $(CFLAGS) $(INC) $(SRC_CORE) ../tests/test_scheduler.c -lm -o $(OUT)/test_scheduler && $(OUT)/test_scheduler

plan: $(OUT)
	$(CC) -std=c11 -Wall -Wextra $(INC) $(SRC_CORE) plan_dump.c -lm -o $(OUT)/plan_dump && $(OUT)/plan_dump $(DAY)

# Runs the REAL main.c with a virtual clock and stub wifi/mqtt/power stage.
demo: $(OUT)
	$(CC) $(CFLAGS) $(INC) -I. ../main.c host_stubs.c ../src/converter.c $(SRC_CORE) -lm -o $(OUT)/demo
	$(OUT)/demo

demo-mqtt: $(OUT)
	$(CC) $(CFLAGS) -DHOST_MQTT $(INC) -I. ../main.c host_stubs.c ../src/converter.c $(SRC_CORE) -lm -o $(OUT)/demo_mqtt
	$(OUT)/demo_mqtt
