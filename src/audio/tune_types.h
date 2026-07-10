// tune_types.h — shared data structures for the tune engine.
// Ported from xavierforge/main src/tune_types.h, unchanged.
// BUDDY_EXTRAS_FULL builds only (8MB boards); see tunes.h.
#pragma once
#include <stdint.h>

struct ToneEvent {
  uint32_t startMs;   // start time from the beginning of the tune (ms)
  uint16_t freq;      // frequency (Hz)
  uint16_t durMs;     // duration (ms)
  uint8_t  ch;        // speaker channel 0..3
};

// A tune is EITHER a tone-event track (midi2tones output) OR a PCM clip
// (wav2raw output). Unset fields are zero-initialized, so older tone-only
// files stay compatible without re-conversion.
struct Tune {
  const ToneEvent* ev;    // tone mode: event array
  uint32_t len;           //            event count
  const int8_t* pcm;      // PCM mode:  sample data (nullptr = tone mode)
  uint32_t pcmLen;        //            sample count
  uint32_t pcmRate;       //            sample rate (Hz)
};
