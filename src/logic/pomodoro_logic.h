#pragma once
#include <stdint.h>

// Pomodoro timer state machine. Pure logic — no Arduino / M5 dependency —
// so it unit-tests on the native host (test/test_pomodoro).
//
// Classic cadence: 25 min focus / 5 min break, every 4th *completed* focus
// earns a 15 min long break (skipped focus blocks don't count — skipping
// four times in a row must not buy a long break without any work).
//
// Time base is the caller's millis() tick; all arithmetic is unsigned-
// subtraction wrap-safe. Pausing freezes the remaining time by latching
// elapsed-at-pause and rebasing startMs on resume. The machine never
// self-advances more than one phase per tick — the UI loop runs at tens of
// hertz, so a whole missed phase can't happen while the firmware is alive.

enum PomoPhase : uint8_t { POMO_IDLE, POMO_FOCUS, POMO_BREAK, POMO_LONG_BREAK };
enum PomoEvent : uint8_t { POMO_EV_NONE, POMO_EV_FOCUS_DONE, POMO_EV_BREAK_DONE };

static const uint32_t POMO_FOCUS_MS      = 25UL * 60UL * 1000UL;
static const uint32_t POMO_BREAK_MS      =  5UL * 60UL * 1000UL;
static const uint32_t POMO_LONG_BREAK_MS = 15UL * 60UL * 1000UL;
static const uint8_t  POMO_CYCLE_LEN     = 4;    // completed focuses per long break

struct PomoState {
  PomoPhase phase;
  uint8_t   focusDone;        // completed focus blocks this cycle (0..3)
  bool      paused;
  uint32_t  startMs;          // phase start tick (rebased on resume)
  uint32_t  pausedElapsedMs;  // elapsed-in-phase latched at pause
};

inline void pomoInit(PomoState& s) {
  s.phase = POMO_IDLE;
  s.focusDone = 0;
  s.paused = false;
  s.startMs = 0;
  s.pausedElapsedMs = 0;
}

inline uint32_t pomoPhaseDurMs(PomoPhase ph) {
  switch (ph) {
    case POMO_FOCUS:      return POMO_FOCUS_MS;
    case POMO_BREAK:      return POMO_BREAK_MS;
    case POMO_LONG_BREAK: return POMO_LONG_BREAK_MS;
    default:              return 0;
  }
}

inline uint32_t pomoElapsedMs(const PomoState& s, uint32_t nowMs) {
  if (s.phase == POMO_IDLE) return 0;
  uint32_t e = s.paused ? s.pausedElapsedMs : (nowMs - s.startMs);
  uint32_t d = pomoPhaseDurMs(s.phase);
  return e > d ? d : e;
}

inline uint32_t pomoRemainMs(const PomoState& s, uint32_t nowMs) {
  return pomoPhaseDurMs(s.phase) - pomoElapsedMs(s, nowMs);
}

inline void _pomoEnter(PomoState& s, PomoPhase ph, uint32_t nowMs) {
  s.phase = ph;
  s.startMs = nowMs;
  s.paused = false;
  s.pausedElapsedMs = 0;
}

// A button: idle starts a fresh cycle; running pauses; paused resumes.
inline void pomoStartPause(PomoState& s, uint32_t nowMs) {
  if (s.phase == POMO_IDLE) {
    s.focusDone = 0;
    _pomoEnter(s, POMO_FOCUS, nowMs);
    return;
  }
  if (!s.paused) {
    uint32_t e = nowMs - s.startMs;
    uint32_t d = pomoPhaseDurMs(s.phase);
    s.pausedElapsedMs = e > d ? d : e;
    s.paused = true;
  } else {
    s.startMs = nowMs - s.pausedElapsedMs;
    s.paused = false;
  }
}

// B button: skip the current phase immediately. A skipped focus moves to a
// short break without counting toward the long-break cycle; skipping any
// break starts the next focus. Idle: nothing to skip.
inline void pomoSkip(PomoState& s, uint32_t nowMs) {
  if (s.phase == POMO_IDLE) return;
  _pomoEnter(s, s.phase == POMO_FOCUS ? POMO_BREAK : POMO_FOCUS, nowMs);
}

// B long-press: back to idle. Cycle progress resets with it.
inline void pomoStop(PomoState& s) { pomoInit(s); }

// Call once per loop. Returns the transition that fired (at most one) so
// the caller can chime; the machine has already entered the next phase.
inline PomoEvent pomoTick(PomoState& s, uint32_t nowMs) {
  if (s.phase == POMO_IDLE || s.paused) return POMO_EV_NONE;
  if (nowMs - s.startMs < pomoPhaseDurMs(s.phase)) return POMO_EV_NONE;
  if (s.phase == POMO_FOCUS) {
    s.focusDone++;
    if (s.focusDone >= POMO_CYCLE_LEN) {
      s.focusDone = 0;
      _pomoEnter(s, POMO_LONG_BREAK, nowMs);
    } else {
      _pomoEnter(s, POMO_BREAK, nowMs);
    }
    return POMO_EV_FOCUS_DONE;
  }
  _pomoEnter(s, POMO_FOCUS, nowMs);
  return POMO_EV_BREAK_DONE;
}
