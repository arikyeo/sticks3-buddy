#pragma once
#include "tune_types.h"

// Built-in jingle tracks. xavierforge's tune registry expects user-converted
// midi2tones headers in tunes/ — none ship with any repo — so these tiny
// hand-written tone-event tracks make the engine audible out of the box.
// Each maps a buddy moment; tunes.h picks a user drop-in over the builtin
// per slot. There is no builtin BGM: background music stays opt-in data
// (and off by default even then).
//
// Frequencies are tempered-scale notes so the jingles read as music, not
// alarm chirps: approve rises (major arpeggio), deny falls (minor third),
// done is a "ta-daa", alert is a two-pip attention pattern repeated once.

static const ToneEvent TUNE_BI_APPROVE_EV[] = {
  {   0,  523,  70, 0 },   // C5
  {  80,  659,  70, 0 },   // E5
  { 160,  784,  70, 0 },   // G5
  { 240, 1047, 140, 0 },   // C6
};
static const Tune TUNE_BUILTIN_APPROVE = { TUNE_BI_APPROVE_EV, 4, nullptr, 0, 0 };

static const ToneEvent TUNE_BI_DENY_EV[] = {
  {   0,  392, 110, 0 },   // G4
  { 120,  311, 180, 0 },   // D#4
};
static const Tune TUNE_BUILTIN_DENY = { TUNE_BI_DENY_EV, 2, nullptr, 0, 0 };

static const ToneEvent TUNE_BI_DONE_EV[] = {
  {   0,  659,  90, 0 },   // E5
  { 100,  784,  90, 0 },   // G5
  { 200, 1047, 220, 0 },   // C6
};
static const Tune TUNE_BUILTIN_DONE = { TUNE_BI_DONE_EV, 3, nullptr, 0, 0 };

static const ToneEvent TUNE_BI_ALERT_EV[] = {
  {   0, 1047,  60, 0 },   // C6
  {  90, 1319,  60, 0 },   // E6
  { 300, 1047,  60, 0 },   // C6
  { 390, 1319, 100, 0 },   // E6
};
static const Tune TUNE_BUILTIN_ALERT = { TUNE_BI_ALERT_EV, 4, nullptr, 0, 0 };
