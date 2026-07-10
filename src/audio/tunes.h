// tunes.h — tune slot registry, ported from xavierforge/main src/tunes.h.
// BUDDY_EXTRAS_FULL builds only (8MB boards); include from main.cpp only.
//
// Five slots, mapped to buddy moments:
//
//   SLOT_BGM      background music while tasks run — OFF by default: no
//                 builtin data and nothing calls bgmStart() yet, so it
//                 stays silent unless a future toggle wires it up
//   SLOT_ALERT    permission prompt arrival (attention)
//   SLOT_APPROVE  approve pressed
//   SLOT_DENY     deny pressed
//   SLOT_DONE     task complete (celebrate)
//
// Drop converted .h files into src/audio/tunes/ to override per slot:
//
//   tunes/bgm.h  tunes/alert.h  tunes/approve.h  tunes/deny.h  tunes/done.h
//
// Conversion (xavierforge tooling): python midi2tones.py file.mid bgm >
// src/audio/tunes/bgm.h. __has_include is a compile-time check — a missing
// file is not an error, the slot just falls back to the builtin jingle
// (tunes_builtin.h) or, for bgm, to nullptr (playback calls no-op on it).

#pragma once
#include "tune_player.h"
#include "tunes_builtin.h"

#if __has_include("tunes/bgm.h")
  #include "tunes/bgm.h"
  [[maybe_unused]] static const Tune* SLOT_BGM = &TUNE_BGM;
#else
  [[maybe_unused]] static const Tune* SLOT_BGM = nullptr;
#endif

#if __has_include("tunes/alert.h")
  #include "tunes/alert.h"
  [[maybe_unused]] static const Tune* SLOT_ALERT = &TUNE_ALERT;
#else
  [[maybe_unused]] static const Tune* SLOT_ALERT = &TUNE_BUILTIN_ALERT;
#endif

#if __has_include("tunes/approve.h")
  #include "tunes/approve.h"
  [[maybe_unused]] static const Tune* SLOT_APPROVE = &TUNE_APPROVE;
#else
  [[maybe_unused]] static const Tune* SLOT_APPROVE = &TUNE_BUILTIN_APPROVE;
#endif

#if __has_include("tunes/deny.h")
  #include "tunes/deny.h"
  [[maybe_unused]] static const Tune* SLOT_DENY = &TUNE_DENY;
#else
  [[maybe_unused]] static const Tune* SLOT_DENY = &TUNE_BUILTIN_DENY;
#endif

#if __has_include("tunes/done.h")
  #include "tunes/done.h"
  [[maybe_unused]] static const Tune* SLOT_DONE = &TUNE_DONE;
#else
  [[maybe_unused]] static const Tune* SLOT_DONE = &TUNE_BUILTIN_DONE;
#endif
