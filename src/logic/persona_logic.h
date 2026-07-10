#pragma once
#include <stdint.h>

// Persona (buddy mood) base-state mapping, extracted from main.cpp. Pure
// logic — no Arduino / M5 dependency — so it unit-tests on the native host.
//
// derivePersona() is only the bridge-driven BASE state. main.cpp layers the
// rest on top: the inactivity timer demotes P_IDLE to P_SLEEP, and one-shot
// effects (P_CELEBRATE on a completion edge, P_DIZZY on shake, P_HEART on
// feed) temporarily override whatever the base state says.

enum PersonaState { P_SLEEP, P_IDLE, P_BUSY, P_ATTENTION, P_CELEBRATE, P_DIZZY, P_HEART };

// Display names, indexed by PersonaState (HUD / info page).
static const char* const stateNames[] = {
  "sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart"
};

// The subset of bridge state the mapping actually consumes (TamaState
// itself lives in data.h, which drags in Arduino headers).
struct PersonaInputs {
  bool    connected;
  uint8_t sessionsRunning;
  uint8_t sessionsWaiting;
  bool    recentlyCompleted;
};

inline PersonaState derivePersona(const PersonaInputs& s) {
  if (!s.connected)            return P_IDLE;   // sleep comes from the wake timer, not the bridge
  if (s.sessionsWaiting > 0)   return P_ATTENTION;
  if (s.recentlyCompleted)     return P_CELEBRATE;
  if (s.sessionsRunning >= 3)  return P_BUSY;
  return P_IDLE;   // connected, 0+ sessions, nothing urgent — hang out
}
