# v2 — FPGA dechirp (in progress)

[`v1-pc-dechirp/`](../v1-pc-dechirp/) streams raw IQ off the board and does
everything in Python, which caps bandwidth — and therefore range resolution
— at whatever the link can carry. The fix is to dechirp and decimate on the
Pluto's own Zynq 7020 before the data ever leaves the board, so only the
low-rate beat signal has to be streamed out. This folder is that FPGA
signal-processing core.

**Status:** the RTL is written and passes its testbench in behavioral
simulation. It has not yet been integrated into a Vivado block design or run
on real hardware — that's the next step, not something this folder claims is
finished.

## What's here

- `rtl/radar_dsp.sv` — the core: a chirp NCO/DDS, a dechirp (stretch
  processing) multiplier, and a CIC decimator, all pure SystemVerilog with no
  IP dependencies (~800 LUTs, 4 DSP48s, 2 BRAMs on a 7020). Designed for
  61.44 MSPS / 48 MHz bandwidth, decimating 128:1 down to 480 kSPS with
  exactly 256 range samples per chirp (3.13 m resolution). Every design
  decision — the NCO's fixed-point sizing, the dechirp pipeline stages, why
  the CIC's passband droop is left uncompensated — is explained inline.
- `tb/tb_radar_dsp.sv` — testbench: simulates a single point target with a
  known delay line and checks the range-FFT peak lands in the right bin.
- `create_project.tcl` — builds the Vivado simulation-only project from the
  RTL and testbench above. Re-run any time; it deletes and rebuilds.
- `data/sin_lut.mem` — the NCO's sine lookup table (4096 x 16-bit, generated
  by `py/gen_sin_lut.py`; committed here since it's small and deterministic).
- `py/` — host-side tooling:
  - `gen_sin_lut.py` regenerates `data/sin_lut.mem`.
  - `check_radar.py` FFTs the testbench's simulation output and verifies the
    peak location (see Results below).
  - `loopback_test.py` — bring-up test that needs no custom bitstream: loads
    a chirp into TX, loops it back into RX through an attenuator, and
    measures the TX/RX loop delay.
  - `rx_marker_check.py`, `rd_single.py`, `rd_live.py` — capture and
    range-Doppler tools written against `radar_dsp.sv`'s output format
    (chirp-boundary marker in the Q LSB, two RX channels). These need a
    board actually running a bitstream with `radar_dsp` integrated, which
    doesn't exist yet — they're here ready for that point.

## Results

Running the testbench (`launch_simulation` after `create_project.tcl`) and
then `python py/check_radar.py` against the resulting `radar_out.txt`:

```
total samples: 1324
chirp starts: [256 512 768 1024 1280]
start spacing: [256 256 256 256]   <- all 256, as expected

peak range bin: 25   expected: 25
peak range    : 78.125 m
>>> PASS
```

The testbench places a simulated target at a 32-cycle delay (78.125 m); the
recovered range-FFT peak lands exactly there.

## Building / simulating

Requires Vivado 2022.2 (SystemVerilog + `xsim`, no other IP).

```
python py/gen_sin_lut.py        # writes sin_lut.mem into the cwd; move/copy
                                 # it to data/sin_lut.mem if regenerating
vivado -mode batch -source create_project.tcl
# then, inside Vivado: launch_simulation
python py/check_radar.py        # verify the peak, see the plot
```

## Next steps

1. Instantiate `radar_dsp` inside the Pluto's Vivado block design (in place
   of, or alongside, the stock `axi_ad9361` <-> `cpack`/`dac_fifo` path),
   wire up `bypass`, and generate a bitstream.
2. Bring up on hardware with `bypass=1` first to confirm the integration
   didn't break the stock signal path, then `bypass=0`.
3. Run `rx_marker_check.py` to confirm the chirp-boundary marker appears
   every 256 samples, then `rd_single.py` / `rd_live.py` for range-Doppler.
