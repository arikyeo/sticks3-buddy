// Unit tests for logic/ota_url_logic.h — extracting the release tag from
// the 302 Location header of the api-free OTA probe
// (github.com/<repo>/releases/latest/download/firmware.bin).
#include <unity.h>
#include <string.h>
#include "logic/ota_url_logic.h"

void setUp() {}
void tearDown() {}

void test_parses_bare_path() {
  char tag[32];
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "/releases/download/v0.1.1/firmware.bin", tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("0.1.1", tag);
}

void test_parses_absolute_url() {
  char tag[32];
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "https://github.com/arikyeo/sticks3-buddy/releases/download/v2.10.3/firmware.bin",
      tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("2.10.3", tag);
}

void test_tag_without_v_passes_through() {
  char tag[32];
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "/releases/download/0.9.0/firmware.bin", tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("0.9.0", tag);
}

void test_uppercase_v_stripped() {
  char tag[32];
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "/releases/download/V1.2.3/firmware-s3.bin", tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("1.2.3", tag);
}

void test_prerelease_tag_kept_verbatim() {
  char tag[32];
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "/releases/download/v0.3.0-rc1/firmware.bin", tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("0.3.0-rc1", tag);
}

void test_rejects_missing_marker() {
  char tag[32] = "sentinel";
  TEST_ASSERT_FALSE(otaParseTagFromLocation("https://github.com/", tag, sizeof(tag)));
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/latest/download/firmware.bin", tag, sizeof(tag)));
}

void test_rejects_empty_or_bare_v_tag() {
  char tag[32];
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/download//firmware.bin", tag, sizeof(tag)));
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/download/v/firmware.bin", tag, sizeof(tag)));
}

void test_rejects_tag_without_filename() {
  char tag[32];
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/download/v1.2.3", tag, sizeof(tag)));
}

void test_rejects_overflow_instead_of_truncating() {
  char tag[6];   // "1.2.3" fits exactly, "1.2.34" must not clip into "1.2.3"
  TEST_ASSERT_TRUE(otaParseTagFromLocation(
      "/releases/download/v1.2.3/firmware.bin", tag, sizeof(tag)));
  TEST_ASSERT_EQUAL_STRING("1.2.3", tag);
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/download/v1.2.34/firmware.bin", tag, sizeof(tag)));
}

void test_rejects_null_and_empty() {
  char tag[32];
  TEST_ASSERT_FALSE(otaParseTagFromLocation(nullptr, tag, sizeof(tag)));
  TEST_ASSERT_FALSE(otaParseTagFromLocation("", tag, sizeof(tag)));
  TEST_ASSERT_FALSE(otaParseTagFromLocation(
      "/releases/download/v1.2.3/firmware.bin", tag, 0));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_parses_bare_path);
  RUN_TEST(test_parses_absolute_url);
  RUN_TEST(test_tag_without_v_passes_through);
  RUN_TEST(test_uppercase_v_stripped);
  RUN_TEST(test_prerelease_tag_kept_verbatim);
  RUN_TEST(test_rejects_missing_marker);
  RUN_TEST(test_rejects_empty_or_bare_v_tag);
  RUN_TEST(test_rejects_tag_without_filename);
  RUN_TEST(test_rejects_overflow_instead_of_truncating);
  RUN_TEST(test_rejects_null_and_empty);
  return UNITY_END();
}
