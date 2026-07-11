// motifs.h — the buddy's sound vocabulary: one short, distinguishable
// motif per event so a beep TELLS you what happened without looking.
//
//   event        motif                          reads as
//   ---------    ---------------------------    -------------------------
//   PROMPT       1200/1600/2000Hz rising x3     "come here" — approval
//                (70ms each)                    needed, the urgent one
//   APPROVE      2400Hz 60ms single high        confirmed
//   DENY         600Hz 90ms single low          denied
//   DONE         2000->2800Hz two-note ding     task complete (celebrate)
//   CARD         900Hz 40ms one soft mid blip   ntfy card — NOT the
//                                               prompt sound, glance later
//   CONNECT      900->1400Hz rising pair        host link up
//   DISCONNECT   1400->900Hz falling pair       host link lost
//   OTA_START    1000Hz double mid              update starting
//   OTA_OK       = DONE (celebrate reuse)       update flashed
//   OTA_FAIL     500Hz triple low               update failed
//   PASSKEY      1800Hz 60ms single             pairing code on screen
//
// Routing (main.cpp soundEvent): BUDDY_EXTRAS_FULL builds send PROMPT /
// APPROVE / DENY / DONE (and OTA_OK) through the tune slots (SLOT_ALERT /
// SLOT_APPROVE / SLOT_DENY / SLOT_DONE) when the tunes setting is on;
// every other event — and every event on non-FULL boards — plays the
// motif below through board::beep via the pure scheduler here. Everything
// gates on s_vol. The info screen's SOUNDS page is the on-device legend
// (hold B there to hear the vocabulary).
//
// Pure data + pure scheduler: no Arduino dependency, unit-tested natively
// (test/test_motifs). Header-static tables: fine to include from more than
// one translation unit (each gets its own copy).
#pragma once
#include <stdint.h>

enum MotifId : uint8_t {
  MOTIF_PROMPT = 0,
  MOTIF_APPROVE,
  MOTIF_DENY,
  MOTIF_DONE,
  MOTIF_CARD,
  MOTIF_CONNECT,
  MOTIF_DISCONNECT,
  MOTIF_OTA_START,
  MOTIF_OTA_OK,
  MOTIF_OTA_FAIL,
  MOTIF_PASSKEY,
  MOTIF_COUNT
};

struct MotifNote {
  uint16_t freq;    // Hz
  uint16_t durMs;   // tone length
  uint16_t gapMs;   // silence after the tone, before the next note
};

struct Motif {
  const MotifNote* n;
  uint8_t len;
};

static const MotifNote MOTIF_EV_PROMPT[]  = { {1200,70,20}, {1600,70,20}, {2000,70,0} };
static const MotifNote MOTIF_EV_APPROVE[] = { {2400,60,0} };
static const MotifNote MOTIF_EV_DENY[]    = { {600,90,0} };
static const MotifNote MOTIF_EV_DONE[]    = { {2000,100,30}, {2800,150,0} };
static const MotifNote MOTIF_EV_CARD[]    = { {900,40,0} };
static const MotifNote MOTIF_EV_CONN[]    = { {900,50,20}, {1400,60,0} };
static const MotifNote MOTIF_EV_DISC[]    = { {1400,50,20}, {900,60,0} };
static const MotifNote MOTIF_EV_OTA_S[]   = { {1000,60,60}, {1000,60,0} };
static const MotifNote MOTIF_EV_OTA_F[]   = { {500,90,40}, {500,90,40}, {500,90,0} };
static const MotifNote MOTIF_EV_PASSKEY[] = { {1800,60,0} };

// Indexed by MotifId. OTA_OK deliberately aliases DONE — an update landing
// IS a celebration, and one fewer sound to learn.
static const Motif MOTIFS[MOTIF_COUNT] = {
  { MOTIF_EV_PROMPT,  3 },
  { MOTIF_EV_APPROVE, 1 },
  { MOTIF_EV_DENY,    1 },
  { MOTIF_EV_DONE,    2 },
  { MOTIF_EV_CARD,    1 },
  { MOTIF_EV_CONN,    2 },
  { MOTIF_EV_DISC,    2 },
  { MOTIF_EV_OTA_S,   2 },
  { MOTIF_EV_DONE,    2 },   // MOTIF_OTA_OK = celebrate reuse
  { MOTIF_EV_OTA_F,   3 },
  { MOTIF_EV_PASSKEY, 1 },
};

// --- pure non-blocking scheduler ---------------------------------------------
// One player, latest-start wins (the speaker is one-tone-at-a-time anyway).
// The caller owns time: pass a monotonic ms clock to start/tick and sound
// whatever note tick returns (board::beep on device, capture in tests).
struct MotifPlayer {
  const Motif* m;      // currently playing motif, null = idle
  uint8_t idx;         // next note to emit
  uint32_t nextAt;     // when that note is due (caller clock)
};

inline void motifReset(MotifPlayer* p) { p->m = 0; p->idx = 0; p->nextAt = 0; }

inline void motifStart(MotifPlayer* p, uint8_t id, uint32_t now) {
  if (id >= MOTIF_COUNT) { motifReset(p); return; }
  p->m = &MOTIFS[id];
  p->idx = 0;
  p->nextAt = now;
}

inline bool motifActive(const MotifPlayer* p) { return p->m != 0; }

// Advance the player: returns the note due now (caller sounds it), or
// null when nothing is due yet / the player is idle. Signed compare keeps
// it millis()-wrap safe.
inline const MotifNote* motifTick(MotifPlayer* p, uint32_t now) {
  if (!p->m) return 0;
  if ((int32_t)(now - p->nextAt) < 0) return 0;
  const MotifNote* n = &p->m->n[p->idx];
  p->nextAt = now + n->durMs + n->gapMs;
  if (++p->idx >= p->m->len) { p->m = 0; p->idx = 0; }   // last note sent -> idle
  return n;
}
