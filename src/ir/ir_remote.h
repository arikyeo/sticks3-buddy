#pragma once
#include <stdint.h>

// Raw IR learn/replay on the ESP32 RMT peripheral (legacy driver/rmt.h —
// the IDF 4.4 API arduino-esp32 2.x ships). BUDDY_EXTRAS_FULL builds only.
//
// No protocol decoding: a learned code is the raw mark/space µs timings of
// one burst, replayed under a 38 kHz carrier. That covers NEC/RC5/Sony/...
// alike — most remotes don't care that we never understood the bits.
//
// 4 slots, each stored as an NVS blob (namespace "ir", key slotN: uint16
// durations, mark first, ≤ IR_MAX_EDGES). Learn is armed then polled from
// the main loop (never blocks); blast is a short blocking write (a burst
// is tens of ms — well under the 12 s loop watchdog).
//
// Pins: the StickS3 uses the task-pinned grove wiring (RX G42, TX G46).
// The Plus2 has no receiver and its onboard IR emitter shares G19 with the
// red LED, so TX = 19 (the driver releases the pin after each blast so the
// LED keeps working) and RX = grove G33 for an external receiver. Override
// per env with -DIR_TX_GPIO=n / -DIR_RX_GPIO=n.

#define IR_SLOTS     4
#define IR_MAX_EDGES 256   // stored durations per code (mark/space alternating)

#ifndef IR_TX_GPIO
#if defined(BOARD_STICKC_PLUS2)
#define IR_TX_GPIO 19
#else
#define IR_TX_GPIO 46
#endif
#endif

#ifndef IR_RX_GPIO
#if defined(BOARD_STICKC_PLUS2)
#define IR_RX_GPIO 33
#else
#define IR_RX_GPIO 42
#endif
#endif

uint16_t irSlotEdges(uint8_t slot);   // stored duration count; 0 = empty
bool     irBlast(uint8_t slot);       // replay a slot; false if empty/error

bool     irLearnStart(uint8_t slot);  // arm the receiver for one burst
void     irLearnCancel();             // disarm + release the RX channel
bool     irLearning();
int8_t   irLearnSlot();               // -1 when not learning
// Poll once per loop while learning: 0 = still waiting, 1 = captured and
// stored to NVS, -1 = burst too short / capture failed (learn ends).
int      irLearnPoll();
