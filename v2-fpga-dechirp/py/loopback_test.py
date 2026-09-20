#!/usr/bin/env python3
"""Pluto FMCW bring-up, step 1: TX1 --(attenuator)--> RX2 loopback.

Purpose:
  1. Confirm a chirp can be loaded into the TX cyclic DMA buffer.
  2. Confirm what's received is really a 48 MHz sweep.
  3. Use software dechirp to measure the TX/RX loop delay (needed later to
     calibrate absolute range).

Requires no FPGA changes -- runs on the stock bitstream.

Requirements:  pip install pyadi-iio matplotlib
"""

import numpy as np
import matplotlib.pyplot as plt
import adi

# =============================================================================
# Parameters
# =============================================================================
URI = "ip:192.168.1.10"

FS = 61_440_000      # sample rate
LO = 2_350_000_000   # carrier
BW = 48_000_000      # sweep bandwidth (baseband -24 to +24 MHz)
N = 32768            # samples per chirp
NCAP = 4             # capture this many chirp lengths

TX_GAIN = -50        # dB; with a 30 dB attenuator in line, -20 works too
RX_GAIN = 0          # dB
TX_AMP = 2 ** 14     # DAC full scale is ~2^15, leave half as headroom

RES_M = 3e8 / (2 * BW)  # 3.125 m range resolution

# =============================================================================
# Generate the chirp
# =============================================================================
n = np.arange(N)
f = -BW / 2 + BW * n / N              # instantaneous frequency, linear ramp
phase = 2 * np.pi * np.cumsum(f) / FS
chirp = np.exp(1j * phase)

tx_wave = (chirp * TX_AMP).astype(np.complex64)

print(f"chirp: {N} samples, {N / FS * 1e6:.1f} us, {-BW / 2 / 1e6:.0f} -> {+BW / 2 / 1e6:.0f} MHz")
print(f"slope S = {BW / (N / FS):.3e} Hz/s")

# =============================================================================
# Configure the Pluto
# =============================================================================
sdr = adi.ad9361(uri=URI)

sdr.sample_rate = FS
sdr.rx_rf_bandwidth = 56_000_000
sdr.tx_rf_bandwidth = 56_000_000
sdr.rx_lo = LO
sdr.tx_lo = LO

sdr.tx_enabled_channels = [0]     # TX1
sdr.rx_enabled_channels = [1]     # RX2 (switch to [0] if this errors)

sdr.tx_hardwaregain_chan0 = TX_GAIN
sdr.gain_control_mode_chan1 = "manual"
sdr.rx_hardwaregain_chan1 = RX_GAIN

sdr.rx_buffer_size = N * NCAP

# these tracking loops would mistake the chirp for something to correct;
# disable them
for ch in ("voltage0", "voltage1"):
    for attr in ("quadrature_tracking_en", "rf_dc_offset_tracking_en",
                 "bb_dc_offset_tracking_en"):
        try:
            sdr._ctrl.find_channel(ch).attrs[attr].value = "0"
        except Exception:
            pass

# =============================================================================
# Transmit (cyclic mode: load once, hardware repeats it forever)
# =============================================================================
sdr.tx_cyclic_buffer = True
sdr.tx_destroy_buffer()
sdr.tx(tx_wave)
print("TX cyclic buffer started")

# discard the first few buffers while AGC/filters settle
for _ in range(3):
    sdr.rx()
rx = sdr.rx()

print(f"received {len(rx)} samples, amplitude rms={np.abs(rx).mean():.0f} max={np.abs(rx).max():.0f}")

if np.abs(rx).max() > 30000:
    print("!! near saturation, lower TX_GAIN")
elif np.abs(rx).max() < 200:
    print("!! signal is weak, raise TX_GAIN by 5 dB and retry")

# =============================================================================
# Analysis 1: spectrogram -- should show a ramp from -24 to +24 MHz
# =============================================================================
plt.figure(figsize=(11, 4))

plt.subplot(1, 2, 1)
plt.specgram(rx[: N * 2], NFFT=1024, Fs=FS / 1e6, noverlap=512)
plt.ylabel("MHz")
plt.xlabel("us")
plt.title("Received chirp")

# =============================================================================
# Analysis 2: cross-correlation to find the loop delay
#
# TX plays cyclically, so one period of rx is a circular shift of the
# reference. A circular cross-correlation via FFT gives the delay as the
# peak position.
# =============================================================================
seg = rx[:N]
R = np.fft.ifft(np.fft.fft(seg) * np.conj(np.fft.fft(chirp)))
lag = int(np.argmax(np.abs(R)))

print(f"\nloop delay = {lag} samples = {lag / FS * 1e9:.0f} ns")
print(f"equivalent range offset = {lag * 3e8 / (2 * FS):.1f} m")

plt.subplot(1, 2, 2)
plt.plot(np.abs(R))
plt.axvline(lag, color="r", ls="--")
plt.xlabel("lag (samples)")
plt.title(f"cross-correlation, peak @ {lag}")
plt.tight_layout()
plt.show()

# =============================================================================
# Analysis 3: dechirp -- after alignment the beat frequency should be near 0
# =============================================================================
ref_aligned = np.roll(chirp, lag)
beat = seg * np.conj(ref_aligned)

S = np.fft.fftshift(np.fft.fft(beat * np.hanning(N)))
fax = np.fft.fftshift(np.fft.fftfreq(N, 1 / FS))

mag = 20 * np.log10(np.abs(S) + 1e-9)
mag -= mag.max()

pk_f = fax[np.argmax(mag)]
print(f"residual beat frequency = {pk_f / 1e3:.1f} kHz  (should be < 5 kHz if well aligned)")

plt.figure(figsize=(9, 4))
plt.plot(fax / 1e3, mag)
plt.xlim(-500, 500)
plt.ylim(-80, 5)
plt.xlabel("Beat frequency (kHz)")
plt.ylabel("dB")
plt.title("Spectrum after dechirp")
plt.grid(alpha=0.3)
plt.show()

sdr.tx_destroy_buffer()
