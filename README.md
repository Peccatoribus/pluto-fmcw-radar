# pluto-fmcw-radar
A 2.35GHzFMCW radar based on PlutoSDR
## System Overview

| | |
|---|---|
| **SDR** | ADALM-PlutoSDR (integrated PA / LNA) — chirp generation and reception |
| **Carrier** | 2.35 GHz |
| **Bandwidth** | 10 MHz |
| **Waveform** | Linear-FM (LFM) chirp, FMCW |
| **Antenna** | Self-designed Yagi + a commercial circularly-polarized antenna |
| **Processing** | Python — dechirp, range FFT, range-Doppler FFT |

<!-- Optional: drop a signal-chain block diagram here if you make one. -->

---

## Results

![Range-Doppler map](range_doppler.png)

*Range-Doppler map produced by the Python processing chain at 10 MHz bandwidth. Target range and velocity are resolved; velocity measurement verified against a moving target.* <!-- add the actual range / speed you measured if you have it -->

---

## Custom Yagi Antenna

I designed the Yagi in **ANSYS HFSS**, fabricated it by hand, and verified it on a **VNA**. Measured performance agrees closely with simulation.

**Design (HFSS)**

![Yagi — HFSS model](yagi_hfss_model.png)
*HFSS model of the Yagi, designed for 2.35 GHz.*

![Simulated S11](yagi_s11_sim.png)
*Simulated S11: **−25 dB at 2.35 GHz**.*

**Build & measurement**

![Fabricated Yagi](yagi_photo.jpg)
*The fabricated antenna.*

![Measured S11 (VNA)](yagi_s11_measured.png)
*Measured S11: **−20 dB at 2.35 GHz** on the VNA — close agreement with the simulated result.*

---

## Other Antennas

![Earlier Yagi build](yagi_v1.jpg)
*[An earlier Yagi iteration — fill in: which band / what changed between this and the final design.]*

![Circularly-polarized antenna](cp_antenna.jpg)
*A commercial (off-the-shelf) circularly-polarized antenna used in the setup for [its role — e.g. the receive side / comparison]. Not a custom design.*

---

## How It Works

1. **Chirp generation** — a linear-FM chirp is synthesized and transmitted through the PlutoSDR.
2. **Reception** — the reflected signal is captured on the receive chain.
3. **Dechirp** — the received signal is mixed with a reference chirp; the resulting beat frequency encodes target range.
4. **Range-Doppler processing** — a range FFT, followed by a Doppler FFT across successive chirps, produces the range-Doppler map, giving both distance and velocity.

---

## Next Steps

Moving the dechirp from the host into the PlutoSDR's FPGA (Zynq programmable logic) so the full bandwidth can be processed on-board, working toward 60 MHz.

---

*Chen "CQ" Qiu — qiuchen@umich.edu*
