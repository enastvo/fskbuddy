//
// pocsag_bitsync: recovers per-bit timing from an oversampled 1-bit FSK
// discriminator stream (see fsk_demod.v) so downstream logic sees exactly
// one decision per POCSAG bit period, instead of one per RX sample.
//
// Technique: a free-running sample counter divides by `sps` (samples per
// bit, set by the host from the chosen POCSAG rate and the configured RX
// sample rate) and emits a majority-vote (integrate-and-dump) bit decision
// every time it wraps. Between wraps, WHILE NOT YET LOCKED (framer_locked
// low -- still searching for the frame sync word), any transition of the
// raw discriminator bit that lands away from either edge of the window
// (i.e. clearly mid-window, not just jitter right at a boundary) is
// treated as evidence we've drifted out of phase, and forces an early
// wrap right on that transition; this converges to a solid lock during
// POCSAG's long alternating-bit preamble, fast.
//
// Once framer_locked goes high, this edge-triggered resync is disabled
// and the divider just free-runs. That's deliberate, not a simplification
// left on the table: TX and RX here share the same on-board reference
// clock (same physical radio, no independent oscillators), so once
// correctly phase-aligned there is no clock drift to track -- continuing
// to hard-resync on every mid-window transition instead means any single
// noisy discriminator sample (routine on a real RF path, never seen over
// the internal digital loopback this was first validated against) can
// throw away a good lock and restart the integration window from
// scratch. Free-running after lock is both simpler and more robust here.
module pocsag_bitsync
  (
   input        clk,
   input        reset,
   input        en,             // must be held while sps is valid; clears state when low
   input [15:0] sps,            // samples per bit (RX rate / POCSAG bit rate), host-set
   input        raw_bit,        // fsk_demod.bit_out
   input        raw_bit_valid,  // fsk_demod.bit_valid
   input        framer_locked,  // pocsag_framer.locked -- gates edge-triggered resync, see above
   output reg        sym_bit,   // recovered bit decision
   output reg        sym_valid  // pulses one clk when sym_bit is valid
   );

   // Quarter-window guard band: a mid-window transition outside
   // [sps/4, sps - sps/4) is considered "on time" jitter, not drift.
   wire [15:0] guard = {2'b0, sps[15:2]}; // sps >> 2

   reg [15:0]  cnt;
   reg [15:0]  ones_accum;
   reg         prev_bit;
   reg         first;

   wire        window_done = (cnt >= sps);
   wire        mid_window  = (cnt > guard) && (cnt < (sps - guard));
   wire        is_edge     = raw_bit != prev_bit;

   always @(posedge clk) begin
      if (reset || !en || (sps < 16'd2)) begin
         cnt        <= 16'd0;
         ones_accum <= 16'd0;
         prev_bit   <= 1'b0;
         first      <= 1'b1;
         sym_bit    <= 1'b0;
         sym_valid  <= 1'b0;
      end else begin
         sym_valid <= 1'b0;

         if (raw_bit_valid) begin
            if (first) begin
               first      <= 1'b0;
               prev_bit   <= raw_bit;
               cnt        <= 16'd1;
               ones_accum <= {15'd0, raw_bit};
            end else if (is_edge && mid_window && !framer_locked) begin
               // Drifted -- resync hard on this transition.
               sym_bit    <= (({ones_accum, 1'b0}) >= {1'b0, cnt}); // ones_accum*2 >= cnt
               sym_valid  <= 1'b1;
               prev_bit   <= raw_bit;
               cnt        <= 16'd1;
               ones_accum <= {15'd0, raw_bit};
            end else if (window_done) begin
               sym_bit    <= (({ones_accum, 1'b0}) >= {1'b0, sps});  // ones_accum*2 >= sps
               sym_valid  <= 1'b1;
               prev_bit   <= raw_bit;
               cnt        <= 16'd1;
               ones_accum <= {15'd0, raw_bit};
            end else begin
               prev_bit   <= raw_bit;
               cnt        <= cnt + 16'd1;
               ones_accum <= ones_accum + {15'd0, raw_bit};
            end
         end
      end
   end
endmodule
