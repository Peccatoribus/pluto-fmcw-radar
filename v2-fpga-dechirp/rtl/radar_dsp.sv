`timescale 1ns / 1ps
//=============================================================================
//  radar_dsp.sv  --  PlutoSDR 2.35 GHz FMCW radar signal processing
//
//  Pure SystemVerilog, zero IP dependencies. The only external file is the
//  plain-text sin_lut.mem.
//
//  Signal chain:
//    chirp_nco  ---+--> dac_data_i/q            transmit
//                  +--> dechirp reference        stretch processing
//    adc_data_i/q --> dechirp --> cic_decim --> out_data_i/q
//
//  Parameters (61.44 MSPS):
//    chirp = 32768 samples = 533.33 us, PRF 1875 Hz
//    baseband sweep -24 MHz -> +24 MHz, B = 48 MHz, S = 9.0e10 Hz/s
//    decimate by 128 -> 480 kSPS, exactly 256 samples per chirp
//    range resolution 3.13 m
//
//  Resources: 4 DSP48s, 2 BRAMs, ~800 LUTs (2% of a 7020)
//=============================================================================


//=============================================================================
//  1. chirp_nco  --  linear-FM waveform generator
//
//  Dual accumulator:
//    ftw   resets to FTW_START every chirp, += FTW_STEP every enabled tick
//          (frequency, linear ramp)
//    phase resets to 0 every chirp,          += ftw every enabled tick
//          (phase, quadratic -> the chirp)
//
//  Instantaneous frequency = ftw / 2^32 * fs, so:
//    FTW_START = -24e6 / 61.44e6 * 2^32 = -1677721600
//    FTW_STEP  = 48e6 / 61.44e6 * 2^32 / 32768 = 102400
//    Check: 32768 * 102400 = 3355443200 = 2 * 1677721600  <- sweep closes
//    exactly, zero accumulated error.
//
//  The top 12 bits of phase index a 4096-entry sine table.
//  cos is read at index+1024 (90 degrees away) from the same table -- the
//  BRAM is dual-port, so both addresses can be read in the same cycle.
//
//  The BRAM has a 1-cycle read latency, plus an output register, for 2
//  cycles total. `start` has to be delayed by the same number of cycles or
//  the marker lands on the wrong sample.
//
//  Note: TX and the dechirp reference share this same signal, so both see
//  the same 2-cycle delay and their relative timing is unaffected -- these
//  2 cycles only shift where the `start` marker falls, not the ranging
//  result.
//=============================================================================
module chirp_nco #(
    parameter int     CHIRP_LEN = 32768,
    parameter longint FTW_START = -1677721600,
    parameter longint FTW_STEP  = 102400,
    parameter         LUT_FILE  = "sin_lut.mem"
)(
    input  logic               clk,
    input  logic               rst_n,
    input  logic               en,          // sample-rate gate, tie to adc_valid
    output logic signed [15:0] cos_out,
    output logic signed [15:0] sin_out,
    output logic               start_out    // marks the first sample of each chirp, aligned with the output
);

    localparam int LAT = 2;

    //-------------------------------------------------------------------------
    // Dual accumulator
    //-------------------------------------------------------------------------
    logic signed [31:0] ftw;
    logic        [31:0] phase;
    logic        [15:0] cnt;
    logic               start_pre;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            ftw       <= FTW_START;
            phase     <= '0;
            cnt       <= '0;
            start_pre <= 1'b0;
        end else if (en) begin
            if (cnt == CHIRP_LEN - 1) begin
                // next cycle is sample 0 of the new chirp
                ftw       <= FTW_START;
                phase     <= '0;
                cnt       <= '0;
                start_pre <= 1'b1;
            end else begin
                ftw       <= ftw + FTW_STEP;
                phase     <= phase + ftw;
                cnt       <= cnt + 1'b1;
                start_pre <= 1'b0;
            end
        end
    end

    //-------------------------------------------------------------------------
    // Sine ROM (dual-port, sin and cos read in the same cycle)
    //-------------------------------------------------------------------------
    (* rom_style = "block" *) logic [15:0] lut [0:4095];

    initial $readmemh(LUT_FILE, lut);

    logic [11:0] idx_s, idx_c;
    assign idx_s = phase[31:20];
    assign idx_c = phase[31:20] + 12'd1024;   // +90 degrees

    logic [15:0] s1, c1, s2, c2;

    always_ff @(posedge clk) begin
        if (en) begin
            s1 <= lut[idx_s];   // BRAM read, 1 cycle
            c1 <= lut[idx_c];
            s2 <= s1;           // output register, 1 cycle
            c2 <= c1;
        end
    end

    //-------------------------------------------------------------------------
    // `start` is delayed by LAT cycles alongside the data
    //-------------------------------------------------------------------------
    logic [LAT-1:0] s_dly;

    always_ff @(posedge clk) begin
        if (!rst_n)  s_dly <= '0;
        else if (en) s_dly <= {s_dly[LAT-2:0], start_pre};
    end

    assign cos_out   = c2;
    assign sin_out   = s2;
    assign start_out = s_dly[LAT-1];

endmodule


//=============================================================================
//  2. dechirp  --  stretch processing
//
//  out = ref * conj(rx)
//
//    rx  = a + jb   echo
//    ref = c + jd   reference (NCO output)
//
//    out_i = c*a + d*b
//    out_q = d*a - c*b
//
//  Math: subtracting the phase of two chirps with the same slope cancels the
//  t^2 term completely, leaving only the linear term. The result is a single
//  beat tone at fb = S*tau = 2*R*S/c. A target at 150 m produces a 90 kHz
//  beat -- the 48 MHz swept signal collapses down to 90 kHz.
//
//  Using ref*conj(rx) rather than the other way round makes target range
//  map to a "positive" beat frequency, which is more intuitive to look at
//  on a spectrum. The cost is that the Doppler axis comes out flipped; the
//  PC side just reverses it.
//
//  SHIFT=15: ref at full scale is ~32767 =~ 2^15, dividing it out keeps the
//  overall gain at 1.
//
//  3-stage pipeline, 4 DSP48s
//=============================================================================
module dechirp #(
    parameter int SHIFT = 15
)(
    input  logic               clk,
    input  logic               rst_n,

    input  logic               in_valid,
    input  logic signed [15:0] rx_i,
    input  logic signed [15:0] rx_q,
    input  logic signed [15:0] ref_i,
    input  logic signed [15:0] ref_q,
    input  logic               in_start,

    output logic               out_valid,
    output logic signed [15:0] out_i,
    output logic signed [15:0] out_q,
    output logic               out_start
);

    //-------------------------------------------------------------------------
    // stage 0: input register
    //
    // clk here is the 245.76 MHz LVDS DATA_CLK, not the sample rate.
    // Without this input register the whole multiplier array's delay would
    // be crammed into one cycle, which doesn't close timing at 4 ns.
    // Vivado absorbs this stage into the DSP48's AREG/BREG.
    //-------------------------------------------------------------------------
    logic signed [15:0] rx_i_r, rx_q_r, ref_i_r, ref_q_r;
    logic               v0, s0;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            v0 <= 1'b0;
            s0 <= 1'b0;
        end else begin
            rx_i_r  <= rx_i;
            rx_q_r  <= rx_q;
            ref_i_r <= ref_i;
            ref_q_r <= ref_q;
            v0      <= in_valid;
            s0      <= in_start;
        end
    end

    //-------------------------------------------------------------------------
    // stage 1: four multiplies (4 DSP48s)
    //-------------------------------------------------------------------------
    logic signed [31:0] p_ca, p_db, p_da, p_cb;
    logic               v1, s1;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            v1 <= 1'b0;
            s1 <= 1'b0;
        end else begin
            p_ca <= ref_i_r * rx_i_r;
            p_db <= ref_q_r * rx_q_r;
            p_da <= ref_q_r * rx_i_r;
            p_cb <= ref_i_r * rx_q_r;
            v1   <= v0;
            s1   <= s0;
        end
    end

    //-------------------------------------------------------------------------
    // stage 2: add / subtract (33 bits to avoid overflow)
    //-------------------------------------------------------------------------
    logic signed [32:0] sum_i, sum_q;
    logic               v2, s2;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            v2 <= 1'b0;
            s2 <= 1'b0;
        end else begin
            sum_i <= p_ca + p_db;
            sum_q <= p_da - p_cb;
            v2    <= v1;
            s2    <= s1;
        end
    end

    //-------------------------------------------------------------------------
    // stage 3: shift + saturate
    //-------------------------------------------------------------------------
    logic signed [32:0] sh_i, sh_q;

    always_comb begin
        sh_i = sum_i >>> SHIFT;
        sh_q = sum_q >>> SHIFT;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            out_valid <= 1'b0;
            out_start <= 1'b0;
            out_i     <= '0;
            out_q     <= '0;
        end else begin
            if      (sh_i >  32767) out_i <=  16'sd32767;
            else if (sh_i < -32768) out_i <= -16'sd32768;
            else                    out_i <=  sh_i[15:0];

            if      (sh_q >  32767) out_q <=  16'sd32767;
            else if (sh_q < -32768) out_q <= -16'sd32768;
            else                    out_q <=  sh_q[15:0];

            out_valid <= v2;
            out_start <= s2;
        end
    end

endmodule


//=============================================================================
//  3. cic_1ch  --  single-channel CIC decimation filter (R=128, N=4, M=1)
//
//  Why not just keep 1 sample out of every 128: anything above 240 kHz
//  would alias back into the signal band. The signal must be low-pass
//  filtered before decimating. CIC does this with the fewest resources
//  possible -- only adders and subtractors, no multipliers.
//
//  Structure: N integrator stages (full rate) -> decimate -> N comb stages
//  (low rate)
//
//  Bit growth = N * log2(R*M) = 4 * 7 = 28 bits
//  ACC_W  = 16 + 28 = 44
//  OUT_SHIFT = 28 gives unity passband gain
//
//  The integrators are deliberately allowed to overflow and wrap -- as long
//  as ACC_W is wide enough, the comb stage reconstructs the correct result
//  exactly. This is a well-known CIC property, not a bug.
//
//  Alias rejection is about 53 dB at N=4. If that's not enough, N=5
//  (ACC_W->51, OUT_SHIFT->35).
//
//  Passband droop (~-2 dB at 90 kHz) is not compensated for here -- the
//  droop is deterministic, so the PC side can just divide it out as a
//  per-range-bin gain factor, saving an entire compensation FIR.
//=============================================================================
module cic_1ch #(
    parameter int N         = 4,
    parameter int ACC_W     = 44,
    parameter int OUT_SHIFT = 28
)(
    input  logic               clk,
    input  logic               rst_n,
    input  logic               in_valid,
    input  logic signed [15:0] in_data,
    input  logic               decim_stb,
    output logic signed [15:0] out_data
);

    logic signed [ACC_W-1:0] in_ext;
    assign in_ext = {{(ACC_W-16){in_data[15]}}, in_data};

    //-------------------------------------------------------------------------
    // integrator stage (full rate, 61.44 MHz)
    //-------------------------------------------------------------------------
    logic signed [ACC_W-1:0] integ [0:N-1];

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int k = 0; k < N; k++) integ[k] <= '0;
        end else if (in_valid) begin
            integ[0] <= integ[0] + in_ext;
            for (int k = 1; k < N; k++) integ[k] <= integ[k] + integ[k-1];
        end
    end

    //-------------------------------------------------------------------------
    // comb stage (480 kHz, very cheap)
    //-------------------------------------------------------------------------
    logic signed [ACC_W-1:0] comb   [0:N-1];
    logic signed [ACC_W-1:0] comb_d [0:N-1];

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int k = 0; k < N; k++) begin
                comb[k]   <= '0;
                comb_d[k] <= '0;
            end
        end else if (decim_stb) begin
            comb_d[0] <= integ[N-1];
            comb[0]   <= integ[N-1] - comb_d[0];
            for (int k = 1; k < N; k++) begin
                comb_d[k] <= comb[k-1];
                comb[k]   <= comb[k-1] - comb_d[k];
            end
        end
    end

    //-------------------------------------------------------------------------
    // shift + saturate (registered output)
    //
    // A combinational output would put the saturation logic and the long
    // route to cpack in the same cycle, which doesn't close timing at
    // 245.76 MHz. An extra register stage separates the two.
    //-------------------------------------------------------------------------
    logic signed [ACC_W-1:0] sh;
    always_comb sh = comb[N-1] >>> OUT_SHIFT;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            out_data <= '0;
        end else begin
            if      (sh >  32767) out_data <=  16'sd32767;
            else if (sh < -32768) out_data <= -16'sd32768;
            else                  out_data <=  sh[15:0];
        end
    end

endmodule


//=============================================================================
//  4. cic_decim  --  complex wrapper + decimation phase control
//
//  in_start is used to align the decimation phase so every chirp's sample
//  boundary lands at the same position. 32768 / 128 = 256 exactly, so every
//  chirp produces exactly 256 samples.
//=============================================================================
module cic_decim #(
    parameter int R = 128
)(
    input  logic               clk,
    input  logic               rst_n,

    input  logic               in_valid,
    input  logic signed [15:0] in_i,
    input  logic signed [15:0] in_q,
    input  logic               in_start,

    output logic               out_valid,
    output logic signed [15:0] out_i,
    output logic signed [15:0] out_q,
    output logic               out_start
);

    localparam int CW = $clog2(R);

    logic [CW-1:0] cnt;
    logic          decim_stb;
    logic          first_flag;   // this chirp hasn't produced an output sample yet
    logic          first_out;
    logic decim_stb_d, first_out_d;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            decim_stb_d <= 1'b0;
            first_out_d <= 1'b0;
        end else begin
            decim_stb_d <= decim_stb;
            first_out_d <= first_out;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            cnt        <= '0;
            decim_stb  <= 1'b0;
            first_flag <= 1'b0;
            first_out  <= 1'b0;
        end else begin
            decim_stb <= 1'b0;
            first_out <= 1'b0;
            if (in_valid) begin
                if (in_start) begin
                    cnt        <= 1;        // this cycle is chirp input sample 0
                    first_flag <= 1'b1;
                end else if (cnt == R-1) begin
                    cnt        <= '0;
                    decim_stb  <= 1'b1;
                    first_out  <= first_flag;
                    first_flag <= 1'b0;
                end else begin
                    cnt <= cnt + 1'b1;
                end
            end
        end
    end

    cic_1ch u_i (.clk(clk), .rst_n(rst_n), .in_valid(in_valid),
                 .in_data(in_i), .decim_stb(decim_stb), .out_data(out_i));

    cic_1ch u_q (.clk(clk), .rst_n(rst_n), .in_valid(in_valid),
                 .in_data(in_q), .decim_stb(decim_stb), .out_data(out_q));

    // the comb stage updates on the decim_stb cycle; the output is only
    // stable one cycle later
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            out_valid <= 1'b0;
            out_start <= 1'b0;
        end else begin
            out_valid <= decim_stb_d;
            out_start <= first_out_d;
        end
    end

endmodule


//=============================================================================
//  5. radar_dsp  --  top level; this is the module that plugs into the
//     Pluto's block design
//
//  bypass = 1 passes the chain straight through, outputting the raw
//  61.44 MSPS data with TX output held at zero. A single signal restores
//  "factory behaviour", so if integration misbehaves you can immediately
//  tell whether this module is broken or the wiring is wrong.
//
//  The chirp-boundary marker is packed into the LSB of out_data_q:
//    Q precision drops from 16 bits to 15, a 6 dB loss.
//    But 1 LSB corresponds to about -90 dB, while the measured noise floor
//    is around -50 dB -- completely buried in the noise, effectively free.
//    PC side: starts = np.flatnonzero(q.astype(np.int16) & 1)
//=============================================================================
module radar_dsp #(
    parameter int CHIRP_LEN = 32768,
    parameter     LUT_FILE  = "sin_lut.mem"
)(
    input  logic        clk,          // axi_ad9361/l_clk
    input  logic        rst,          // axi_ad9361/rst  (active high)
    input  logic        bypass,

    // from axi_ad9361 RX
    input  logic        adc_valid,
    input  logic [15:0] adc_data_i,
    input  logic [15:0] adc_data_q,

    // to cpack
    output logic        out_valid,
    output logic [15:0] out_data_i,
    output logic [15:0] out_data_q,

    // to axi_ad9361 TX
    output logic [15:0] dac_data_i,
    output logic [15:0] dac_data_q
);

    logic rst_n;
    assign rst_n = ~rst;

    //-------------------------------------------------------------------------
    // chirp generator
    //
    // Gated by adc_valid rather than dac_valid: dechirp requires the
    // reference and the RX sample to land on exactly the same cycle, and
    // only a shared valid signal guarantees that. TX and RX share the same
    // sample clock, so TX never underruns.
    //-------------------------------------------------------------------------
    logic signed [15:0] nco_c, nco_s;
    logic               nco_start;

    chirp_nco #(
        .CHIRP_LEN (CHIRP_LEN),
        .LUT_FILE  (LUT_FILE)
    ) u_nco (
        .clk      (clk),
        .rst_n    (rst_n),
        .en       (adc_valid),
        .cos_out  (nco_c),
        .sin_out  (nco_s),
        .start_out(nco_start)
    );

    assign dac_data_i = bypass ? 16'd0 : nco_c;
    assign dac_data_q = bypass ? 16'd0 : nco_s;

    //-------------------------------------------------------------------------
    // dechirp
    //-------------------------------------------------------------------------
    logic signed [15:0] dc_i, dc_q;
    logic               dc_valid, dc_start;

    dechirp u_dc (
        .clk      (clk),
        .rst_n    (rst_n),
        .in_valid (adc_valid),
        .rx_i     (adc_data_i),
        .rx_q     (adc_data_q),
        .ref_i    (nco_c),
        .ref_q    (nco_s),
        .in_start (nco_start),
        .out_valid(dc_valid),
        .out_i    (dc_i),
        .out_q    (dc_q),
        .out_start(dc_start)
    );

    //-------------------------------------------------------------------------
    // decimation
    //-------------------------------------------------------------------------
    logic signed [15:0] ci, cq;
    logic               c_valid, c_start;

    cic_decim u_cic (
        .clk      (clk),
        .rst_n    (rst_n),
        .in_valid (dc_valid),
        .in_i     (dc_i),
        .in_q     (dc_q),
        .in_start (dc_start),
        .out_valid(c_valid),
        .out_i    (ci),
        .out_q    (cq),
        .out_start(c_start)
    );

    //-------------------------------------------------------------------------
    // output + chirp-boundary marker
    //-------------------------------------------------------------------------
    always_comb begin
        if (bypass) begin
            out_valid  = adc_valid;
            out_data_i = adc_data_i;
            out_data_q = adc_data_q;
        end else begin
            out_valid  = c_valid;
            out_data_i = ci;
            out_data_q = {cq[15:1], c_start};   // LSB = chirp boundary marker
        end
    end

endmodule
