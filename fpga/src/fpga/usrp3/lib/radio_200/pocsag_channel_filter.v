//
// pocsag_channel_filter: narrowband lowpass ahead of fsk_demod's
// discriminator, to fix the FM/FSK "threshold effect" measured on real RF
// (see the design notes this came out of): a delay-and-multiply
// discriminator's output SNR scales with the *square* of the deviation for
// a *fixed* input noise bandwidth, so running it straight off ddc_chain's
// full-bandwidth output (effectively hundreds of kHz of noise, vs.
// POCSAG's actual ~15-20kHz channel) meant standard +/-4500Hz deviation
// collapsed completely while a much larger deviation worked -- confirmed
// empirically with a deviation sweep before this filter existed. This
// narrows the noise bandwidth instead of inflating the deviation, which is
// what real POCSAG hardware/software decoders do too: SDRangel's pager
// demod plugin (a mature, widely-used open-source implementation) defaults
// to a 20kHz RF bandwidth filter ahead of its discriminator, at the same
// 4500Hz spec deviation -- this filter's cutoff matches that reference
// point.
//
// 31-tap symmetric (linear-phase) FIR lowpass, Hamming-windowed sinc,
// ~20kHz cutoff at a 1MHz sample rate (see the design notes for the Python
// that generated the coefficients). Tap symmetry halves the multiplier
// count: mirrored delay-line samples are added *before* multiplying by
// the shared coefficient, so this costs 16 real multiplies per channel
// (32 total for I+Q) instead of 31, well inside the spare DSP48A1 budget
// (54 of 132 free after fsk_demod's own 2). Runs at the input sample rate
// (whatever strobe_rx's rate is) -- no decimation, so it's a drop-in tap
// that doesn't disturb the existing sample_rx/strobe_rx path anything
// else consumes.
//
// Pipelined across 4 clock cycles (shift -> pair-sum -> multiply ->
// sum+scale), instead of one big combinational block: strobe_rx is ~32x
// slower than radio_clk (1MHz vs 32MHz), so there's ample slack for a few
// cycles of pipeline latency, and the very first cut of this (everything
// combinational between two register stages) blew the clkout1/clkout2
// timing constraints that had comfortable margin before this filter
// existed -- 16 DSP48A1 multiplies feeding straight into a 15-way adder
// tree in one cycle was simply too much combinational depth. Splitting
// pair-sum, multiply, and sum+scale into their own registered stages
// fixed it (each stage is now shallow enough on its own).
//
// Coefficients sum to exactly 2^15, so the final stage is just the 40-bit
// MAC sum arithmetic-shifted right by 15 (unity DC gain) and truncated
// back to 16 bits -- no explicit saturation logic; a proper unity-gain
// lowpass shouldn't meaningfully overshoot a well-behaved input's range,
// and Hamming windowing keeps FIR ripple/overshoot low.
//
module pocsag_channel_filter
  (
   input        clk,
   input        reset,
   input        strobe_in,
   input [31:0] sample_in,    // {i[15:0], q[15:0]}, same format as ddc_chain's sample_rx
   output reg        strobe_out,
   output reg [31:0] sample_out
   );

   localparam signed [15:0] C0  = 16'sd89;
   localparam signed [15:0] C1  = 16'sd111;
   localparam signed [15:0] C2  = 16'sd162;
   localparam signed [15:0] C3  = 16'sd246;
   localparam signed [15:0] C4  = 16'sd365;
   localparam signed [15:0] C5  = 16'sd519;
   localparam signed [15:0] C6  = 16'sd705;
   localparam signed [15:0] C7  = 16'sd915;
   localparam signed [15:0] C8  = 16'sd1140;
   localparam signed [15:0] C9  = 16'sd1371;
   localparam signed [15:0] C10 = 16'sd1595;
   localparam signed [15:0] C11 = 16'sd1799;
   localparam signed [15:0] C12 = 16'sd1972;
   localparam signed [15:0] C13 = 16'sd2103;
   localparam signed [15:0] C14 = 16'sd2186;
   localparam signed [15:0] C15 = 16'sd2212; // center tap

   // ---- Stage 0: delay line (31 taps deep, shifts on strobe_in) ----
   reg signed [15:0] i_dly [0:30];
   reg signed [15:0] q_dly [0:30];
   integer k;

   always @(posedge clk) begin
      if (reset) begin
         for (k = 0; k < 31; k = k + 1) begin
            i_dly[k] <= 16'sd0;
            q_dly[k] <= 16'sd0;
         end
      end else if (strobe_in) begin
         i_dly[0] <= sample_in[31:16];
         q_dly[0] <= sample_in[15:0];
         for (k = 30; k > 0; k = k - 1) begin
            i_dly[k] <= i_dly[k-1];
            q_dly[k] <= q_dly[k-1];
         end
      end
   end

   // ---- Stage 1: mirrored pair sums, registered ----
   reg        strobe_p1;
   reg signed [16:0] i_pair_r [0:14];
   reg signed [16:0] q_pair_r [0:14];
   reg signed [15:0] i_ctr_r, q_ctr_r;
   integer m;

   always @(posedge clk) begin
      if (reset) begin
         strobe_p1 <= 1'b0;
         i_ctr_r   <= 16'sd0;
         q_ctr_r   <= 16'sd0;
         for (m = 0; m < 15; m = m + 1) begin
            i_pair_r[m] <= 17'sd0;
            q_pair_r[m] <= 17'sd0;
         end
      end else begin
         strobe_p1 <= strobe_in;
         if (strobe_in) begin
            for (m = 0; m < 15; m = m + 1) begin
               i_pair_r[m] <= i_dly[m] + i_dly[30-m];
               q_pair_r[m] <= q_dly[m] + q_dly[30-m];
            end
            i_ctr_r <= i_dly[15];
            q_ctr_r <= q_dly[15];
         end
      end
   end

   // ---- Stage 2: multiply by shared coefficients, registered ----
   reg        strobe_p2;
   reg signed [34:0] i_prod_r [0:14];
   reg signed [34:0] q_prod_r [0:14];
   reg signed [33:0] i_prod15_r, q_prod15_r;

   wire signed [15:0] coef [0:14];
   assign coef[0]=C0;  assign coef[1]=C1;  assign coef[2]=C2;   assign coef[3]=C3;
   assign coef[4]=C4;  assign coef[5]=C5;  assign coef[6]=C6;   assign coef[7]=C7;
   assign coef[8]=C8;  assign coef[9]=C9;  assign coef[10]=C10; assign coef[11]=C11;
   assign coef[12]=C12; assign coef[13]=C13; assign coef[14]=C14;

   always @(posedge clk) begin
      if (reset) begin
         strobe_p2  <= 1'b0;
         i_prod15_r <= 34'sd0;
         q_prod15_r <= 34'sd0;
         for (m = 0; m < 15; m = m + 1) begin
            i_prod_r[m] <= 35'sd0;
            q_prod_r[m] <= 35'sd0;
         end
      end else begin
         strobe_p2 <= strobe_p1;
         if (strobe_p1) begin
            for (m = 0; m < 15; m = m + 1) begin
               i_prod_r[m] <= i_pair_r[m] * coef[m];
               q_prod_r[m] <= q_pair_r[m] * coef[m];
            end
            i_prod15_r <= i_ctr_r * C15;
            q_prod15_r <= q_ctr_r * C15;
         end
      end
   end

   // ---- Stage 3: partial sums (two 8-way adds instead of one 16-way add),
   // registered. The single 16-way adder tree that used to live here (see
   // git history) left clkout2 (5ns period, 200MHz) 0.12ns over budget --
   // small, but still a real miss. Splitting it into two shallower halves,
   // each its own registered stage, closes that gap.
   wire signed [37:0] i_sum_lo = i_prod_r[0] + i_prod_r[1] + i_prod_r[2] + i_prod_r[3] +
                                 i_prod_r[4] + i_prod_r[5] + i_prod_r[6] + i_prod_r[7];
   wire signed [37:0] i_sum_hi = i_prod_r[8] + i_prod_r[9] + i_prod_r[10] + i_prod_r[11] +
                                 i_prod_r[12] + i_prod_r[13] + i_prod_r[14] + i_prod15_r;
   wire signed [37:0] q_sum_lo = q_prod_r[0] + q_prod_r[1] + q_prod_r[2] + q_prod_r[3] +
                                 q_prod_r[4] + q_prod_r[5] + q_prod_r[6] + q_prod_r[7];
   wire signed [37:0] q_sum_hi = q_prod_r[8] + q_prod_r[9] + q_prod_r[10] + q_prod_r[11] +
                                 q_prod_r[12] + q_prod_r[13] + q_prod_r[14] + q_prod15_r;

   reg        strobe_p3;
   reg signed [37:0] i_sum_lo_r, i_sum_hi_r, q_sum_lo_r, q_sum_hi_r;

   always @(posedge clk) begin
      if (reset) begin
         strobe_p3  <= 1'b0;
         i_sum_lo_r <= 38'sd0;
         i_sum_hi_r <= 38'sd0;
         q_sum_lo_r <= 38'sd0;
         q_sum_hi_r <= 38'sd0;
      end else begin
         strobe_p3 <= strobe_p2;
         if (strobe_p2) begin
            i_sum_lo_r <= i_sum_lo;
            i_sum_hi_r <= i_sum_hi;
            q_sum_lo_r <= q_sum_lo;
            q_sum_hi_r <= q_sum_hi;
         end
      end
   end

   // ---- Stage 4: final sum + scale, registered (output) ----
   wire signed [39:0] i_sum = i_sum_lo_r + i_sum_hi_r;
   wire signed [39:0] q_sum = q_sum_lo_r + q_sum_hi_r;

   // >>>15 for unity DC gain. Named wires (not inline expressions) because
   // Verilog-2001 part-selects only apply to a net/reg, not to an arbitrary
   // expression -- (i_sum >>> 15)[15:0] isn't legal syntax.
   wire signed [39:0] i_scaled = i_sum >>> 15;
   wire signed [39:0] q_scaled = q_sum >>> 15;

   always @(posedge clk) begin
      if (reset) begin
         strobe_out <= 1'b0;
         sample_out <= 32'd0;
      end else begin
         strobe_out <= strobe_p3;
         if (strobe_p3)
            sample_out <= {i_scaled[15:0], q_scaled[15:0]};
      end
   end
endmodule
