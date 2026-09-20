#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FMCW radar demo -- full PC-side processing (no FPGA dechirp required).

OVERVIEW
    Chirp synthesis, dechirp, range FFT, Doppler FFT and live plotting all run
    in Python on the host. The ADALM-Pluto is used only as a transmitter and as
    a raw IQ digitiser; the raw stream is pulled over the network interface.

    Work is in progress to move dechirp onto the Pluto's on-board Zynq FPGA;
    this script is the PC-side baseline that stands on its own until then.

SIGNAL CHAIN
    host chirp  ->   Pluto TX (cyclic, LO = CENTER_FREQ)   ->    TX antenna
                                                                        |
                                                                      target
                                                                        v
    live plot <- range/Doppler FFT <- dechirp <- Pluto RX raw IQ <- RX antenna

DESIGN TRADE-OFF (why fine range resolution is not reachable here)
    Streaming raw IQ requires fs >= B, and the link carries 4 bytes per complex
    sample. A gigabit link sustains roughly 90-110 MB/s, which caps fs near
    25-30 MSPS and therefore caps B. Since dR = c / (2B), range resolution is
    limited to the 10-40 m class. Metre-class resolution requires on-FPGA
    dechirp so that only the low-rate beat signal is streamed back. This script
    exists to validate the complete chain end to end with zero FPGA risk, and to
    serve as a baseline for that later work.

REQUIREMENTS
    pip install pyadi-iio numpy matplotlib
    libiio must be installed system-wide; the board must run Pluto firmware.

HARDWARE SETUP
    TX SMA -> transmit antenna, RX SMA -> receive antenna (separate antennas or
    a circulator). For first bring-up, connect TX to RX through a 20-30 dB
    attenuator: the leakage peak should appear at range bin 0, which confirms
    that dechirp and alignment are correct before switching to antennas.

USAGE
    python fmcw_pluto_pc.py
    Close the figure window or press Ctrl+C to stop. TX is shut down on exit.

WHAT TO CHANGE FOR YOUR OWN SETUP
    SDR_URI           board address, e.g. "ip:192.168.2.1" over USB
    CENTER_FREQ       carrier frequency
    SAMPLE_RATE       raises B and improves dR, but increases the link load;
                      start low, raise it until packets start dropping
    RX_GAIN_DB        raise for weak echoes, lower if the ADC saturates
    TX_GAIN_DB        keep low when TX and RX are cable-connected
    MODE              "range_doppler" or "range_profile"
    CHIRPS_PER_FRAME  Doppler bin count; larger means finer velocity
                      resolution but a slower frame rate
    R_DISPLAY_MAX / Y_DB_MIN / Y_DB_MAX / V_DISPLAY_MAX   plot scaling only

RANGE MAPPING (for later parameter checks)
    Beat frequency of a target: f_b = slope * tau, with tau = 2R/c.
    Range-FFT bin m maps to f_b = m * fs / N, and N = fs * T, so
        R = m * c / (2B)  ->  dR = c / (2B), independent of N
        R_max = (N/2) * dR
"""

import collections
import sys
from dataclasses import dataclass

import numpy as np
import matplotlib.pyplot as plt

try:
    import adi  # pyadi-iio
except ImportError:  # pragma: no cover
    sys.exit("pyadi-iio not found. Install it with: pip install pyadi-iio")


# =============================================================================
# Configuration
# =============================================================================

# --- Connection ---
SDR_URI = "ip:192.168.1.10"

# --- RF ---
CENTER_FREQ = int(2.35e9)

# --- Sample rate and sweep bandwidth ---
# fs drives the link load (fs * 4 bytes/s) and bounds B; B sets dR = c / (2B).
# Reference points with B = 0.8 * fs:
#   fs =  5 MSPS ->  20 MB/s -> B =  4 MHz -> dR = 37.5 m  (safest starting point)
#   fs = 10 MSPS ->  40 MB/s -> B =  8 MHz -> dR = 18.8 m
#   fs = 20 MSPS ->  80 MB/s -> B = 16 MHz -> dR =  9.4 m  (watch for drops)
#   fs = 25 MSPS -> 100 MB/s -> B = 20 MHz -> dR =  7.5 m  (likely unstable)
SAMPLE_RATE = int(10e6)
CHIRP_BW = int(0.8 * SAMPLE_RATE)  # 20 % margin for the anti-alias filter skirt

# --- Chirp length ---
# N = fs * T. It sets the number of range gates and the frame duration, not dR.
# Powers of two keep the FFT fast.
N_PER_CHIRP = 4096

# --- Chirps per frame ---
# Range-profile mode: 8-16 is enough for coherent integration.
# Range-Doppler mode: this is the Doppler bin count M and sets the velocity
# resolution dv = lambda / (2 * M * T_pri). Unambiguous velocity depends only on
# T_pri, not on M, and indoor targets leave large margin, so M can be traded
# freely for finer dv at the cost of frame rate.
CHIRPS_PER_FRAME = 256

# --- Gains ---
RX_GAIN_DB = 50   # manual RX gain, AGC disabled to avoid gain hunting
TX_GAIN_DB = -50  # roughly -89..0 dB, 0 is maximum

# --- Display ---
MODE = "range_doppler"  # "range_doppler" or "range_profile"
R_DISPLAY_MAX = 300.0   # range axis limit, m
Y_DB_MIN = 30.0         # colour/amplitude floor, set just below the noise floor
Y_DB_MAX = 130.0        # ceiling, set just above the leakage peak
V_DISPLAY_MAX = 5.0     # velocity axis zoom in m/s, None for full scale
WARMUP_FRAMES = 3       # discarded while the data path settles

# --- Range-profile mode options ---
CLUTTER_REMOVAL = False  # True highlights movers, False shows the raw profile
CLUTTER_ALPHA = 0.95     # background averaging factor, closer to 1 is slower
MONITOR_BIN = None       # bin to watch, None selects the strongest one
MONITOR_SKIP_BINS = 3    # bins skipped during auto-selection (leakage at DC)
HIST_LEN = 200           # frames kept in the phase history plot
IQ_TRAIL = 60            # points kept in the I/Q trail

C = 3e8


# =============================================================================
# Parameters
# =============================================================================

@dataclass
class RadarParams:
    """Derived radar parameters and axes.

    Attributes:
        fs: Sample rate in Hz.
        bw: Chirp sweep bandwidth in Hz.
        n: Samples per chirp.
        n_chirps: Chirps per acquired frame.
        fc: Carrier frequency in Hz.
        t_chirp: Chirp duration in s, also the pulse repetition interval.
        slope: Frequency sweep rate in Hz/s.
        range_res: Range resolution in m.
        range_max: Maximum unambiguous range in m.
        wavelength: Carrier wavelength in m.
        range_axis: Range of every positive-frequency FFT bin, in m.
        n_disp: Number of range bins actually plotted.
    """

    fs: float
    bw: float
    n: int
    n_chirps: int
    fc: float
    t_chirp: float
    slope: float
    range_res: float
    range_max: float
    wavelength: float
    range_axis: np.ndarray
    n_disp: int


def derive_params(fs, bw, n, n_chirps, fc, r_display_max):
    """Compute all derived radar quantities from the user configuration.

    Args:
        fs: Sample rate in Hz.
        bw: Chirp sweep bandwidth in Hz, must not exceed fs.
        n: Samples per chirp.
        n_chirps: Chirps per frame.
        fc: Carrier frequency in Hz.
        r_display_max: Requested plot range limit in m.

    Returns:
        A populated RadarParams instance.

    Raises:
        ValueError: If bw exceeds fs, which would alias the sweep.
    """
    if bw > fs:
        raise ValueError("CHIRP_BW must not exceed SAMPLE_RATE")

    t_chirp = n / fs
    range_res = C / (2.0 * bw)
    range_axis = np.arange(n // 2) * range_res
    n_disp = max(int(min(r_display_max, (n // 2) * range_res) / range_res), 4)

    return RadarParams(
        fs=fs,
        bw=bw,
        n=n,
        n_chirps=n_chirps,
        fc=fc,
        t_chirp=t_chirp,
        slope=bw / t_chirp,
        range_res=range_res,
        range_max=(n // 2) * range_res,
        wavelength=C / fc,
        range_axis=range_axis,
        n_disp=n_disp,
    )


def print_summary(p):
    """Print the configuration summary and a bin-to-range lookup table.

    Args:
        p: RadarParams instance to describe.

    Returns:
        None.
    """
    print("=" * 60)
    print(f" Carrier          : {p.fc / 1e9:.3f} GHz")
    print(f" Sample rate      : {p.fs / 1e6:.2f} MSPS  -> {p.fs * 4 / 1e6:.1f} MB/s")
    print(f" Sweep bandwidth  : {p.bw / 1e6:.2f} MHz")
    print(f" Chirp duration   : {p.t_chirp * 1e6:.1f} us  (N = {p.n})")
    print(f" Range resolution : {p.range_res:.2f} m")
    print(f" Maximum range    : {p.range_max:.0f} m  ({p.n // 2} gates)")
    print("-" * 60)
    print(" bin -> range (R = bin * dR):")
    for b in range(min(p.n_disp, 16)):
        print(f"   bin {b:2d} -> {b * p.range_res:7.2f} m")
    print("=" * 60)


# =============================================================================
# Waveform
# =============================================================================

def build_chirp(p):
    """Generate the baseband chirp and the matched processing kernels.

    The instantaneous frequency sweeps linearly from -B/2 to +B/2, so the phase
    is 2*pi*(-B/2*t + 0.5*slope*t^2). The same unit-amplitude waveform is used
    both as the transmit signal and as the dechirp reference.

    Args:
        p: RadarParams instance.

    Returns:
        Tuple of (chirp_bb, tx_waveform, ref_fft, window):
            chirp_bb: Complex unit-amplitude chirp, length N.
            tx_waveform: Same chirp scaled to the int16 range expected by TX.
            ref_fft: FFT of chirp_bb, cached for circular cross-correlation.
            window: Hann window used before the range FFT to suppress the
                sidelobes of the strong leakage peak.
    """
    t = np.arange(p.n) / p.fs
    phase = 2 * np.pi * (-p.bw / 2 * t + 0.5 * p.slope * t ** 2)
    chirp_bb = np.exp(1j * phase)

    tx_waveform = (2 ** 14) * chirp_bb  # headroom left below full scale
    ref_fft = np.fft.fft(chirp_bb)
    window = np.hanning(p.n)

    return chirp_bb, tx_waveform, ref_fft, window


# =============================================================================
# Radio
# =============================================================================

def open_sdr(uri, p, rx_gain_db, tx_gain_db, tx_waveform):
    """Connect to the Pluto, configure both chains and start cyclic transmit.

    Args:
        uri: libiio context URI, e.g. "ip:192.168.1.10".
        p: RadarParams instance.
        rx_gain_db: Manual receive gain in dB.
        tx_gain_db: Transmit gain in dB, negative, 0 is maximum.
        tx_waveform: Complex waveform loaded into the cyclic TX buffer.

    Returns:
        The configured adi.Pluto instance, already transmitting.
    """
    print(f"Connecting to {uri} ...")
    sdr = adi.Pluto(uri)
    sdr.sample_rate = int(p.fs)

    sdr.rx_lo = int(p.fc)
    sdr.rx_rf_bandwidth = int(p.fs)
    sdr.rx_enabled_channels = [0]
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = rx_gain_db
    sdr.rx_buffer_size = p.n * p.n_chirps

    sdr.tx_lo = int(p.fc)
    sdr.tx_rf_bandwidth = int(p.fs)
    sdr.tx_hardwaregain_chan0 = tx_gain_db
    sdr.tx_cyclic_buffer = True
    sdr.tx_destroy_buffer()  # clear any buffer left by a previous run
    sdr.tx(tx_waveform)

    print("TX running, chirp repeating continuously.")
    return sdr


def shutdown_sdr(sdr):
    """Stop transmission and release the TX buffer.

    Args:
        sdr: adi.Pluto instance returned by open_sdr.

    Returns:
        None.
    """
    sdr.tx_destroy_buffer()
    print("TX stopped, cleanup done.")


# =============================================================================
# Signal processing
# =============================================================================

def estimate_chirp_offset(rx_chunk, ref_fft):
    """Locate the chirp start inside the receive buffer.

    TX repeats cyclically while RX is captured asynchronously, so the first
    buffer sample rarely coincides with a chirp start. Because both chains share
    a clock the offset is essentially constant across frames. Without the
    correction a buffer straddles two chirps and the phase discontinuity at the
    seam smears the range spectrum.

    Args:
        rx_chunk: One chirp worth of raw complex samples.
        ref_fft: Cached FFT of the reference chirp.

    Returns:
        Integer sample offset of the chirp start.
    """
    corr = np.fft.ifft(np.fft.fft(rx_chunk) * np.conj(ref_fft))
    return int(np.argmax(np.abs(corr)))


def process_frame(rx, p, chirp_bb, ref_fft, window):
    """Convert one raw IQ frame into a complex range spectrum per chirp.

    Steps: align the buffer, reshape into chirps, dechirp against the reference,
    apply the window, then take the range FFT along fast time. The slow-time
    axis is preserved so the caller can either average it into a range profile
    or run a Doppler FFT along it.

    Args:
        rx: Raw complex samples, at least n_chirps * N long.
        p: RadarParams instance.
        chirp_bb: Baseband reference chirp.
        ref_fft: Cached FFT of chirp_bb.
        window: Range window, length N.

    Returns:
        Complex array of shape (n_chirps, N//2); rows are slow time, columns are
        positive-frequency range bins.

    Note:
        If range profiles look smeared, try np.roll(rx, +lag) instead of -lag;
        the sign convention depends on the correlation peak definition.
    """
    lag = estimate_chirp_offset(rx[:p.n], ref_fft)
    rx = np.roll(rx, -lag)

    rx = rx[:p.n_chirps * p.n].reshape(p.n_chirps, p.n)
    beat = np.conj(rx) * chirp_bb[None, :]
    spec = np.fft.fft(beat * window[None, :], axis=1)

    return spec[:, :p.n // 2]


# =============================================================================
# Display: range-Doppler
# =============================================================================

def run_range_doppler(sdr, p, chirp_bb, ref_fft, window):
    """Stream frames and display a live range-Doppler map.

    Stationary clutter and TX leakage sit at zero Doppler, so any response that
    separates from the centre line is a genuine moving target. Keep the antennas
    fixed and move a reflector to confirm detections.

    Args:
        sdr: Transmitting adi.Pluto instance.
        p: RadarParams instance.
        chirp_bb: Baseband reference chirp.
        ref_fft: Cached FFT of chirp_bb.
        window: Range window.

    Returns:
        None. Runs until the figure is closed or Ctrl+C is pressed.
    """
    doppler_hz = np.fft.fftshift(np.fft.fftfreq(p.n_chirps, d=p.t_chirp))
    vel_axis = doppler_hz * p.wavelength / 2.0
    v_res = p.wavelength / (2 * p.n_chirps * p.t_chirp)
    print(f"Velocity resolution {v_res:.2f} m/s, "
          f"unambiguous velocity +/-{vel_axis.max():.1f} m/s")

    fig, ax = plt.subplots(figsize=(10, 6))
    image = ax.imshow(
        np.zeros((p.n_chirps, p.n_disp)),
        origin="lower",
        aspect="auto",
        extent=[0, p.range_axis[p.n_disp - 1], vel_axis[0], vel_axis[-1]],
        vmin=Y_DB_MIN,
        vmax=Y_DB_MAX,
        cmap="turbo",
    )
    ax.axhline(0, color="white", lw=0.6, alpha=0.5)
    ax.set_xlabel("Range (m)")
    ax.set_ylabel("Velocity (m/s)")
    ax.set_title("Range-Doppler map: responses off the zero-Doppler line are movers")
    if V_DISPLAY_MAX is not None:
        vlim = min(V_DISPLAY_MAX, np.abs(vel_axis).max())
        ax.set_ylim(-vlim, vlim)  # zoom only, ambiguity limit is unchanged
    fig.colorbar(image, ax=ax, label="dB")
    fig.tight_layout()

    print("Acquiring. Keep the antennas fixed and move a reflector.")
    frame_idx = 0
    while True:
        rx = sdr.rx()
        frame_idx += 1
        if frame_idx <= WARMUP_FRAMES:
            continue

        spec = process_frame(rx, p, chirp_bb, ref_fft, window)
        rd = np.fft.fftshift(np.fft.fft(spec, axis=0), axes=0)
        rd_db = 20 * np.log10(np.abs(rd[:, :p.n_disp]) + 1e-9)

        image.set_data(rd_db)
        fig.canvas.draw_idle()
        plt.pause(0.001)
        if not plt.fignum_exists(fig.number):
            break


# =============================================================================
# Display: range profile with bin monitor
# =============================================================================

def _build_profile_figure(p):
    """Create the range-profile figure and return its artists.

    Args:
        p: RadarParams instance.

    Returns:
        Tuple of (fig, axes_dict, artists_dict) used by run_range_profile.
    """
    fig = plt.figure(figsize=(11, 7))
    grid = fig.add_gridspec(2, 2, height_ratios=[1.3, 1])

    ax_range = fig.add_subplot(grid[0, :])
    (profile_line,) = ax_range.plot(p.range_axis[:p.n_disp], np.zeros(p.n_disp))
    (bin_marker,) = ax_range.plot([], [], "rv", ms=9)
    ax_range.set_xlabel("Range (m)")
    ax_range.set_ylabel("Magnitude (dB)")
    ax_range.set_title(
        f"FMCW range profile | dR = {p.range_res:.1f} m | "
        f"fs = {p.fs / 1e6:.0f} MSPS | B = {p.bw / 1e6:.0f} MHz"
    )
    ax_range.grid(True, alpha=0.3)
    ax_range.set_xlim(0, p.range_axis[p.n_disp - 1])
    ax_range.set_ylim(Y_DB_MIN, Y_DB_MAX)

    ax_phase = fig.add_subplot(grid[1, 0])
    (phase_line,) = ax_phase.plot([], [], lw=1.5)
    ax_phase.set_xlabel("Frame")
    ax_phase.set_ylabel("Phase (rad)")
    ax_phase.set_title("Monitored bin phase")
    ax_phase.set_xlim(0, HIST_LEN)
    ax_phase.set_ylim(-np.pi, np.pi)
    ax_phase.grid(True, alpha=0.3)

    ax_iq = fig.add_subplot(grid[1, 1])
    (iq_trail,) = ax_iq.plot([], [], "-", lw=1, alpha=0.5)
    (iq_point,) = ax_iq.plot([], [], "o", ms=8)
    ax_iq.set_xlabel("I")
    ax_iq.set_ylabel("Q")
    ax_iq.set_title("Monitored bin I/Q")
    ax_iq.axhline(0, color="gray", lw=0.5)
    ax_iq.axvline(0, color="gray", lw=0.5)
    ax_iq.set_aspect("equal", "box")
    ax_iq.grid(True, alpha=0.3)

    fig.tight_layout()
    axes = {"range": ax_range, "phase": ax_phase, "iq": ax_iq}
    artists = {
        "profile": profile_line,
        "marker": bin_marker,
        "phase": phase_line,
        "trail": iq_trail,
        "point": iq_point,
    }
    return fig, axes, artists


def run_range_profile(sdr, p, chirp_bb, ref_fft, window):
    """Stream frames and display a live range profile plus a bin monitor.

    The monitor is a target-validation aid: when a reflector is moved, the
    complex value of a true echo bin rotates smoothly in the I/Q plane, whereas
    DC offset, leakage and spurs hold a fixed phase. Pick a bin well away from
    zero range.

    Args:
        sdr: Transmitting adi.Pluto instance.
        p: RadarParams instance.
        chirp_bb: Baseband reference chirp.
        ref_fft: Cached FFT of chirp_bb.
        window: Range window.

    Returns:
        None. Runs until the figure is closed or Ctrl+C is pressed.
    """
    fig, axes, artists = _build_profile_figure(p)

    clutter_bg = None
    history = collections.deque(maxlen=HIST_LEN)
    monitor_bin = MONITOR_BIN
    iq_lim = None

    print("Acquiring. Close the figure or press Ctrl+C to stop.")
    frame_idx = 0
    while True:
        rx = sdr.rx()
        frame_idx += 1
        if frame_idx <= WARMUP_FRAMES:
            continue

        spec = process_frame(rx, p, chirp_bb, ref_fft, window).mean(axis=0)

        if CLUTTER_REMOVAL:
            if clutter_bg is None:
                clutter_bg = spec.copy()
            else:
                clutter_bg = CLUTTER_ALPHA * clutter_bg + (1 - CLUTTER_ALPHA) * spec
            shown = spec - clutter_bg
        else:
            shown = spec

        mag_db = 20 * np.log10(np.abs(shown[:p.n_disp]) + 1e-9)
        artists["profile"].set_ydata(mag_db)

        if monitor_bin is None:
            segment = np.abs(spec[MONITOR_SKIP_BINS:p.n_disp])
            monitor_bin = MONITOR_SKIP_BINS + int(np.argmax(segment))
            print(f"Monitoring bin {monitor_bin} "
                  f"({p.range_axis[monitor_bin]:.1f} m), override with MONITOR_BIN")
        if monitor_bin < p.n_disp:
            artists["marker"].set_data([p.range_axis[monitor_bin]], [mag_db[monitor_bin]])

        history.append(spec[monitor_bin])
        hist = np.array(history)
        artists["phase"].set_data(np.arange(len(hist)), np.angle(hist))

        trail = hist[-IQ_TRAIL:]
        artists["trail"].set_data(trail.real, trail.imag)
        artists["point"].set_data([trail.real[-1]], [trail.imag[-1]])

        limit = np.abs(hist).max() * 1.3
        if iq_lim is None or limit > iq_lim:
            iq_lim = limit
            axes["iq"].set_xlim(-iq_lim, iq_lim)
            axes["iq"].set_ylim(-iq_lim, iq_lim)

        fig.canvas.draw_idle()
        plt.pause(0.001)
        if not plt.fignum_exists(fig.number):
            break


# =============================================================================
# Entry point
# =============================================================================

def main():
    """Set up the radar, run the selected display mode and clean up on exit.

    Returns:
        None.
    """
    params = derive_params(
        fs=SAMPLE_RATE,
        bw=CHIRP_BW,
        n=N_PER_CHIRP,
        n_chirps=CHIRPS_PER_FRAME,
        fc=CENTER_FREQ,
        r_display_max=R_DISPLAY_MAX,
    )
    print_summary(params)

    chirp_bb, tx_waveform, ref_fft, window = build_chirp(params)
    sdr = open_sdr(SDR_URI, params, RX_GAIN_DB, TX_GAIN_DB, tx_waveform)

    plt.ion()
    try:
        if MODE == "range_doppler":
            run_range_doppler(sdr, params, chirp_bb, ref_fft, window)
        elif MODE == "range_profile":
            run_range_profile(sdr, params, chirp_bb, ref_fft, window)
        else:
            raise ValueError(f"Unknown MODE: {MODE!r}")
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        shutdown_sdr(sdr)
        plt.ioff()


if __name__ == "__main__":
    main()
