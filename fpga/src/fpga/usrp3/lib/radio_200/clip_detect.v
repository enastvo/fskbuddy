//
// clip_detect: flags RX front-end saturation (ADC/DDC magnitude pegged near
// full scale -- RX gain set too hot for the actual link) directly in
// fabric, replacing a per-buffer software check
// (`np.max(np.abs(buf[:n])) > CLIP_THRESHOLD` in transceiver.py) that
// required the host to have real IQ samples in hand and scan every one of
// them, every RX loop iteration. Taps sample_rx/strobe_rx directly -- PRE
// pocsag_channel_filter, the same point that filter itself taps -- because
// this needs to catch actual front-end overload, a property of the raw
// signal, not the narrowed/filtered one downstream.
//
// Free-running clip_count[7:0] (wraps), exactly the same idiom
// pocsag_framer.v already uses for codeword_count: the host reads it and
// diffs against the last-seen value ((count - last) & 8'hFF), treating any
// nonzero diff as "clipped since I last looked." No sticky flag, no
// host-side clear needed -- one less thing to get wrong across a
// disconnect/reconnect.
//
// Comparison is on I^2+Q^2 against a magnitude threshold squared (avoids a
// sqrt -- exactly equivalent to comparing sqrt(I^2+Q^2) against a magnitude
// threshold, just cheaper: two signed 16x16 multiplies, same order as
// fsk_demod.v's own 2, well inside the spare DSP48A1 budget noted in
// pocsag_channel_filter.v's header).
//
// THRESHOLD_MAG below (95% of int16 full scale, 32768) is EMPIRICALLY
// CONFIRMED, not just assumed: calibrated against real hardware with a
// temporary peak-hold diagnostic register (compared clip_detect's own raw
// I^2+Q^2 against the host's np.max(np.abs(buf)) during deliberate extreme
// overdrive -- both TX and RX gain maxed). Implied raw full-scale value was
// 32767 (vs the 32768 assumed here, 1 LSB of rounding noise) and the
// derived threshold was 31129 (vs 31130 configured) -- close enough to
// call confirmed. Both sides also independently pegged at the same
// saturation point during the test (I=Q=-32768, the ADC/DDC chain's own
// clip_reg saturation logic, per ddc_chain.v), which is exactly the
// front-end-overload condition this module exists to catch.
//
module clip_detect
  (
   input        clk,
   input        reset,
   input        strobe_rx,
   input [31:0] sample_rx,       // {I[15:0], Q[15:0]}, same tap as pocsag_channel_filter
   output reg [7:0] clip_count   // free-running, wraps -- see header
   );

   localparam [15:0] THRESHOLD_MAG = 16'd31130;
   localparam [31:0] THRESHOLD_SQ  = THRESHOLD_MAG * THRESHOLD_MAG;

   wire signed [15:0] i_in = sample_rx[31:16];
   wire signed [15:0] q_in = sample_rx[15:0];

   // Three explicit registered stages (multiply -> sum -> compare/count),
   // not one big combinational block between two register stages -- the
   // first cut of this had the sum and the compare both combinational in
   // the same cycle, and XST's MAC-fusion optimizer folded the multiply in
   // with it too, which together blew the clkout2 (~200MHz) timing
   // constraint on real synthesis. Same root cause and same fix pattern
   // pocsag_channel_filter.v's header documents for exactly this clock
   // domain -- more (registered) stages, each one shallow, rather than one
   // deep one. Ample slack for the extra cycle either way: strobe_rx runs
   // ~32x slower than radio_clk.
   reg [31:0] i_sq, q_sq;
   reg [31:0] mag_sq;
   reg        strobe_d1, strobe_d2;

   always @(posedge clk) begin
      if (reset) begin
         i_sq       <= 32'd0;
         q_sq       <= 32'd0;
         mag_sq     <= 32'd0;
         strobe_d1  <= 1'b0;
         strobe_d2  <= 1'b0;
         clip_count <= 8'd0;
      end else begin
         strobe_d1 <= strobe_rx;
         strobe_d2 <= strobe_d1;

         if (strobe_rx) begin
            i_sq <= i_in * i_in;   // always non-negative -- fits unsigned
            q_sq <= q_in * q_in;
         end

         if (strobe_d1)
            mag_sq <= i_sq + q_sq;   // registered sum, its own stage

         if (strobe_d2 && (mag_sq > THRESHOLD_SQ))
            clip_count <= clip_count + 8'd1;
      end
   end
endmodule
