"""Arithmetic regression for nr_ssb_fft_input_bounds.w30.patch, not OAI execution.

These stdlib-only tests neither load/compile OAI nor contact equipment. Frame
parameters are valid positive FFT sizes, nonnegative CP lengths and PSS indices;
the model covers the actual four FFT read spans and the uint32 legacy wrap.
"""
import unittest


UINT32_MASK = (1 << 32) - 1


def checked_offsets(pos, prefix, fft_size, divisor, buffer_size):
    if divisor == 0:
        return None
    first_sample = pos - prefix // divisor
    end_sample = first_sample + 3 * (fft_size + prefix) + fft_size
    if first_sample < 0 or end_sample > buffer_size:
        return None
    return tuple(first_sample + symbol * (prefix + fft_size) for symbol in range(4))


def legacy_offsets(pos, prefix, fft_size, divisor):
    sample_offset = (pos - prefix) & UINT32_MASK
    return tuple((sample_offset + prefix + symbol * (prefix + fft_size)
                  - prefix // divisor) & UINT32_MASK for symbol in range(4))


class SsbFftInputBoundsArithmeticTests(unittest.TestCase):
    def test_sample_zero_wrap_is_rejected(self):
        old = legacy_offsets(0, 36, 512, 8)
        self.assertEqual(old[0], 0xFFFFFFFC)
        self.assertEqual(old[0] * 4, (1 << 34) - 16)
        self.assertIsNone(checked_offsets(0, 36, 512, 8, 153600))

    def test_negative_cp_start_can_have_valid_fft_reads(self):
        self.assertLess(4 - 36, 0)
        self.assertEqual(checked_offsets(4, 36, 512, 8, 153600), (0, 548, 1096, 1644))

    def test_first_sample_boundary(self):
        self.assertIsNone(checked_offsets(3, 36, 512, 8, 153600))
        self.assertEqual(checked_offsets(4, 36, 512, 8, 153600)[0], 0)
        self.assertEqual(checked_offsets(5, 36, 512, 8, 153600)[0], 1)

    def test_exact_exclusive_end_is_allowed(self):
        offsets = checked_offsets(4, 36, 512, 8, 2156)
        self.assertEqual(offsets[-1] + 512, 2156)

    def test_one_sample_beyond_end_is_rejected(self):
        self.assertIsNone(checked_offsets(5, 36, 512, 8, 2156))
        self.assertIsNone(checked_offsets(4, 36, 512, 8, 2155))

    def test_zero_divisor_is_rejected_without_dividing(self):
        self.assertIsNone(checked_offsets(100, 36, 512, 0, 153600))

    def test_large_divisor_disables_integer_cp_advance_only(self):
        # Supported CLI shape, not an assertion of successful OTA reception.
        self.assertEqual(checked_offsets(0, 36, 512, 64, 153600), (0, 548, 1096, 1644))
        self.assertEqual(checked_offsets(0, 0, 512, 8, 153600), (0, 512, 1024, 1536))

    def test_wide_end_arithmetic_does_not_wrap_signed_32_bits(self):
        self.assertIsNone(checked_offsets((1 << 31) - 1, 4096, 65536, 8, 1 << 31))

    def test_acceptance_matches_every_fft_read_and_preserves_valid_samples(self):
        for fft_size, prefix in ((256, 18), (512, 36), (1024, 72), (2048, 144)):
            buffer_size = 12 * (fft_size + prefix)
            for divisor in (1, 2, 8, 64, 256):
                for pos in range(buffer_size + 2):
                    first = pos - prefix // divisor
                    spans = tuple(first + symbol * (prefix + fft_size) for symbol in range(4))
                    fits = all(0 <= start and start + fft_size <= buffer_size for start in spans)
                    checked = checked_offsets(pos, prefix, fft_size, divisor, buffer_size)
                    self.assertEqual(checked is not None, fits)
                    if fits:
                        self.assertEqual(checked, spans)
                        self.assertEqual(checked, legacy_offsets(pos, prefix, fft_size, divisor))


if __name__ == '__main__':
    unittest.main()
