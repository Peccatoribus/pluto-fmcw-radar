#!/usr/bin/env python3
"""Minimal sanity check: capture from a board running the radar_dsp.sv
bitstream and confirm the chirp-boundary marker (Q LSB) repeats every
256 samples, as radar_dsp.sv's cic_decim is expected to produce.
"""
import numpy as np
import adi

sdr = adi.ad9361(uri="ip:192.168.1.10")
sdr.rx_enabled_channels = [0]
sdr.rx_buffer_size = 256 * 256
d = sdr.rx()
q = d.imag.astype(np.int16)
st = np.flatnonzero(q & 1)
print("marker spacing:", np.diff(st[:6]))
