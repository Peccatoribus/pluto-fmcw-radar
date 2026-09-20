#!/usr/bin/env python3
"""Generate the 4096 x 16-bit sine lookup table read by radar_dsp.sv via $readmemh.

4096 entries means phase is truncated to 12 bits, giving an SFDR of about
72 dBc. The AD9361's 12-bit ADC has a theoretical ceiling of ~74 dB, and the
measured noise floor is around -50 dB, so 72 dB leaves ample margin.
"""
import numpy as np

N = 4096
AMP = 32767

v = np.round(AMP * np.sin(2 * np.pi * np.arange(N) / N)).astype(int)

with open("sin_lut.mem", "w") as f:
    for x in v:
        f.write("%04X\n" % (x & 0xFFFF))

print(f"sin_lut.mem: {N} entries, peak {v.max()} / {v.min()}")
