// Native fixtures for the protocol v1+v2 parse core (src/proto_parse.h).
// The hooks are recording stubs; protoXferCommand mimics the device's v1
// contract exactly (xfer.h: any cmd other than "permission" is consumed,
// silently, when no transfer is active).
//
// The fixture that matters most: PRE-HELLO PURITY. A v1 host (stock desktop
// app) must see byte-identical behavior from a v2 device — no rx acks, no
// hello ack, no v2 parsing side effects.
#include <unity.h>
#include <string.h>
#include <string>
#include <vector>
#include "proto_parse.h"

// ---- recording stubs --------------------------------------------------------
static std::vector<std::string> g_emits;
static uint32_t g_now = 1000;
static std::string g_lastXferCmd;
static int      g_xferCalls = 0;
static uint32_t g_clockEpoch = 0;
static int32_t  g_clockTz = 0;
static int      g_clockCalls = 0;
static uint32_t g_tokens = 0;
static int      g_tokenCalls = 0;
static ProtoHello g_hello;
static int      g_helloCalls = 0;
static bool     g_helloSel = true;

void protoEmit(const char* line) { g_emits.push_back(line); }
uint32_t protoMillis() { return g_now; }
bool protoXferCommand(JsonDocument& doc) {
  const char* cmd = doc["cmd"];
  if (!cmd) return false;
  g_lastXferCmd = cmd;
  g_xferCalls++;
  // Device contract (xfer.h): with no transfer active, every cmd except
  // "permission" is consumed — including unknown ones, with no ack.
  return strcmp(cmd, "permission") != 0;
}
void protoSetClock(uint32_t epoch, int32_t tz) {
  g_clockEpoch = epoch; g_clockTz = tz; g_clockCalls++;
}
void protoTokens(uint32_t total) { g_tokens = total; g_tokenCalls++; }
bool protoHelloAccept(const ProtoHello& h) { g_hello = h; g_helloCalls++; return g_helloSel; }
const char* protoBoardName()  { return "TestBoard"; }
const char* protoDeviceName() { return "Buddy"; }

// ---- helpers ----------------------------------------------------------------
static TamaState  tama;
static ProtoState ps;

static bool feed(const char* line) {
  ps.rxBytes += strlen(line) + 1;   // what the device feeders count
  return protoApplyJson(line, &tama, &ps);
}

static int emitsContaining(const char* needle) {
  int n = 0;
  for (auto& e : g_emits) if (e.find(needle) != std::string::npos) n++;
  return n;
}

void setUp() {
  memset(&tama, 0, sizeof(tama));
  memset(&ps, 0, sizeof(ps));
  ps.transport = PROTO_TRANSPORT_BT;
  protoResetConn(&ps);
  sessionTabReset(&_sessions);
  memset(&_ntfy, 0, sizeof(_ntfy));
  g_emits.clear();
  g_lastXferCmd.clear();
  g_xferCalls = 0; g_clockCalls = 0; g_tokenCalls = 0; g_helloCalls = 0;
  g_clockEpoch = 0; g_clockTz = 0; g_tokens = 0;
  g_helloSel = true;
  g_now = 1000;
}
void tearDown() {}

static void doHello(const char* caps = "\"sessions\",\"ask\",\"rxack\",\"cancel\"") {
  char b[256];
  snprintf(b, sizeof(b),
    "{\"cmd\":\"hello\",\"proto\":2,"
    "\"host\":{\"id\":\"h1\",\"name\":\"Mac\",\"app\":\"claude\",\"ver\":\"1.0\"},"
    "\"caps\":[%s]}", caps);
  feed(b);
  g_emits.clear();   // tests inspect post-handshake traffic only
}

// ---- v1 golden lines --------------------------------------------------------
void test_v1_heartbeat_golden() {
  bool live = feed("{\"total\":3,\"running\":1,\"waiting\":1,\"msg\":\"approve: Bash\","
                   "\"entries\":[\"10:42 git push\",\"10:41 yarn test\"],"
                   "\"tokens\":184502,\"tokens_today\":31200,"
                   "\"prompt\":{\"id\":\"req_abc123\",\"tool\":\"Bash\",\"hint\":\"rm -rf /tmp/foo\"}}");
  TEST_ASSERT_TRUE(live);
  TEST_ASSERT_EQUAL_UINT8(3, tama.sessionsTotal);
  TEST_ASSERT_EQUAL_UINT8(1, tama.sessionsRunning);
  TEST_ASSERT_EQUAL_UINT8(1, tama.sessionsWaiting);
  TEST_ASSERT_EQUAL_STRING("approve: Bash", tama.msg);
  TEST_ASSERT_EQUAL_UINT8(2, tama.nLines);
  TEST_ASSERT_EQUAL_STRING("10:42 git push", tama.lines[0]);
  TEST_ASSERT_EQUAL_UINT32(31200, tama.tokensToday);
  TEST_ASSERT_EQUAL_UINT32(184502, g_tokens);
  TEST_ASSERT_EQUAL_STRING("req_abc123", tama.promptId);
  TEST_ASSERT_EQUAL_STRING("Bash", tama.promptTool);
  TEST_ASSERT_EQUAL_UINT32(1000, tama.lastUpdated);
  // v1 host, pre-hello: absolutely nothing goes out
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
}

void test_v1_promptless_heartbeat_clears_prompt() {
  feed("{\"prompt\":{\"id\":\"req_1\",\"tool\":\"Bash\",\"hint\":\"x\"}}");
  TEST_ASSERT_EQUAL_STRING("req_1", tama.promptId);
  feed("{\"total\":1}");
  TEST_ASSERT_EQUAL_STRING("", tama.promptId);
  TEST_ASSERT_EQUAL_STRING("", tama.promptSid);
  TEST_ASSERT_EQUAL_UINT8(0, tama.promptQn);
}

void test_v1_cmds_route_to_xfer() {
  feed("{\"cmd\":\"status\"}");
  TEST_ASSERT_EQUAL_STRING("status", g_lastXferCmd.c_str());
  feed("{\"cmd\":\"owner\",\"name\":\"Felix\"}");
  TEST_ASSERT_EQUAL_STRING("owner", g_lastXferCmd.c_str());
  TEST_ASSERT_EQUAL_INT(2, g_xferCalls);
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());   // acks are xfer's job, not ours
  TEST_ASSERT_EQUAL_UINT8(0, tama.sessionsTotal);  // no state side effects
}

void test_v1_time_sync() {
  bool live = feed("{\"time\":[1775731234,-25200]}");
  TEST_ASSERT_TRUE(live);
  TEST_ASSERT_EQUAL_INT(1, g_clockCalls);
  TEST_ASSERT_EQUAL_UINT32(1775731234u, g_clockEpoch);
  TEST_ASSERT_EQUAL_INT32(-25200, g_clockTz);
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
}

void test_garbage_line_not_live() {
  TEST_ASSERT_FALSE(feed("{not json"));
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
}

void test_unknown_field_tolerance() {
  bool live = feed("{\"total\":2,\"flurble\":{\"deep\":[1,2,3]},\"zap\":true}");
  TEST_ASSERT_TRUE(live);
  TEST_ASSERT_EQUAL_UINT8(2, tama.sessionsTotal);
}

// ---- hello negotiation -------------------------------------------------------
void test_hello_ack_shape_and_state() {
  char b[256];
  snprintf(b, sizeof(b),
    "{\"cmd\":\"hello\",\"proto\":2,"
    "\"host\":{\"id\":\"h1\",\"name\":\"Mac\",\"app\":\"claude\",\"ver\":\"1.0\"},"
    "\"caps\":[\"sessions\",\"ask\",\"rxack\",\"cancel\"]}");
  TEST_ASSERT_TRUE(feed(b));
  TEST_ASSERT_EQUAL_INT(1, (int)g_emits.size());   // exactly the ack, no rx ack
  const std::string& e = g_emits[0];
  TEST_ASSERT_TRUE(e.find("\"ack\":\"hello\"") != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"ok\":true") != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"proto\":2") != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"board\":\"TestBoard\"") != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"name\":\"Buddy\"") != std::string::npos);
  char expect[64];
  snprintf(expect, sizeof(expect), "\"maxSessions\":%d", BUDDY_MAX_SESSIONS);
  TEST_ASSERT_TRUE(e.find(expect) != std::string::npos);
  snprintf(expect, sizeof(expect), "\"maxLine\":%d", BUDDY_LINEBUF);
  TEST_ASSERT_TRUE(e.find(expect) != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"sel\":true") != std::string::npos);
  TEST_ASSERT_TRUE(e.find("\"ntfy\"") != std::string::npos);   // we advertise ntfy
  TEST_ASSERT_TRUE(ps.helloSeen);
  TEST_ASSERT_EQUAL_UINT8(2, ps.effProto);
  TEST_ASSERT_EQUAL_UINT8(PCAP_SESSIONS | PCAP_ASK | PCAP_RXACK | PCAP_CANCEL, ps.hostCaps);
  TEST_ASSERT_EQUAL_INT(1, g_helloCalls);
  TEST_ASSERT_EQUAL_STRING("h1", g_hello.id);
  TEST_ASSERT_EQUAL_STRING("Mac", g_hello.name);
  TEST_ASSERT_TRUE(g_hello.viaBle);
}

void test_hello_proto1_host_negotiates_down() {
  feed("{\"cmd\":\"hello\",\"proto\":1,\"host\":{\"id\":\"h\"},\"caps\":[\"rxack\"]}");
  TEST_ASSERT_TRUE(ps.helloSeen);
  TEST_ASSERT_EQUAL_UINT8(1, ps.effProto);   // min(1, 2)
  g_emits.clear();
  // effective v1: no rxack, no sessions ingest
  feed("{\"total\":1,\"sessions\":[{\"sid\":\"a\"}]}");
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
  TEST_ASSERT_EQUAL_UINT(0, _sessions.count);
}

void test_hello_sel_false_passthrough() {
  g_helloSel = false;
  feed("{\"cmd\":\"hello\",\"proto\":2,\"host\":{\"id\":\"h2\"},\"caps\":[]}");
  TEST_ASSERT_EQUAL_INT(1, emitsContaining("\"sel\":false"));
}

// ---- sessions ----------------------------------------------------------------
void test_sessions_ingest_post_hello() {
  doHello();
  feed("{\"total\":2,\"running\":1,\"waiting\":1,\"sessions\":["
       "{\"sid\":\"cli:1\",\"agent\":\"claude\",\"title\":\"fix tests\",\"state\":\"wait\",\"tok\":18420,\"last\":\"running pytest\"},"
       "{\"sid\":\"cli:2\",\"agent\":\"codex\",\"title\":\"refactor\",\"state\":\"run\",\"tok\":90000,\"last\":\"\"}]}");
  TEST_ASSERT_EQUAL_UINT(2, _sessions.count);
  TEST_ASSERT_EQUAL_STRING("cli:1", _sessions.s[0].sid);       // order preserved
  TEST_ASSERT_EQUAL_UINT8(SES_WAIT, _sessions.s[0].state);
  TEST_ASSERT_EQUAL_UINT32(18420, _sessions.s[0].tok);
  TEST_ASSERT_EQUAL_STRING("running pytest", _sessions.s[0].last);
  TEST_ASSERT_EQUAL_STRING("codex", _sessions.s[1].agent);
  TEST_ASSERT_EQUAL_STRING("", _sessions.s[1].last);           // codex: empty last ok
  // aggregates stay authoritative
  TEST_ASSERT_EQUAL_UINT8(2, tama.sessionsTotal);
}

void test_sessions_clamped_and_sidless_rows_skipped() {
  doHello();
  std::string line = "{\"sessions\":[";
  for (int i = 0; i < BUDDY_MAX_SESSIONS + 3; i++) {
    char b[48];
    snprintf(b, sizeof(b), "%s{\"sid\":\"s%d\"}", i ? "," : "", i);
    line += b;
  }
  line += ",{\"title\":\"no sid\"}]}";
  feed(line.c_str());
  TEST_ASSERT_EQUAL_UINT(BUDDY_MAX_SESSIONS, _sessions.count);
  TEST_ASSERT_EQUAL_STRING("s0", _sessions.s[0].sid);
}

void test_sessions_ignored_pre_hello() {
  feed("{\"total\":1,\"sessions\":[{\"sid\":\"a\",\"state\":\"run\"}]}");
  TEST_ASSERT_EQUAL_UINT(0, _sessions.count);
  TEST_ASSERT_EQUAL_UINT8(1, tama.sessionsTotal);   // v1 fields still land
}

// ---- prompt sid / qn -----------------------------------------------------------
void test_prompt_sid_qn() {
  doHello();
  feed("{\"waiting\":1,\"prompt\":{\"id\":\"req_9\",\"sid\":\"cli:1\",\"qn\":2,"
       "\"tool\":\"Bash\",\"hint\":\"make deploy\"}}");
  TEST_ASSERT_EQUAL_STRING("req_9", tama.promptId);
  TEST_ASSERT_EQUAL_STRING("cli:1", tama.promptSid);
  TEST_ASSERT_EQUAL_UINT8(2, tama.promptQn);
}

void test_prompt_sid_tolerated_pre_hello() {
  // Bridge decision #10: sid/qn arrive whenever proto>=2 was negotiated,
  // and parsing them is plain optional-field tolerance — accepted even
  // without hello since a v1 host simply never sends them.
  feed("{\"prompt\":{\"id\":\"r\",\"sid\":\"cli:9\",\"qn\":1,\"tool\":\"t\",\"hint\":\"h\"}}");
  TEST_ASSERT_EQUAL_STRING("cli:9", tama.promptSid);
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());   // still silent pre-hello
}

// ---- prompt_cancel --------------------------------------------------------------
void test_prompt_cancel_post_hello() {
  doHello();
  feed("{\"prompt\":{\"id\":\"req_1\",\"tool\":\"Bash\",\"hint\":\"x\"}}");
  g_emits.clear();
  feed("{\"cmd\":\"prompt_cancel\",\"id\":\"req_1\"}");
  TEST_ASSERT_EQUAL_STRING("", tama.promptId);
  TEST_ASSERT_EQUAL_INT(1, emitsContaining("{\"ack\":\"prompt_cancel\",\"ok\":true}"));
  g_emits.clear();
  feed("{\"cmd\":\"prompt_cancel\",\"id\":\"req_unknown\"}");
  TEST_ASSERT_EQUAL_INT(1, emitsContaining("{\"ack\":\"prompt_cancel\",\"ok\":false}"));
}

void test_prompt_cancel_pre_hello_is_v1_swallowed() {
  feed("{\"prompt\":{\"id\":\"req_1\",\"tool\":\"Bash\",\"hint\":\"x\"}}");
  feed("{\"cmd\":\"prompt_cancel\",\"id\":\"req_1\"}");
  // v1 swallow: no ack, no effect, prompt untouched
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
  TEST_ASSERT_EQUAL_STRING("req_1", tama.promptId);
  TEST_ASSERT_EQUAL_STRING("prompt_cancel", g_lastXferCmd.c_str());
}

// ---- ask -------------------------------------------------------------------------
void test_ask_first_question_only() {
  doHello();
  feed("{\"evt\":\"ask\",\"id\":\"ask_1\",\"sid\":\"cli:1\",\"multiSelect\":true,"
       "\"questions\":["
       "{\"header\":\"Which approach?\",\"text\":\"Pick one:\",\"options\":["
       "{\"label\":\"Option A\",\"desc\":\"a\"},{\"label\":\"Option B\"},"
       "{\"label\":\"C\"},{\"label\":\"D\"},{\"label\":\"E overflows\"}]},"
       "{\"header\":\"second q ignored\"}]}");
  TEST_ASSERT_EQUAL_STRING("ask_1", tama.qAskId);
  TEST_ASSERT_EQUAL_STRING("cli:1", tama.qSid);
  TEST_ASSERT_TRUE(tama.qMulti);
  TEST_ASSERT_EQUAL_STRING("Which approach?", tama.qHeader);
  TEST_ASSERT_EQUAL_STRING("Pick one:", tama.qText);
  TEST_ASSERT_EQUAL_UINT8(4, tama.qNOpts);   // clamped to 4
  TEST_ASSERT_EQUAL_STRING("Option A", tama.qOpts[0]);
  TEST_ASSERT_EQUAL_STRING("D", tama.qOpts[3]);
}

void test_ask_cancel_clears_only_matching_id() {
  doHello();
  feed("{\"evt\":\"ask\",\"id\":\"ask_1\",\"questions\":[{\"header\":\"h\",\"options\":[{\"label\":\"x\"}]}]}");
  feed("{\"evt\":\"ask_cancel\",\"id\":\"ask_other\"}");
  TEST_ASSERT_EQUAL_STRING("ask_1", tama.qAskId);
  feed("{\"evt\":\"ask_cancel\",\"id\":\"ask_1\"}");
  TEST_ASSERT_EQUAL_STRING("", tama.qAskId);
  TEST_ASSERT_EQUAL_UINT8(0, tama.qNOpts);
}

void test_ask_pre_hello_ignored() {
  feed("{\"evt\":\"ask\",\"id\":\"ask_1\",\"questions\":[{\"header\":\"h\"}]}");
  TEST_ASSERT_EQUAL_STRING("", tama.qAskId);   // fell through to v1 path
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
}

// ---- ntfy ------------------------------------------------------------------------
void test_ntfy_ring_stores_and_wraps() {
  doHello();   // note: host caps do NOT include ntfy — must still ingest
  for (int i = 0; i < 5; i++) {
    char b[128];
    snprintf(b, sizeof(b),
      "{\"evt\":\"ntfy\",\"kind\":\"gh\",\"title\":\"PR #%d\",\"body\":\"b\",\"ts\":%d}", i, i);
    feed(b);
  }
  TEST_ASSERT_EQUAL_UINT8(4, _ntfy.count);       // ring holds 4
  TEST_ASSERT_EQUAL_UINT32(5, _ntfy.total);
  TEST_ASSERT_EQUAL_STRING("PR #4", _ntfy.card[0].title);   // wrapped over #0
}

// ---- rxack gating ------------------------------------------------------------------
void test_no_rxack_pre_hello() {
  feed("{\"total\":1}");
  feed("{\"cmd\":\"status\"}");
  feed("{\"time\":[123,0]}");
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());   // BYTE-IDENTICAL v1: silence
}

void test_rxack_post_hello_with_cap() {
  doHello();
  uint32_t before = ps.rxBytes;
  const char* hb = "{\"total\":1}";
  feed(hb);
  TEST_ASSERT_EQUAL_INT(1, (int)g_emits.size());
  char expect[48];
  snprintf(expect, sizeof(expect), "{\"ack\":\"rx\",\"n\":%lu}",
           (unsigned long)(before + strlen(hb) + 1));
  TEST_ASSERT_EQUAL_STRING(expect, g_emits[0].c_str());
  // counter is monotonic across lines
  g_emits.clear();
  feed("{\"total\":2}");
  TEST_ASSERT_EQUAL_INT(1, (int)g_emits.size());
  TEST_ASSERT_TRUE(g_emits[0] != expect);
}

void test_no_rxack_without_host_cap() {
  doHello("\"sessions\",\"ask\",\"cancel\"");   // no rxack claimed
  feed("{\"total\":1}");
  TEST_ASSERT_EQUAL_INT(0, (int)g_emits.size());
}

// debug_state without BUDDY_DEBUG compiles out: swallowed like any unknown
// cmd, no ack — release builds can't leak introspection.
void test_debug_state_swallowed_without_flag() {
  doHello();
  g_emits.clear();
  feed("{\"cmd\":\"debug_state\"}");
#ifdef BUDDY_DEBUG
  TEST_ASSERT_TRUE(g_emits.size() > 0);
#else
  // consumed by the xfer catch-all; only the (capability-gated) rx ack goes out
  TEST_ASSERT_EQUAL_INT(0, emitsContaining("dbg"));
#endif
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_v1_heartbeat_golden);
  RUN_TEST(test_v1_promptless_heartbeat_clears_prompt);
  RUN_TEST(test_v1_cmds_route_to_xfer);
  RUN_TEST(test_v1_time_sync);
  RUN_TEST(test_garbage_line_not_live);
  RUN_TEST(test_unknown_field_tolerance);
  RUN_TEST(test_hello_ack_shape_and_state);
  RUN_TEST(test_hello_proto1_host_negotiates_down);
  RUN_TEST(test_hello_sel_false_passthrough);
  RUN_TEST(test_sessions_ingest_post_hello);
  RUN_TEST(test_sessions_clamped_and_sidless_rows_skipped);
  RUN_TEST(test_sessions_ignored_pre_hello);
  RUN_TEST(test_prompt_sid_qn);
  RUN_TEST(test_prompt_sid_tolerated_pre_hello);
  RUN_TEST(test_prompt_cancel_post_hello);
  RUN_TEST(test_prompt_cancel_pre_hello_is_v1_swallowed);
  RUN_TEST(test_ask_first_question_only);
  RUN_TEST(test_ask_cancel_clears_only_matching_id);
  RUN_TEST(test_ask_pre_hello_ignored);
  RUN_TEST(test_ntfy_ring_stores_and_wraps);
  RUN_TEST(test_no_rxack_pre_hello);
  RUN_TEST(test_rxack_post_hello_with_cap);
  RUN_TEST(test_no_rxack_without_host_cap);
  RUN_TEST(test_debug_state_swallowed_without_flag);
  return UNITY_END();
}
