#include "host_registry.h"
#include "ble_bridge.h"
#include <Arduino.h>
#include <Preferences.h>
#include <esp_gap_ble_api.h>

// Device half of the multi-host registry: NVS persistence (namespace
// "hosts"), boot reconcile against the Bluedroid bond store, and the
// connection policy engine (soft-pin, pairing window, pinned-host
// fallback). The pure table ops live in host_registry.h.
//
// Policy model (Track F P4):
//   AUTO  (pin < 0): open advertising, first bonded connect wins — stock
//         v1 behavior, untouched.
//   PINNED (pin = slot): connections are accepted, the peer is resolved
//         against the registry, and a non-pinned bonded host is soft-
//         rejected: if its hello arrives first it gets an ack with
//         sel:false, then the link is dropped (<= 1s after connect either
//         way). Advertising restarts automatically on disconnect.
//   Fallback: while pinned and disconnected, only the pinned host may stay
//         connected for the first 30s of an advertising cycle; after that,
//         hostfb=1 (auto) accepts any bonded host until the next
//         disconnect/pin change, hostfb=0 (stay) keeps holding out.
//   Pairing window: while open (60s, menu-triggered), the device courts NEW
//         machines exclusively: opening the window evicts the currently
//         connected host, and ble_bridge.cpp's onConnect gate drops BONDED
//         peers for the window's duration (they auto-reconnect once it
//         ends) so an auto-reconnecting host can't re-occupy the single
//         link before the new machine pairs. Unknown peers connect + bond;
//         the soft-pin reject is suspended for them (the window is explicit
//         user intent). The first successful new bond closes the window and
//         normal advertising + policy (auto/pinned) resumes. Outside the
//         window, the same gate drops UNbonded peers before pairing can
//         start. Window state is RAM-only — a reboot lands in normal mode.

static HostTable   _hosts;
static Preferences _hprefs;   // separate instance from stats.h's _prefs

static int8_t   _pin          = -1;
static uint8_t  _fbAuto       = 1;
static bool     _fallback     = false;   // pinned-host no-show fallback active
static uint32_t _pinWaitUntil = 0;       // pinned-only acceptance deadline
static uint32_t _doomAt       = 0;       // pending disconnect of current link
static bool     _connSelected = true;    // drives hello ack data.sel
static int      _connSlot     = -1;      // registry slot of the connected peer
static char     _bleName[24]  = "";      // display name for the connected peer
static char     _usbName[24]  = "";      // hello name of the USB host, if any
static bool     _wasConn      = false;
static bool     _wasSecure    = false;
static char     _pairedName[24] = "";    // pending "paired:" toast for main.cpp

// --- NVS ---------------------------------------------------------------------
// Layout: "n" (U8 used-slot count), "seq" (U32 LRU counter), "h0".."h6"
// (HostRec blobs, compact — h{i} exists iff i < n).
static void _save() {
  _hprefs.begin("hosts", false);
  _hprefs.putUChar("n", _hosts.count);
  _hprefs.putUInt("seq", _hosts.seq);
  char key[4];
  for (uint8_t i = 0; i < BUDDY_MAX_HOSTS; i++) {
    snprintf(key, sizeof(key), "h%u", (unsigned)i);
    if (i < _hosts.count) _hprefs.putBytes(key, &_hosts.h[i], sizeof(HostRec));
    else if (_hprefs.isKey(key)) _hprefs.remove(key);
  }
  _hprefs.end();
}

static void _load() {
  hostTabInit(&_hosts);
  if (!_hprefs.begin("hosts", true)) return;   // namespace not created yet
  uint8_t n = _hprefs.getUChar("n", 0);
  if (n > BUDDY_MAX_HOSTS) n = BUDDY_MAX_HOSTS;
  _hosts.seq = _hprefs.getUInt("seq", 0);
  char key[4];
  uint8_t out = 0;
  for (uint8_t i = 0; i < n; i++) {
    snprintf(key, sizeof(key), "h%u", (unsigned)i);
    if (_hprefs.getBytes(key, &_hosts.h[out], sizeof(HostRec)) == sizeof(HostRec)
        && (_hosts.h[out].flags & HOST_F_USED)) {
      out++;
    }
  }
  _hosts.count = out;
  _hprefs.end();
}

// --- bond-store reconcile ------------------------------------------------------
// Registry truth follows the bond store: a record without a bond is useless
// (the peer can't reconnect encrypted), and a bond without a record is a
// host we adopted before the registry existed (or a store restored by
// factory tooling). Runs once at boot, after bleInit.
static void _reconcileBonds() {
  int n = esp_ble_get_bond_device_num();
  esp_ble_bond_dev_t* list = nullptr;
  if (n > 0) {
    list = (esp_ble_bond_dev_t*)malloc((size_t)n * sizeof(esp_ble_bond_dev_t));
    if (!list) return;
    if (esp_ble_get_bond_device_list(&n, list) != ESP_OK) { free(list); return; }
  } else {
    n = 0;
  }

  bool dirty = false;
  // Drop registry entries whose bond vanished.
  for (int i = (int)_hosts.count - 1; i >= 0; i--) {
    bool found = false;
    for (int b = 0; b < n; b++) {
      if (memcmp(_hosts.h[i].bda, list[b].bd_addr, 6) == 0) { found = true; break; }
    }
    if (!found) {
      Serial.printf("[hosts] drop stale '%s' (bond gone)\n", _hosts.h[i].name);
      _pin = hostTabRemapPin(_pin, i);
      hostTabDrop(&_hosts, i);
      dirty = true;
    }
  }
  // Adopt unknown bonds as Host-N, LRU-evicting if somehow over capacity.
  for (int b = 0; b < n; b++) {
    if (hostTabFindBda(&_hosts, list[b].bd_addr) >= 0) continue;
    if (_hosts.count >= BUDDY_MAX_HOSTS) {
      int v = hostTabLru(&_hosts);
      if (v < 0) break;
      Serial.printf("[hosts] evict LRU '%s'\n", _hosts.h[v].name);
      esp_ble_remove_bond_device(_hosts.h[v].bda);
      _pin = hostTabRemapPin(_pin, v);
      hostTabDrop(&_hosts, v);
    }
    int idx = hostTabAdopt(&_hosts, list[b].bd_addr);
    Serial.printf("[hosts] adopt bond as '%s'\n", idx >= 0 ? _hosts.h[idx].name : "?");
    dirty = true;
  }
  if (list) free(list);
  if (dirty) _save();
}

// --- lifecycle -----------------------------------------------------------------
void hostGlueInit(int8_t pinnedSlot, uint8_t fallbackAuto) {
  _fbAuto = fallbackAuto ? 1 : 0;
  _load();
  _reconcileBonds();
  _pin = pinnedSlot;
  if (_pin >= (int8_t)_hosts.count) _pin = -1;   // stale slot from NVS
  if (_pin >= 0) _pinWaitUntil = millis() + 30000;
  Serial.printf("[hosts] %u host(s), pin=%d fb=%u\n",
                _hosts.count, (int)_pin, (unsigned)_fbAuto);
}

void hostGluePoll(uint32_t now) {
  bool conn = bleConnected();

  if (conn && !_wasConn) {
    // Link up (possibly pre-bond for a pairing-window newcomer).
    _doomAt = 0;
    _connSelected = true;
    _connSlot = -1;
    _bleName[0] = 0;
    uint8_t bda[6];
    if (blePeerBda(bda)) {
      _connSlot = hostTabFindBda(&_hosts, bda);
      if (_connSlot >= 0) {
        strncpy(_bleName, _hosts.h[_connSlot].name, sizeof(_bleName) - 1);
        _bleName[sizeof(_bleName) - 1] = 0;
      }
      // Soft-pin: a known host that isn't the pinned one is rejected unless
      // fallback kicked in or a pairing window is open (explicit user
      // intent). Unknown peers here are pairing-window newcomers — the
      // onConnect gate already dropped unbonded peers outside the window.
      if (_pin >= 0 && !_fallback && blePairingRemainingMs() == 0 &&
          _connSlot >= 0 && _connSlot != _pin) {
        _connSelected = false;
        _doomAt = now + 1000;
        Serial.printf("[hosts] soft-pin reject '%s' (pinned '%s')\n",
                      _hosts.h[_connSlot].name, _hosts.h[_pin].name);
      }
    }
  }

  if (!conn && _wasConn) {
    // Link down: restart the pinned-only acceptance clock.
    _connSlot = -1;
    _bleName[0] = 0;
    _doomAt = 0;
    _connSelected = true;
    if (_pin >= 0) {
      _pinWaitUntil = now + 30000;
      _fallback = false;
    }
  }

  bool sec = conn && bleSecure();
  if (sec && !_wasSecure) {
    // Bond established (fresh pairing or LTK resume): make sure the peer is
    // in the registry and freshen its LRU stamp.
    uint8_t bda[6];
    if (blePeerBda(bda)) {
      int idx = hostTabFindBda(&_hosts, bda);
      if (idx < 0) {
        if (_hosts.count >= BUDDY_MAX_HOSTS) {
          int v = hostTabLru(&_hosts);
          if (v >= 0) {
            Serial.printf("[hosts] evict LRU '%s' for new host\n", _hosts.h[v].name);
            esp_ble_remove_bond_device(_hosts.h[v].bda);
            _pin = hostTabRemapPin(_pin, v);
            hostTabDrop(&_hosts, v);
          }
        }
        idx = hostTabAdopt(&_hosts, bda);
        // A new host just bonded. End the pairing window immediately (also
        // harmless for the windowless zero-bond out-of-box flow) so normal
        // advertising + policy resume and the previously evicted hosts can
        // reconnect on their own. The name here is the adopt-time
        // placeholder (Host-N); the hello that follows on the now-encrypted
        // link renames it moments later.
        bool viaWindow = blePairingRemainingMs() > 0;
        bleAllowPairing(false, 0);
        if (idx >= 0) {
          if (viaWindow)
            Serial.printf("[ble] pairwin paired '%s' %02x:%02x:%02x:%02x:%02x:%02x\n",
                          _hosts.h[idx].name, bda[0], bda[1], bda[2],
                          bda[3], bda[4], bda[5]);
          // Queue the UI toast (window or out-of-box alike).
          strncpy(_pairedName, _hosts.h[idx].name, sizeof(_pairedName) - 1);
          _pairedName[sizeof(_pairedName) - 1] = 0;
        }
        Serial.printf("[hosts] new host '%s'\n", idx >= 0 ? _hosts.h[idx].name : "?");
      }
      if (idx >= 0) {
        hostTabTouch(&_hosts, idx);
        _connSlot = idx;
        if (!_bleName[0]) {
          strncpy(_bleName, _hosts.h[idx].name, sizeof(_bleName) - 1);
          _bleName[sizeof(_bleName) - 1] = 0;
        }
        _save();
      }
    }
  }

  _wasConn = conn;
  _wasSecure = sec;

  // Execute a pending soft-pin disconnect.
  if (_doomAt && conn && (int32_t)(now - _doomAt) >= 0) {
    _doomAt = 0;
    bleDisconnect();
  }

  // Pinned host didn't show inside 30s: hostfb=1 falls back to accepting
  // any bonded host (until the next disconnect or pin change).
  if (_pin >= 0 && !conn && _fbAuto && !_fallback && _pinWaitUntil &&
      (int32_t)(now - _pinWaitUntil) >= 0) {
    _fallback = true;
    Serial.println("[hosts] pinned host no-show, falling back to auto accept");
  }

#ifdef BUDDY_BLE_WHITELIST
  // Re-arm the controller whitelist when the pairing window ends — by any
  // path (manual toggle, successful pairing, expiry). hostGluePairWindow
  // lifted it on open so unknown peers could connect at all.
  static bool wasWin = false;
  bool win = blePairingRemainingMs() > 0;
  if (wasWin && !win) bleWhitelistPin(_pin >= 0 ? _hosts.h[_pin].bda : nullptr);
  wasWin = win;
#endif
}

// --- hello glue ----------------------------------------------------------------
bool hostGlueOnHello(const char* hostId, const char* name, const char* app,
                     bool viaBle) {
  if (!viaBle) {
    // USB link: always the selected one; remember the name for the badge.
    utf8Sanitize(_usbName, sizeof(_usbName), name ? name : "");
    return true;
  }
  if (_connSlot >= 0) {
    hostTabHello(&_hosts, _connSlot, hostId, name, app);
    strncpy(_bleName, _hosts.h[_connSlot].name, sizeof(_bleName) - 1);
    _bleName[sizeof(_bleName) - 1] = 0;
    _save();
  } else if (name && name[0]) {
    utf8Sanitize(_bleName, sizeof(_bleName), name);
  }
  if (!_connSelected) {
    // sel:false ack goes out first (bleWrite is synchronous), then drop the
    // link quickly so the losing host backs off per spec.
    _doomAt = millis() + 300;
  }
  return _connSelected;
}

// --- pin / pairing controls ------------------------------------------------------
void hostGlueSetPin(int8_t slot) {
  if (slot >= (int8_t)_hosts.count) slot = -1;
  _pin = slot;
  _fallback = false;
  _pinWaitUntil = millis() + 30000;
#ifdef BUDDY_BLE_WHITELIST
  bleWhitelistPin(_pin >= 0 ? _hosts.h[_pin].bda : nullptr);
#endif
  if (_pin >= 0 && bleConnected() && _connSlot != _pin) {
    // Switch flow: drop the current link and re-advertise for the pin.
    bleDisconnect();
  }
}

int8_t hostGluePin() { return _pin; }

void hostGluePairWindow(bool open) {
#ifdef BUDDY_BLE_WHITELIST
  // The window courts unknown peers, which a controller whitelist would
  // block at the radio layer — lift it for the window's duration. The
  // restore on close (any close: manual, paired, expired) happens in
  // hostGluePoll's window-transition check.
  if (open) bleWhitelistPin(nullptr);
#endif
  bleAllowPairing(open, 60000);
}

// One-shot: copy out the display name of a host that just completed a NEW
// bond (pairing window or out-of-box) and clear the pending flag. main.cpp
// polls this from loop() to toast "paired: <name>".
bool hostGlueTakePaired(char* out, size_t cap) {
  if (!_pairedName[0] || !out || cap == 0) return false;
  strncpy(out, _pairedName, cap - 1);
  out[cap - 1] = 0;
  _pairedName[0] = 0;
  return true;
}

void hostGlueForget(uint8_t slot) {
  if (slot >= _hosts.count) return;
  if ((int)slot == _connSlot) {
    bleDisconnect();
    _connSlot = -1;
    _bleName[0] = 0;
  }
  esp_ble_remove_bond_device(_hosts.h[slot].bda);
  _pin = hostTabRemapPin(_pin, (int)slot);
  if (_connSlot > (int)slot) _connSlot--;
  hostTabDrop(&_hosts, (int)slot);
  _save();
  Serial.printf("[hosts] forgot slot %u, %u left\n", (unsigned)slot, _hosts.count);
}

void hostGlueForgetAll() {
  if (bleConnected()) bleDisconnect();
  bleClearBonds();
  hostTabInit(&_hosts);
  _pin = -1;
  _fallback = false;
  _connSlot = -1;
  _bleName[0] = 0;
  _hprefs.begin("hosts", false);
  _hprefs.clear();
  _hprefs.end();
  Serial.println("[hosts] forgot all hosts");
}

// --- UI getters ------------------------------------------------------------------
uint8_t hostGlueCount() { return _hosts.count; }

const HostRec* hostGlueGet(uint8_t slot) {
  return slot < _hosts.count ? &_hosts.h[slot] : nullptr;
}

int hostGlueConnectedSlot() { return bleConnected() ? _connSlot : -1; }

const char* hostGlueActiveName() {
  if (bleConnected() && _bleName[0]) return _bleName;
  return _usbName;   // caller gates on dataConnected() for the USB case
}
