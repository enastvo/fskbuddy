//
// channel_width_detect: classifies the RX channel as narrowband
// (12.5kHz-style, smaller FSK deviation) or wideband (25kHz-style, larger
// deviation) directly in fabric, by comparing the statistical swing of
// fsk_demod.v's own discriminator output against the signal's own power --
// not by running a second bandpass filter pair to measure occupied
// bandwidth directly (the more literal approach), which would need ~32
// more DSP48A1 multiplies this design doesn't have spare (112 of 132
// already used by pocsag_channel_filter.v + fsk_demod.v + clip_detect.v
// combined). This module adds exactly 2 more (for its own power
// reference).
//
// Math: for small FSK modulation angles (true here -- POCSAG/GSC-scale
// deviations of a few kHz at a ~1MHz sample rate give phase steps on the
// order of hundredths of a radian per sample), Im{s[n]*conj(s[n-1])} ~=
// |s[n]|^2 * sin(dphi) ~= |s[n]|^2 * dphi -- i.e. the raw discriminator
// value scales with BOTH deviation and instantaneous signal power, so
// comparing its raw magnitude against a fixed threshold would be
// confounded by RX gain/link strength (a stronger signal at the SAME
// deviation would read "wider"). Dividing it out sample-by-sample needs a
// real divider; instead this accumulates |disc| and power (I^2+Q^2)
// SEPARATELY over a window and compares abs_disc_sum against SHIFTED (not
// divided) versions of power_sum -- proportional comparison without a
// divider, same spirit as pocsag_channel_filter.v's own shift-based
// unity-gain scaling.
//
// Debounced over DEBOUNCE_N consecutive windows before flipping the
// latched decision -- same "commit only after sustained agreement, don't
// chase a single noisy reading" lesson pocsag_bitsync.v's own
// locked-vs-searching resync gating already established for this project.
//
// THRESHOLD_SHIFT_WIDE/NARROW are EMPIRICALLY CALIBRATED, not guessed:
// measured on real hardware, transmitting known deviations through this
// project's own TX chain at a genuinely locked/decoding link (not just a
// strong-signal/noise-floor guess -- confirmed via on_status's `locked`
// and real decoded pages). At 2000Hz (narrowband-style) deviation,
// abs_disc_sum/power_sum measured ~0.0123-0.0124; at 4500Hz (POCSAG
// standard/wideband-style), ~0.0278-0.0279 -- a ratio of ~2.26x, matching
// the deviation ratio (4500/2000 = 2.25) almost exactly, and both close to
// the small-angle theoretical prediction (2*pi*f_dev/1e6: 0.0126 and
// 0.0283 respectively). Also confirmed gain-independent: re-measured at a
// completely different gain (TX=40/RX=50 vs TX=50/RX=55) and got the same
// 0.0278 ratio at 4500Hz, as the normalize-by-power design intends. A
// single shared shift of 6 (threshold 0.015625, sitting between the two
// measured ratios with reasonable margin on each side -- ~1.3x above the
// narrow measurement, ~1.8x below the wide one) cleanly separates them;
// the original guessed values (10/12, i.e. thresholds ~0.001/0.00024) were
// off by 4-6 octaves and would never have worked. See the project's plan
// notes for the full calibration writeup (including two dead-end attempts:
// too-low gain measured pure noise floor indistinguishable across
// deviations, and the temporary debug output's first width had to be
// widened from a bare 16-bit sum reading, which saturated even at a
// genuinely-locked, non-overdriven signal level).
//
module channel_width_detect
  (
   input        clk,
   input        reset,

   // Filtered baseband -- the same signal fsk_demod.v itself consumes
   // (pocsag_filt_strobe/pocsag_filt_sample in radio_legacy.v).
   input        strobe,
   input [31:0] sample,          // {I[15:0], Q[15:0]}

   // fsk_demod.v's raw discriminator output (its disc_out/disc_valid).
   input signed [32:0] disc,
   input                disc_valid,

   output reg width_narrow,      // 1 = narrowband (12.5kHz-style) classification
   output reg width_locked       // debounce has settled -- see header
   );

   localparam WINDOW_LEN = 16'd4096;  // ~4.096ms at a 1MHz strobe rate
   localparam DEBOUNCE_N = 4'd8;      // consecutive agreeing windows (~33ms) to (re)latch

   // See header for the calibration measurement behind this value.
   localparam THRESHOLD_SHIFT_WIDE   = 4'd6;
   localparam THRESHOLD_SHIFT_NARROW = 4'd6;

   wire signed [15:0] i_in = sample[31:16];
   wire signed [15:0] q_in = sample[15:0];

   // Power reference: same 3-stage multiply->sum pipeline style as
   // clip_detect.v (its own header explains the timing/XST-fusion
   // reasoning), computed independently here since it needs to be on the
   // SAME (filtered) signal fsk_demod/disc is derived from, not
   // clip_detect's pre-filter tap.
   reg [31:0] i_sq, q_sq;
   reg [31:0] power_sample;
   reg        strobe_d1;

   always @(posedge clk) begin
      if (reset) begin
         i_sq <= 32'd0;
         q_sq <= 32'd0;
         power_sample <= 32'd0;
         strobe_d1 <= 1'b0;
      end else begin
         strobe_d1 <= strobe;
         if (strobe) begin
            i_sq <= i_in * i_in;
            q_sq <= q_in * q_in;
         end
         if (strobe_d1)
            power_sample <= i_sq + q_sq;
      end
   end

   // |disc|, cheap (conditional two's-complement negate, no multiply).
   wire [32:0] disc_abs = disc[32] ? (~disc + 33'd1) : disc;

   // Per-window accumulators. Window length (4096) x worst-case per-sample
   // magnitude comfortably fits 48 bits with wide margin -- LUT-cheap,
   // this part of the chip (47% Slice LUTs used) isn't under the same
   // pressure DSP48A1s are.
   reg [15:0] win_cnt;
   reg [47:0] power_sum, abs_disc_sum;

   // Debounce: agree_cnt counts consecutive windows agreeing with
   // pending_narrow; hitting DEBOUNCE_N (re)latches width_narrow/locked.
   // A disagreeing window restarts the count in the new direction and
   // drops width_locked until it reconverges. An ambiguous window (neither
   // threshold crossed) leaves agree_cnt untouched -- doesn't advance
   // toward relatching, but doesn't restart it either.
   reg [3:0]  agree_cnt;
   reg        pending_narrow;

   always @(posedge clk) begin
      if (reset) begin
         win_cnt      <= 16'd0;
         power_sum    <= 48'd0;
         abs_disc_sum <= 48'd0;
         agree_cnt    <= 4'd0;
         pending_narrow <= 1'b0;
         width_narrow <= 1'b0;   // default wide -- matches today's single (wideband) filter
         width_locked <= 1'b0;
      end else begin
         if (strobe_d1)
            power_sum <= power_sum + {16'd0, power_sample};
         if (disc_valid)
            abs_disc_sum <= abs_disc_sum + {15'd0, disc_abs};

         // Window tick -- counts strobes (disc_valid runs at essentially
         // the same rate, just fsk_demod's fixed few-cycle pipeline lag
         // behind; that skew is irrelevant averaged over 4096 samples).
         if (strobe) begin
            if (win_cnt == WINDOW_LEN - 16'd1) begin
               win_cnt <= 16'd0;

               if (abs_disc_sum > (power_sum >> THRESHOLD_SHIFT_WIDE)) begin
                  // Clearly wide this window.
                  if (pending_narrow == 1'b0) begin
                     if (agree_cnt < DEBOUNCE_N) agree_cnt <= agree_cnt + 4'd1;
                  end else begin
                     pending_narrow <= 1'b0;
                     agree_cnt      <= 4'd1;
                     width_locked   <= 1'b0;
                  end
               end else if (abs_disc_sum < (power_sum >> THRESHOLD_SHIFT_NARROW)) begin
                  // Clearly narrow this window.
                  if (pending_narrow == 1'b1) begin
                     if (agree_cnt < DEBOUNCE_N) agree_cnt <= agree_cnt + 4'd1;
                  end else begin
                     pending_narrow <= 1'b1;
                     agree_cnt      <= 4'd1;
                     width_locked   <= 1'b0;
                  end
               end
               // else: ambiguous window -- agree_cnt untouched.

               if (agree_cnt >= DEBOUNCE_N) begin
                  width_narrow <= pending_narrow;
                  width_locked <= 1'b1;
               end

               power_sum    <= 48'd0;
               abs_disc_sum <= 48'd0;
            end else begin
               win_cnt <= win_cnt + 16'd1;
            end
         end
      end
   end
endmodule
