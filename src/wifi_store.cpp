// LittleFS-backed WiFi network list — see wifi_store.h for the format and
// lifecycle. The pure list semantics live in logic/wifi_store_logic.h.
#include "wifi_store.h"
#include <Arduino.h>
#include <LittleFS.h>
#include <Preferences.h>
#ifdef BUDDY_OTA
#include <WiFi.h>
#endif

static const char* WIFI_DAT = "/wifi.dat";
static const char* WIFI_TMP = "/wifi.tmp";
static const uint32_t WIFI_MAGIC = 0x31465742;   // "BWF1" little-endian

struct WifiFileHdr {
  uint32_t magic;
  uint16_t count;
  uint16_t rsvd;
};

static WifiList _list = { nullptr, 0, 0 };
static bool     _loaded = false;
static int32_t  _cachedCount = -1;   // header-derived count before first load

static bool _alloc() {
  if (_list.c) return true;
  size_t sz = (size_t)WIFI_MAX * sizeof(WifiCred);   // ~25 KB
  // PSRAM when the board has it (S3); ps_malloc returns NULL otherwise and
  // the internal heap takes it (fine on the PICO boards — rare path, and
  // only ever paid once the user actually stores networks).
  WifiCred* p = (WifiCred*)ps_malloc(sz);
  if (!p) p = (WifiCred*)malloc(sz);
  if (!p) return false;
  memset(p, 0, sz);
  _list.c = p;
  _list.cap = WIFI_MAX;
  _list.n = 0;
  return true;
}

// A save that died between remove and rename leaves only the tmp — adopt it.
static void _adoptStrandedTmp() {
  if (!LittleFS.exists(WIFI_DAT) && LittleFS.exists(WIFI_TMP))
    LittleFS.rename(WIFI_TMP, WIFI_DAT);
}

static bool _ensureLoaded() {
  if (_loaded) return true;
  if (!_alloc()) return false;
  _list.n = 0;
  _adoptStrandedTmp();
  File f = LittleFS.open(WIFI_DAT, "r");
  if (f) {
    WifiFileHdr h;
    if (f.read((uint8_t*)&h, sizeof(h)) == sizeof(h) && h.magic == WIFI_MAGIC) {
      uint16_t want = h.count > WIFI_MAX ? WIFI_MAX : h.count;
      size_t got = f.read((uint8_t*)_list.c, (size_t)want * sizeof(WifiCred));
      _list.n = (uint16_t)(got / sizeof(WifiCred));   // whole records only
      for (uint16_t i = 0; i < _list.n; i++) {        // defensive terminators
        _list.c[i].ssid[sizeof(_list.c[i].ssid) - 1] = 0;
        _list.c[i].pass[sizeof(_list.c[i].pass) - 1] = 0;
      }
    }
    f.close();
  }
  _loaded = true;
  _cachedCount = _list.n;
  return true;
}

static bool _save() {
  File f = LittleFS.open(WIFI_TMP, "w");
  if (!f) return false;
  WifiFileHdr h = { WIFI_MAGIC, _list.n, 0 };
  size_t body = (size_t)_list.n * sizeof(WifiCred);
  bool ok = f.write((const uint8_t*)&h, sizeof(h)) == sizeof(h);
  if (ok && body) ok = f.write((const uint8_t*)_list.c, body) == body;
  f.close();
  if (!ok) { LittleFS.remove(WIFI_TMP); return false; }
  LittleFS.remove(WIFI_DAT);                   // rename won't clobber a dest
  if (!LittleFS.rename(WIFI_TMP, WIFI_DAT)) return false;
  _cachedCount = _list.n;
  return true;
}

// Post-mutation: persist, or resync RAM from disk so the model never claims
// what flash lost (the ack already said false; a reboot must agree).
static int16_t _persist(int16_t r) {
  if (r < 0) return r;
  if (_save()) return r;
  _loaded = false;
  _cachedCount = -1;
  _ensureLoaded();
  return WIFI_LIST_ERR;
}

uint16_t wifiStoreCount() {
  if (_loaded) return _list.n;
  if (_cachedCount >= 0) return (uint16_t)_cachedCount;
  uint16_t n = 0;
  _adoptStrandedTmp();
  File f = LittleFS.open(WIFI_DAT, "r");
  if (f) {
    WifiFileHdr h;
    if (f.read((uint8_t*)&h, sizeof(h)) == sizeof(h) && h.magic == WIFI_MAGIC)
      n = h.count > WIFI_MAX ? WIFI_MAX : h.count;
    f.close();
  }
  _cachedCount = n;
  return n;
}

int16_t wifiStoreUpsert(const char* ssid, const char* pass) {
  if (!_ensureLoaded()) return WIFI_LIST_ERR;
  return _persist(wifiListUpsert(&_list, ssid, pass));
}

int16_t wifiStoreRemove(const char* ssid) {
  if (!_ensureLoaded()) return WIFI_LIST_ERR;
  int16_t r = wifiListRemove(&_list, ssid);
  if (r < 0) return r;                         // absent — nothing to persist
  return _persist(r);
}

bool wifiStoreClear() {
  if (!_ensureLoaded()) return false;
  wifiListClear(&_list);                       // scrubs the record bytes too
  LittleFS.remove(WIFI_TMP);
  bool ok = !LittleFS.exists(WIFI_DAT) || LittleFS.remove(WIFI_DAT);
  _cachedCount = 0;
  return ok;
}

uint16_t wifiStoreFormatSsids(char* out, size_t outLen) {
  if (!_ensureLoaded()) {
    if (out && outLen) out[0] = 0;
    return 0;
  }
  return wifiListFormatSsids(&_list, out, outLen);
}

void wifiStoreInit() {
  // Legacy single-cred migration (NVS "wifi" ssid/pass -> /wifi.dat). Read
  // read-only first: begin(..., false) would CREATE the namespace as a side
  // effect on never-provisioned devices.
  char ssid[33] = "", pass[65] = "";
  {
    Preferences p;
    if (!p.begin("wifi", true)) return;        // namespace never created
    bool has = p.isKey("ssid");
    if (has) {
      p.getString("ssid", ssid, sizeof(ssid));
      p.getString("pass", pass, sizeof(pass));
    }
    p.end();
    if (!has) return;                          // already migrated (or never set)
  }
  bool dropKeys;
  if (!ssid[0]) {
    dropKeys = true;                           // empty legacy cred — just shed it
  } else if (_ensureLoaded()) {
    int16_t r = wifiListUpsert(&_list, ssid, pass);
    if (r >= 0)      dropKeys = _save();       // save failed -> retry next boot
    else             dropKeys = true;          // FULL/invalid — the list wins
  } else {
    dropKeys = false;                          // no RAM for the model; retry
  }
  if (dropKeys) {
    Preferences p;
    if (p.begin("wifi", false)) {
      p.remove("ssid");
      p.remove("pass");
      p.end();
    }
  }
}

#ifdef BUDDY_OTA
bool wifiJoinBest(uint32_t timeoutMs, void (*tick)(void), char* errBuf, size_t errLen) {
  if (errLen) errBuf[0] = 0;
  if (!_ensureLoaded()) {
    if (errLen) snprintf(errBuf, errLen, "wifi store failed");
    return false;
  }
  if (_list.n == 0) {
    if (errLen) snprintf(errBuf, errLen, "no wifi networks");
    return false;
  }

  WiFi.persistent(false);   // creds live in OUR store, not the stack's NVS
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  if (tick) tick();
  int16_t found = WiFi.scanNetworks();         // blocking, a few seconds
  if (tick) tick();
  if (found <= 0) {
    WiFi.scanDelete();
    if (errLen) snprintf(errBuf, errLen, "no wifi in range");
    return false;
  }

  // Strongest-signal candidate per stored cred. Tiny fixed pool: the device
  // is in one physical place — more than 8 KNOWN networks visible at once
  // doesn't happen; if it somehow does, the weakest fall off.
  struct Cand { int16_t idx; int32_t rssi; };
  Cand cand[8];
  uint8_t nc = 0;
  for (int16_t i = 0; i < found; i++) {
    String s = WiFi.SSID(i);
    if (!s.length()) continue;                 // hidden AP — nothing to match
    int16_t ci = wifiListFind(&_list, s.c_str());
    if (ci < 0) continue;
    int32_t rssi = WiFi.RSSI(i);
    uint8_t j = 0;                             // same ssid on several BSSIDs
    for (; j < nc; j++) {
      if (cand[j].idx == ci) {
        if (rssi > cand[j].rssi) cand[j].rssi = rssi;
        break;
      }
    }
    if (j < nc) continue;
    if (nc < 8) {
      cand[nc].idx = ci; cand[nc].rssi = rssi; nc++;
    } else {
      uint8_t w = 0;
      for (uint8_t k = 1; k < 8; k++) if (cand[k].rssi < cand[w].rssi) w = k;
      if (rssi > cand[w].rssi) { cand[w].idx = ci; cand[w].rssi = rssi; }
    }
  }
  WiFi.scanDelete();
  if (nc == 0) {
    if (errLen) snprintf(errBuf, errLen, "no known wifi in range (%u stored)",
                         (unsigned)_list.n);
    return false;
  }
  for (uint8_t i = 1; i < nc; i++)             // strongest first
    for (uint8_t j = i; j > 0 && cand[j].rssi > cand[j - 1].rssi; j--) {
      Cand t = cand[j]; cand[j] = cand[j - 1]; cand[j - 1] = t;
    }

  uint32_t t0 = millis();
  char tried[40] = "";                         // names only — never the pass
  size_t tl = 0;
  for (uint8_t i = 0; i < nc; i++) {
    const WifiCred& cr = _list.c[cand[i].idx];
    if (tl < sizeof(tried) - 1) {
      int w = snprintf(tried + tl, sizeof(tried) - tl, "%s%s",
                       i ? "," : "", cr.ssid);
      tl = w < 0 ? sizeof(tried) - 1
                 : (tl + (size_t)w >= sizeof(tried) ? sizeof(tried) - 1
                                                    : tl + (size_t)w);
    }
    WiFi.begin(cr.ssid, cr.pass[0] ? cr.pass : nullptr);
    uint32_t a0 = millis();
    for (;;) {
      wl_status_t st = WiFi.status();
      if (st == WL_CONNECTED) return true;
      if (st == WL_CONNECT_FAILED || st == WL_NO_SSID_AVAIL) break;  // next cred
      if (millis() - a0 > 15000) break;        // per-attempt cap
      if (millis() - t0 > timeoutMs) {
        WiFi.disconnect();
        if (errLen) snprintf(errBuf, errLen, "wifi timeout (%s)", tried);
        return false;
      }
      delay(250);
      if (tick) tick();
    }
    WiFi.disconnect();
    delay(100);
  }
  if (errLen) snprintf(errBuf, errLen, "wifi failed (%s)", tried);
  return false;
}
#endif  // BUDDY_OTA
