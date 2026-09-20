#!/usr/bin/env python3
"""Check the xsim output of tb_radar_dsp against the expected target range.

Run the Vivado simulation first (launch_simulation from create_project.tcl,
or `vivado -mode batch -source create_project.tcl` followed by
launch_simulation), which writes radar_out.txt. This script FFTs one chirp
of that output and checks the peak lands at the expected range bin.

Usage:
    python check_radar.py [path/to/radar_out.txt]
"""
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent
    / "radar_sim" / "radar_sim.sim" / "sim_1" / "behav" / "xsim" / "radar_out.txt"
)
NFFT = 256      # samples per chirp
RES_M = 3.125   # range resolution, c / (2B)
EXPECT = 25     # expected peak range bin (tb_radar_dsp's DELAY=32 target)

path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
d = np.loadtxt(path)

# the Q LSB is the chirp-boundary marker, strip it before using Q as data
i_dat = d[:, 0]
q_dat = np.floor(d[:, 1] / 2) * 2
mark = d[:, 2].astype(int)

s = i_dat + 1j * q_dat
st = np.flatnonzero(mark)

print("total samples:", len(s))
print("chirp starts:", st[:6])
print("start spacing:", np.diff(st[:6]), "  <- should all be 256")

if len(st) == 0:
    raise SystemExit("no chirp marker found, check the out_q LSB logic")

i0 = st[0]
nch = (len(s) - i0) // NFFT
m = s[i0 : i0 + nch * NFFT].reshape(nch, NFFT)
print("complete chirps:", nch)

# use the 2nd chirp to avoid the CIC's startup transient
w = np.hanning(NFFT)
R = np.fft.fft(m[1] * w)

pk = int(np.argmax(np.abs(R)))
print(f"\npeak range bin: {pk}   expected: {EXPECT}")
print(f"peak range    : {pk * RES_M:.2f} m")

if pk == EXPECT:
    print(">>> PASS")
elif pk == NFFT - EXPECT:
    print(">>> peak at the mirror bin -- dechirp conjugate sign is flipped (swap the sign in sum_q)")
else:
    print(">>> peak in the wrong place, check start spacing and in_start alignment")

mag = 20 * np.log10(np.abs(R) + 1e-9)
mag -= mag.max()

plt.figure(figsize=(9, 5))
plt.plot(np.arange(NFFT) * RES_M, mag)
plt.axvline(EXPECT * RES_M, color="r", ls="--", label=f"expected {EXPECT * RES_M:.1f} m")
plt.xlabel("Range (m)")
plt.ylabel("dB")
plt.ylim(-90, 5)
plt.grid(alpha=0.3)
plt.legend()
plt.title("Range profile")
plt.show()
