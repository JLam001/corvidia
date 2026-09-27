#include <unity.h>
#include "dshot.h"

void test_reference_packets_and_reserved_commands() {
    uint16_t word = 123;
    TEST_ASSERT_TRUE(Dshot::encodeThrottle(0, word)); TEST_ASSERT_EQUAL_HEX16(0, word);
    TEST_ASSERT_TRUE(Dshot::encodeThrottle(48, word)); TEST_ASSERT_EQUAL_HEX16(0x0606, word);
    TEST_ASSERT_TRUE(Dshot::encodeThrottle(100, word)); TEST_ASSERT_EQUAL_HEX16(0x0c84, word);
    TEST_ASSERT_TRUE(Dshot::encodeThrottle(2047, word)); TEST_ASSERT_EQUAL_HEX16(0xffee, word);
    for (unsigned v = 1; v < 48; ++v) {
        TEST_ASSERT_FALSE(Dshot::encodeThrottle(v, word)); TEST_ASSERT_EQUAL(0, word);
    }
    TEST_ASSERT_FALSE(Dshot::encodeThrottle(2048, word)); TEST_ASSERT_EQUAL(0, word);
    TEST_ASSERT_FALSE(Dshot::encodeThrottle(65535, word));
}
void test_all_throttle_values_decode_and_checksum() {
    for (unsigned v = 48; v <= 2047; ++v) {
        uint16_t word;
        TEST_ASSERT_TRUE(Dshot::encodeThrottle(v, word));
        TEST_ASSERT_EQUAL(v, word >> 5);
        TEST_ASSERT_EQUAL(0, (word >> 4) & 1); // no telemetry request
        TEST_ASSERT_EQUAL(0, (word ^ (word >> 4) ^ (word >> 8) ^ (word >> 12)) & 15);
    }
}
void test_timing_has_exact_300_kbit_period() {
    Dshot::Timing t;
    TEST_ASSERT_TRUE(Dshot::timing(84000000, t));
    TEST_ASSERT_EQUAL(280, t.period);
    TEST_ASSERT_EQUAL(105, t.zeroHigh);
    TEST_ASSERT_EQUAL(210, t.oneHigh);
    TEST_ASSERT_FALSE(Dshot::timing(0, t)); TEST_ASSERT_EQUAL(0, t.period);
    TEST_ASSERT_FALSE(Dshot::timing(83000000, t));
    TEST_ASSERT_FALSE(Dshot::timing(300000, t));
}
void test_dma_pair_order_bit_order_and_low_tail() {
    Dshot::Timing t; Dshot::timing(84000000, t);
    Dshot::PairBuffer b;
    TEST_ASSERT_TRUE(Dshot::makePair(48, 2047, t, b));
    uint16_t first = 0, second = 0;
    for (unsigned bit = 0; bit < 16; ++bit) {
        TEST_ASSERT_TRUE(b[2*bit] == 105 || b[2*bit] == 210);
        TEST_ASSERT_TRUE(b[2*bit+1] == 105 || b[2*bit+1] == 210);
        first = (first << 1) | (b[2*bit] == 210);
        second = (second << 1) | (b[2*bit+1] == 210);
    }
    TEST_ASSERT_EQUAL_HEX16(0x0606, first);
    TEST_ASSERT_EQUAL_HEX16(0xffee, second);
    for (unsigned n = 32; n < b.size(); ++n) TEST_ASSERT_EQUAL(0, b[n]);
}
void test_zero_frames_are_encoded_bits_not_constant_low() {
    Dshot::Timing t; Dshot::timing(84000000, t);
    Dshot::PairBuffer b;
    TEST_ASSERT_TRUE(Dshot::makePair(0, 0, t, b));
    for (unsigned n = 0; n < 32; ++n) TEST_ASSERT_EQUAL(105, b[n]);
    for (unsigned n = 32; n < b.size(); ++n) TEST_ASSERT_EQUAL(0, b[n]);
}
void test_invalid_pair_clears_entire_buffer() {
    Dshot::Timing t; Dshot::timing(84000000, t);
    Dshot::PairBuffer b; b.fill(999);
    TEST_ASSERT_FALSE(Dshot::makePair(1, 100, t, b));
    for (auto v : b) TEST_ASSERT_EQUAL(0, v);
    t.oneHigh = t.period;
    TEST_ASSERT_FALSE(Dshot::makePair(100, 100, t, b));
    for (auto v : b) TEST_ASSERT_EQUAL(0, v);
}
int main() {
    UNITY_BEGIN();
    RUN_TEST(test_reference_packets_and_reserved_commands);
    RUN_TEST(test_all_throttle_values_decode_and_checksum);
    RUN_TEST(test_timing_has_exact_300_kbit_period);
    RUN_TEST(test_dma_pair_order_bit_order_and_low_tail);
    RUN_TEST(test_zero_frames_are_encoded_bits_not_constant_low);
    RUN_TEST(test_invalid_pair_clears_entire_buffer);
    return UNITY_END();
}
