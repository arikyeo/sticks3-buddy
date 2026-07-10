// Raw IR learn/replay — see ir_remote.h. BUDDY_EXTRAS_FULL builds only;
// the whole file compiles away on the 2MB m5stickc-plus.
#ifdef BUDDY_EXTRAS_FULL

#include "ir_remote.h"
#include <Arduino.h>
#include <Preferences.h>
#include <driver/rmt.h>

// Channels: TX 0 / RX 4 work on both dies — the S3's RMT splits into
// 4 TX-only (0-3) + 4 RX-only (4-7) channels, the classic ESP32 (Plus2)
// allows any. RX borrows the neighbors' RAM blocks for long codes; we
// never use channels 1-3 / 5-7 so nothing conflicts.
static const rmt_channel_t IR_TX_CH = RMT_CHANNEL_0;
static const rmt_channel_t IR_RX_CH = RMT_CHANNEL_4;

static Preferences _irPrefs;
static int16_t  _edgeCache[IR_SLOTS] = { -1, -1, -1, -1 };   // -1 = not read yet
static uint16_t _durs[IR_MAX_EDGES];                          // load/capture scratch
static RingbufHandle_t _rxRing = nullptr;
static int8_t   _learnSlot = -1;
static bool     _txUp = false;

static const char* _slotKey(uint8_t slot) {
  static char k[8];
  snprintf(k, sizeof(k), "slot%u", slot);
  return k;
}

// ---- storage ----------------------------------------------------------------

static uint16_t _loadSlot(uint8_t slot) {
  _irPrefs.begin("ir", false);   // rw: creates the namespace on first touch
  size_t bytes = _irPrefs.getBytesLength(_slotKey(slot));
  uint16_t n = (uint16_t)(bytes / 2);
  if (n > IR_MAX_EDGES) n = IR_MAX_EDGES;
  if (n) _irPrefs.getBytes(_slotKey(slot), _durs, (size_t)n * 2);
  _irPrefs.end();
  _edgeCache[slot] = (int16_t)n;
  return n;
}

uint16_t irSlotEdges(uint8_t slot) {
  if (slot >= IR_SLOTS) return 0;
  if (_edgeCache[slot] < 0) {
    _irPrefs.begin("ir", false);
    size_t bytes = _irPrefs.getBytesLength(_slotKey(slot));
    _irPrefs.end();
    uint16_t n = (uint16_t)(bytes / 2);
    _edgeCache[slot] = (int16_t)(n > IR_MAX_EDGES ? IR_MAX_EDGES : n);
  }
  return (uint16_t)_edgeCache[slot];
}

// ---- transmit -----------------------------------------------------------------

static void _txDown() {
  if (!_txUp) return;
  rmt_driver_uninstall(IR_TX_CH);
  _txUp = false;
#if defined(BOARD_STICKC_PLUS2)
  // G19 doubles as the red LED — hand the pin back to plain GPIO so
  // board::attentionLed keeps working between blasts.
  pinMode(IR_TX_GPIO, OUTPUT);
  digitalWrite(IR_TX_GPIO, LOW);
#endif
}

static bool _txUpNow() {
  if (_txUp) return true;
  rmt_config_t c = RMT_DEFAULT_CONFIG_TX((gpio_num_t)IR_TX_GPIO, IR_TX_CH);
  c.clk_div = 80;                          // 80 MHz APB -> 1 µs ticks
  c.tx_config.carrier_en           = true;
  c.tx_config.carrier_freq_hz      = 38000;
  c.tx_config.carrier_duty_percent = 33;
  c.tx_config.carrier_level        = RMT_CARRIER_LEVEL_HIGH;
  c.tx_config.idle_level           = RMT_IDLE_LEVEL_LOW;
  c.tx_config.idle_output_en       = true;
  if (rmt_config(&c) != ESP_OK) return false;
  if (rmt_driver_install(IR_TX_CH, 0, 0) != ESP_OK) return false;
  _txUp = true;
  return true;
}

bool irBlast(uint8_t slot) {
  if (slot >= IR_SLOTS || _learnSlot >= 0) return false;   // not while learning
  uint16_t n = _loadSlot(slot);
  if (n < 2) return false;
  if (!_txUpNow()) return false;

  // Pack alternating mark/space µs into RMT items (two durations each).
  // Captured values came out of 15-bit RMT durations, so they always fit.
  static rmt_item32_t items[IR_MAX_EDGES / 2 + 1];
  size_t ni = 0;
  for (uint16_t i = 0; i < n; i += 2) {
    items[ni].level0    = 1;
    items[ni].duration0 = _durs[i];
    items[ni].level1    = 0;
    items[ni].duration1 = (uint16_t)(i + 1 < n ? _durs[i + 1] : 0);
    ni++;
  }
  esp_err_t err = rmt_write_items(IR_TX_CH, items, ni, true /* wait */);
  _txDown();   // release the pin every time (Plus2 LED share, cheap on S3)
  return err == ESP_OK;
}

// ---- learn ------------------------------------------------------------------

void irLearnCancel() {
  if (_learnSlot < 0) return;
  rmt_rx_stop(IR_RX_CH);
  rmt_driver_uninstall(IR_RX_CH);
  _rxRing = nullptr;
  _learnSlot = -1;
}

bool irLearnStart(uint8_t slot) {
  if (slot >= IR_SLOTS || _learnSlot >= 0) return false;
  rmt_config_t c = RMT_DEFAULT_CONFIG_RX((gpio_num_t)IR_RX_GPIO, IR_RX_CH);
  c.clk_div = 80;                          // 1 µs ticks
  c.mem_block_num = 4;                     // whole RX bank: fits ≥256 edges
  c.rx_config.filter_en = true;
  c.rx_config.filter_ticks_thresh = 100;   // drop <100 µs glitches (sunlight)
  c.rx_config.idle_threshold = 12000;      // 12 ms of silence ends the burst
  if (rmt_config(&c) != ESP_OK) return false;
  if (rmt_driver_install(IR_RX_CH, 2048, 0) != ESP_OK) return false;
  if (rmt_get_ringbuf_handle(IR_RX_CH, &_rxRing) != ESP_OK || !_rxRing) {
    rmt_driver_uninstall(IR_RX_CH);
    return false;
  }
  rmt_rx_start(IR_RX_CH, true);
  _learnSlot = (int8_t)slot;
  return true;
}

bool   irLearning()  { return _learnSlot >= 0; }
int8_t irLearnSlot() { return _learnSlot; }

int irLearnPoll() {
  if (_learnSlot < 0) return 0;
  size_t bytes = 0;
  rmt_item32_t* items =
      (rmt_item32_t*)xRingbufferReceive(_rxRing, &bytes, 0);   // non-blocking
  if (!items) return 0;

  uint16_t n = 0;
  size_t ni = bytes / sizeof(rmt_item32_t);
  for (size_t i = 0; i < ni && n < IR_MAX_EDGES; i++) {
    if (!items[i].duration0) break;        // 0 duration = end of data
    _durs[n++] = items[i].duration0;
    if (n >= IR_MAX_EDGES) break;
    if (!items[i].duration1) break;        // trailing space of the burst
    _durs[n++] = items[i].duration1;
  }
  vRingbufferReturnItem(_rxRing, (void*)items);

  uint8_t slot = (uint8_t)_learnSlot;
  irLearnCancel();                         // one burst per learn session

  if (n < 6) return -1;                    // a real code has dozens of edges
  _irPrefs.begin("ir", false);
  _irPrefs.putBytes(_slotKey(slot), _durs, (size_t)n * 2);
  _irPrefs.end();
  _edgeCache[slot] = (int16_t)n;
  return 1;
}

#endif  // BUDDY_EXTRAS_FULL
