#!/usr/bin/env python3
"""PlutoSDR FMCW radar -- live range-Doppler display (requires the
radar_dsp.sv bitstream running on the board; see the top-level README for
integration status).
"""

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore
import adi

URI = "ip:192.168.2.1"
NRANGE = 256
NCHIRP = 128              # smaller than rd_single.py for a live ~15 fps
B = 48e6
T_CHIRP = 32768 / 61.44e6
LAMBDA = 3e8 / 2.35e9

RES_M = 3e8 / (2 * B)
RES_V = LAMBDA / (2 * NCHIRP * T_CHIRP)

NR = NRANGE // 2      # display positive beat frequencies only


def cic_droop(n, R=128, N=4):
    f = np.fft.fftfreq(n)
    x = np.pi * f
    h = np.ones(n)
    nz = np.abs(x) > 1e-12
    h[nz] = np.abs(np.sin(x[nz]) / (R * np.sin(x[nz] / R))) ** N
    return h


DROOP = cic_droop(NRANGE)
WR = np.hanning(NRANGE)
WD = np.hanning(NCHIRP)[:, None]
RNG_AX = np.arange(NR) * RES_M
VMAX = NCHIRP // 2 * RES_V

# =============================================================================
sdr = adi.ad9361(uri=URI)
sdr.rx_enabled_channels = [0, 1]
sdr.rx_buffer_size = NRANGE * (NCHIRP + 4)

app = pg.mkQApp("Pluto FMCW Radar")
win = pg.GraphicsLayoutWidget(show=True, title="Pluto FMCW Radar")
win.resize(1200, 520)

# ---- Range-Doppler ----
p1 = win.addPlot(title="Range-Doppler (MTI)")
img = pg.ImageItem()
p1.addItem(img)
p1.setLabel("bottom", "Range", units="m")
p1.setLabel("left", "Velocity", units="m/s")
img.setRect(QtCore.QRectF(0, -VMAX, NR * RES_M, 2 * VMAX))
img.setLookupTable(pg.colormap.get("viridis").getLookupTable())
img.setLevels([-55, 0])

# ---- Range profile ----
p2 = win.addPlot(title="Range profile   white = before MTI, yellow = after MTI")
p2.setLabel("bottom", "Range", units="m")
p2.setLabel("left", "dB")
p2.setYRange(-80, 5)
p2.showGrid(x=True, y=True, alpha=0.3)
curve_raw = p2.plot(pen=pg.mkPen("w", width=1))
curve_mti = p2.plot(pen=pg.mkPen("y", width=2))


def update():
    try:
        raw = sdr.rx()
        if isinstance(raw, list):
            raw = raw[0]
    except Exception as e:
        print("rx failed:", e)
        return

    q = raw.imag.astype(np.int16)
    st = np.flatnonzero(q & 1)
    if len(st) == 0:
        return

    data = raw.real + 1j * (np.floor(raw.imag / 2) * 2)
    i0 = st[0]
    if len(data) - i0 < NRANGE * NCHIRP:
        return

    m = data[i0 : i0 + NRANGE * NCHIRP].reshape(NCHIRP, NRANGE)

    # range FFT
    R = (np.fft.fft(m * WR, axis=1) / DROOP)[:, :NR]

    # MTI
    R_mti = R - R.mean(axis=0, keepdims=True)

    # Doppler FFT
    RD = np.fft.fftshift(np.fft.fft(R_mti * WD, axis=0), axes=0)[::-1]

    mag = 20 * np.log10(np.abs(RD) + 1e-12)
    mag -= mag.max()
    img.setImage(mag.T, autoLevels=False)

    # both profiles share one dB reference
    pr = 20 * np.log10(np.abs(R).max(axis=0) + 1e-12)
    pm = 20 * np.log10(np.abs(R_mti).max(axis=0) + 1e-12)
    ref = pr.max()
    curve_raw.setData(RNG_AX, pr - ref)
    curve_mti.setData(RNG_AX, pm - ref)


timer = QtCore.QTimer()
timer.timeout.connect(update)
timer.start(50)

if __name__ == "__main__":
    pg.exec()
