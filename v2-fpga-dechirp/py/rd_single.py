#!/usr/bin/env python3
"""PlutoSDR FMCW radar -- single-frame range-Doppler analysis (requires the
radar_dsp.sv bitstream running on the board; see the top-level README for
integration status).

Captures one frame and produces three plots:
  1. Range profile (before MTI)
  2. Range profile (after MTI)   -- same dB reference as plot 1, so you can
     read off how much clutter suppression MTI bought you
  3. Range-Doppler map

Usage:
    python rd_single.py
"""

import numpy as np
import matplotlib.pyplot as plt
import adi

# =============================================================================
# Parameters -- must match the FPGA build
# =============================================================================
URI = "ip:192.168.1.10"

NRANGE = 256              # samples per chirp = range-FFT length
NCHIRP = 256              # chirps per frame = Doppler-FFT length
B = 48e6                  # sweep bandwidth
T_CHIRP = 32768 / 61.44e6  # 533.33 us
LAMBDA = 3e8 / 2.35e9      # 12.77 cm

RES_M = 3e8 / (2 * B)                  # 3.125 m
RES_V = LAMBDA / (2 * NCHIRP * T_CHIRP)  # 0.468 m/s


def cic_droop(n, R=128, N=4):
    """CIC passband droop. We skip the compensation FIR on the FPGA and
    divide it out here instead."""
    f = np.fft.fftfreq(n)
    x = np.pi * f
    h = np.ones(n)
    nz = np.abs(x) > 1e-12
    h[nz] = np.abs(np.sin(x[nz]) / (R * np.sin(x[nz] / R))) ** N
    return h


def db(x):
    return 20 * np.log10(np.abs(x) + 1e-12)


# =============================================================================
# Acquire
# =============================================================================
sdr = adi.ad9361(uri=URI)
sdr.rx_enabled_channels = [0, 1]      # only ch0/ch1 carry radar data
sdr.rx_buffer_size = NRANGE * (NCHIRP + 4)

print("Acquiring...")
raw = sdr.rx()
if isinstance(raw, list):
    raw = raw[0]

# =============================================================================
# Split into chirps using the marker in the Q LSB
# =============================================================================
q = raw.imag.astype(np.int16)
st = np.flatnonzero(q & 1)

gaps = np.diff(st)
print(f"markers found: {len(st)}, spacing: {np.unique(gaps)}")
if not np.all(gaps == NRANGE):
    print("!! irregular spacing, samples may have been dropped")

# strip the LSB marker bit before using Q as data
data = raw.real + 1j * (np.floor(raw.imag / 2) * 2)

i0 = st[0]
if len(data) - i0 < NRANGE * NCHIRP:
    raise SystemExit("not enough data for a full frame, increase rx_buffer_size")

m = data[i0 : i0 + NRANGE * NCHIRP].reshape(NCHIRP, NRANGE)
print(f"frame: {m.shape}  (chirp, range sample)")

# =============================================================================
# Range FFT (fast time)
# =============================================================================
R = np.fft.fft(m * np.hanning(NRANGE), axis=1) / cic_droop(NRANGE)

# keep only the positive beat frequencies (target range maps to positive freq)
R = R[:, : NRANGE // 2]
rng_ax = np.arange(NRANGE // 2) * RES_M

# =============================================================================
# MTI -- subtract the slow-time mean to cancel stationary clutter and leakage
#
# A stationary target's echo is identical every chirp, so averaging recovers
# it and subtracting zeroes it out. A moving target's phase rotates chirp to
# chirp, so averaging mostly cancels and subtracting leaves it nearly intact.
# =============================================================================
R_mti = R - R.mean(axis=0, keepdims=True)

# =============================================================================
# Doppler FFT (slow time)
# =============================================================================
RD = np.fft.fftshift(np.fft.fft(R_mti * np.hanning(NCHIRP)[:, None], axis=0),
                      axes=0)

# dechirp used ref*conj(rx), so the Doppler axis needs flipping
RD = RD[::-1]
vel_ax = (np.arange(NCHIRP) - NCHIRP // 2) * RES_V

mag = db(RD)
mag -= mag.max()

# =============================================================================
# Plot
#
# both range profiles share one dB reference (the pre-MTI peak), so the
# suppression amount is directly visible
# =============================================================================
prof_raw = db(R[0])
prof_mti = db(R_mti[0])
ref = prof_raw.max()

fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))

ax[0].plot(rng_ax, prof_raw - ref)
ax[0].set_xlabel("Range (m)")
ax[0].set_ylabel("dB")
ax[0].set_title("Range profile (no MTI)")
ax[0].set_ylim(-80, 5)
ax[0].grid(alpha=0.3)

ax[1].plot(rng_ax, prof_mti - ref, color="C1")
ax[1].set_xlabel("Range (m)")
ax[1].set_ylabel("dB")
ax[1].set_title("Range profile (MTI, same reference)")
ax[1].set_ylim(-80, 5)
ax[1].grid(alpha=0.3)

im = ax[2].imshow(mag, aspect="auto", origin="lower", cmap="viridis",
                   extent=[rng_ax[0], rng_ax[-1], vel_ax[0], vel_ax[-1]],
                   vmin=-60, vmax=0)
ax[2].set_xlabel("Range (m)")
ax[2].set_ylabel("Velocity (m/s)")
ax[2].set_title("Range-Doppler")
plt.colorbar(im, ax=ax[2], label="dB")

plt.tight_layout()
plt.show()

# =============================================================================
# Report
# =============================================================================
print(f"\nMTI suppression: {ref - prof_mti.max():.1f} dB")
print(f"range resolution {RES_M:.2f} m,  velocity resolution {RES_V:.3f} m/s")
print(f"unambiguous max: range {rng_ax[-1]:.0f} m,  velocity +/-{abs(vel_ax[0]):.1f} m/s\n")

print("Strongest 5 points:")
for k in np.argsort(mag.ravel())[::-1][:5]:
    iv, ir = np.unravel_index(k, mag.shape)
    print(f"  {rng_ax[ir]:7.1f} m   {vel_ax[iv]:+7.2f} m/s   {mag[iv, ir]:6.1f} dB")
