// tune_player.h — looping BGM + one-shot jingles (auto-return to BGM),
// tone-event tracks or raw PCM clips.
//
// Ported from xavierforge/main src/tune_player.h (v3) with one change:
// their m5_compat.h include becomes <M5Unified.h> directly (this repo's
// compat shim doesn't carry speaker types). Header-only with file-static
// state: include from exactly one translation unit (main.cpp), and only
// on BUDDY_EXTRAS_FULL builds. Every function is nullptr-safe: calling
// with an empty slot is a no-op.
//
// API:
//   bgmStart(SLOT_BGM);    start looping background music
//   bgmStop();             stop background music
//   jingleStart(SLOT_X);   play a one-shot; returns to BGM when it ends
//                          (if BGM is still active)
//   tuneSkip();            skip the playing jingle back to BGM/idle, or
//                          stop BGM if that's what's playing
//   tuneStop();            silence everything, BGM does not return
//   tuneTick();            call once per loop() iteration
//   tunePlaying();         is anything playing right now

#pragma once
#include <M5Unified.h>
#include "tune_types.h"

// 64-point high-resolution sine: replaces M5Unified's default 16-point
// table to reduce digital edge noise.
static const uint8_t SMOOTH_SINE[64] = {
  134,147,159,171,182,193,204,213,222,230,237,243,248,251,254,255,
  255,254,251,248,243,237,230,222,213,204,193,182,171,159,147,134,
  122,109,97,85,74,63,52,43,34,26,19,13,8,5,2,1,
  1,2,5,8,13,19,26,34,43,52,63,74,85,97,109,122
};

// Square wave with rounded corners — keeps the chiptune bite without the
// harsh aliasing of a hard-edged square.
static const uint8_t SOFT_SQUARE[64] = {
  17,82,174,239,248,248,248,248,248,248,248,248,248,248,248,248,
  248,248,248,248,248,248,248,248,248,248,248,248,248,248,248,248,
  239,174,82,17,8,8,8,8,8,8,8,8,8,8,8,8,
  8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8
};

// Runtime waveform selection: changed by tuneSetWave()
// Smooth sine (soft, quiet) / rounded square (default, loud, classic
// Game Boy).
static const uint8_t* _tuneWaveTbl = SOFT_SQUARE;
inline void tuneSetWave(uint8_t squareMode) {
  _tuneWaveTbl = squareMode ? SOFT_SQUARE : SMOOTH_SINE;
}

static const Tune* _bgmTune = nullptr;   // the tune that SHOULD loop (whether audible or not)
static const Tune* _curTune = nullptr;   // what is actually playing right now
static uint32_t _tuneT0  = 0;            // start timestamp of the current tune
static uint32_t _tuneIdx = 0;            // index of the next tone event to send

inline void _tuneBegin(const Tune* t) {
  _curTune = t;
  _tuneT0  = millis();
  _tuneIdx = 0;
  M5.Speaker.stop();                     // clear all channels so tunes don't overlap
  if (t->pcm) {                          // PCM mode: hand the clip to the speaker
    M5.Speaker.playRaw(t->pcm, t->pcmLen, t->pcmRate, false, 1, 0, true);
  }
}

inline void bgmStart(const Tune* t) {
  _bgmTune = t;
  if (t && (t->len || t->pcm)) _tuneBegin(t);   // nullptr slot: no-op (auto-skip)
}

inline void bgmStop() {
  if (_curTune == _bgmTune) {            // only silence the speaker if BGM is audible
    _curTune = nullptr;
    M5.Speaker.stop();
  }
  _bgmTune = nullptr;                    // a finishing jingle won't bring it back either
}

inline void jingleStart(const Tune* t) {
  if (t && (t->len || t->pcm)) _tuneBegin(t);   // one-shot: takes over playback
}

// Full stop: silence whatever is playing — BGM or jingle — and don't
// return to BGM afterwards.
inline void tuneStop() {
  _curTune = nullptr;
  _bgmTune = nullptr;
  M5.Speaker.stop();
}

// Skip: if a jingle (done/alert) is playing, drop it and resume whatever
// should follow (BGM if still active, silence otherwise). If BGM itself is
// playing (or nothing is), behave like bgmStop() — manual BGM off.
// Skipping never affects future alert/approve/deny jingles.
inline void tuneSkip() {
  if (_curTune && _curTune != _bgmTune) {
    M5.Speaker.stop();
    if (_bgmTune) _tuneBegin(_bgmTune);
    else _curTune = nullptr;
  } else {
    bgmStop();
  }
}

inline bool tunePlaying() { return _curTune != nullptr; }

inline void tuneTick() {
  if (!_curTune) return;

  if (_curTune->pcm) {                   // PCM mode: just wait for it to finish
    if (!M5.Speaker.isPlaying()) {
      if (_bgmTune) _tuneBegin(_bgmTune);
      else _curTune = nullptr;
    }
    return;
  }

  uint32_t elapsed = millis() - _tuneT0;

  // Send every note whose time has come. tone() is asynchronous and
  // never blocks the loop.
  while (_tuneIdx < _curTune->len && _curTune->ev[_tuneIdx].startMs <= elapsed) {
    const ToneEvent& e = _curTune->ev[_tuneIdx];
    M5.Speaker.tone(e.freq, e.durMs, e.ch, false, _tuneWaveTbl, 64);
    _tuneIdx++;
  }

  // All notes sent and the speaker has gone quiet -> decide what's next.
  if (_tuneIdx >= _curTune->len && !M5.Speaker.isPlaying()) {
    if (_bgmTune) _tuneBegin(_bgmTune);  // BGM finished on its own -> loop from the top;
                                         // a jingle finished -> return to BGM
    else _curTune = nullptr;             // no BGM -> silence
  }
}
