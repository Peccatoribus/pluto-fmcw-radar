# v1 — PC-side dechirp

The first working version of the radar. The Pluto only transmits the chirp
and digitizes the raw echo; every processing step (dechirp, range FFT,
Doppler FFT, live display) runs in Python on the host. This is the version
used to produce the range-Doppler result in the top-level README.

Since raw IQ has to be streamed off the board, the achievable bandwidth — and
therefore range resolution — is capped by the link, not by the AD9361
front end. See the trade-off note in the script's docstring for the numbers.
Moving dechirp onto the Pluto's own FPGA (in progress, tracked in the
top-level README) removes this cap.

## Requirements

```
pip install -r requirements.txt
```

`libiio` must also be installed system-wide (it backs `pyadi-iio`), and the
board must be running Pluto firmware with network access enabled.

## Usage

```
python fmcw_pluto_pc.py
```

Edit the constants at the top of the script for your setup (SDR address,
carrier frequency, sample rate, gains, display mode). Close the plot window
or press Ctrl+C to stop; the script shuts the transmitter down on exit.

For first bring-up, connect TX to RX through a 20-30 dB attenuator instead of
antennas — the leakage peak should land at range bin 0, confirming the
dechirp and buffer alignment are correct.
