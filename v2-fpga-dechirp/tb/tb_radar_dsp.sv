`timescale 1ns / 1ps
//=============================================================================
//  tb_radar_dsp  --  full-chain behavioral simulation
//
//  Uses a delay line to simulate a single stationary point target:
//    DELAY = 32 cycles -> range = 32 * c/(2*61.44e6) = 78.125 m
//    beat frequency fb = S*tau = 9.0e10 * 32/61.44e6 = 46875 Hz
//    output rate 480 kSPS, 256-point FFT, bin spacing 1875 Hz
//    => the peak should land at range bin 25
//
//  Changing DELAY verifies linearity: DELAY=64 -> bin 50 (156.25 m)
//=============================================================================
module tb_radar_dsp;

    localparam int CHIRP_LEN = 32768;
    localparam int NCHIRP    = 4;
    localparam int DELAY     = 32;   // target range 78.125 m
    localparam int RX_SHIFT  = 4;    // echo attenuation, models realistic 12-bit ADC amplitude

    logic clk = 0, rst = 1;
    always #8.138 clk = ~clk;        // 61.44 MHz

    //-------------------------------------------------------------------------
    // Sample-rate gate. In the real system adc_valid comes from axi_ad9361
    // and is not asserted every cycle in 2R2T mode; here it's simplified to
    // a constant 1.
    //-------------------------------------------------------------------------
    logic adc_valid = 1'b1;

    //-------------------------------------------------------------------------
    // DUT
    //-------------------------------------------------------------------------
    logic [15:0] adc_i, adc_q;
    logic [15:0] out_i, out_q, dac_i, dac_q;
    logic        out_valid;

    radar_dsp #(
        .CHIRP_LEN (CHIRP_LEN),
        .LUT_FILE  ("sin_lut.mem")
    ) dut (
        .clk        (clk),
        .rst        (rst),
        .bypass     (1'b0),
        .adc_valid  (adc_valid),
        .adc_data_i (adc_i),
        .adc_data_q (adc_q),
        .out_valid  (out_valid),
        .out_data_i (out_i),
        .out_data_q (out_q),
        .dac_data_i (dac_i),
        .dac_data_q (dac_q)
    );

    //-------------------------------------------------------------------------
    // Simulated target: delay the TX output by DELAY cycles, attenuate, and
    // feed it back into RX.
    //
    // The delay line must be reset to zero, otherwise X propagates into the
    // CIC integrators and latches up permanently (X + anything = X).
    //-------------------------------------------------------------------------
    logic signed [15:0] dly_i [0:DELAY-1];
    logic signed [15:0] dly_q [0:DELAY-1];

    always_ff @(posedge clk) begin
        if (rst) begin
            for (int k = 0; k < DELAY; k++) begin
                dly_i[k] <= '0;
                dly_q[k] <= '0;
            end
        end else if (adc_valid) begin
            dly_i[0] <= dac_i;
            dly_q[0] <= dac_q;
            for (int k = 1; k < DELAY; k++) begin
                dly_i[k] <= dly_i[k-1];
                dly_q[k] <= dly_q[k-1];
            end
        end
    end

    assign adc_i = dly_i[DELAY-1] >>> RX_SHIFT;
    assign adc_q = dly_q[DELAY-1] >>> RX_SHIFT;

    //-------------------------------------------------------------------------
    // Assertion: at each chirp start the phase should reset to zero, i.e.
    // cos=full scale, sin=0. This is the most direct way to verify the LAT
    // delay is balanced correctly.
    //-------------------------------------------------------------------------
    int start_cnt = 0;

    always_ff @(posedge clk) begin
        if (!rst && dut.u_nco.start_out) begin
            start_cnt++;
            if (dut.u_nco.sin_out !== 16'sd0)
                $display("[warn] t=%0t chirp start sin=%0d (expected 0), LAT delay may be unbalanced",
                         $time, $signed(dut.u_nco.sin_out));
        end
    end

    //-------------------------------------------------------------------------
    // Capture
    //-------------------------------------------------------------------------
    int fd, n = 0;

    initial begin
        fd = $fopen("radar_out.txt", "w");
        repeat (10) @(posedge clk);
        rst = 0;

        while (n < NCHIRP*256 + 300) begin
            @(posedge clk);
            if (out_valid) begin
                // third column: chirp boundary marker extracted from the Q LSB
                $fwrite(fd, "%0d %0d %0d\n",
                        $signed(out_i), $signed(out_q), out_q[0]);
                n++;
            end
        end

        $fclose(fd);
        $display("wrote %0d samples, %0d chirp starts", n, start_cnt);
        $finish;
    end

endmodule
