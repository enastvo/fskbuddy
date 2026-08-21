//
// fsk_demod.v
//
// Binary FSK/FM discriminator ("delay-and-multiply" quadrature detector)
// operating on the decimated complex baseband samples ddc_chain already
// produces, plus a raw bit-decision output meant to drive a GPIO pin
// straight from fabric -- no software in the per-symbol path.
//
// Math: for consecutive complex baseband samples s[n] = I[n] + jQ[n], the
// instantaneous frequency is arg(s[n] * conj(s[n-1])). A binary FSK slicer
// only needs the SIGN of that angle (mark = positive deviation, space =
// negative deviation), and sign(Im(s[n]*conj(s[n-1]))) gives exactly that
// for any angle in (-180, +180) without an atan2/CORDIC:
//
//   s[n]*conj(s[n-1]) = (I[n]I[n-1] + Q[n]Q[n-1]) + j(Q[n]I[n-1] - I[n]Q[n-1])
//
// so bit_out = [ Q[n]*I[n-1] - I[n]*Q[n-1] >= 0 ]. Two 16x16 signed
// multiplies (map onto the part's DSP48A1 slices) and a subtract.
//
// This is free-running off `strobe` -- no clock/symbol recovery here, just
// a raw per-sample bit decision. Fine for seeing mark/space transitions on
// a scope or a GPIO capture; a real bit synchronizer (matched filter +
// timing recovery) is a follow-on, not attempted here.
//

module fsk_demod #(
   parameter BASE = 0
)(
   input        clk,
   input        reset,

   // Settings bus
   input        set_stb,
   input [7:0]  set_addr,
   input [31:0] set_data,

   // Tap off the DDC chain: sample = {I[15:0], Q[15:0]}, valid when strobe.
   input        strobe,
   input [31:0] sample,

   // Raw bit decision, registered, updated ~4 clocks after each strobe.
   output reg   bit_out,
   output reg   bit_valid,   // one-cycle pulse aligned with bit_out update

   // The discriminator value itself (Im{s[n]*conj(s[n-1])}), before the
   // hard mark/space slice -- exposed for channel_width_detect.v, which
   // estimates FSK deviation from its statistical swing (narrower-channel
   // conventions use smaller deviation, wider use larger) rather than
   // running a second, DSP48A1-expensive bandpass filter pair to measure
   // occupied bandwidth directly. Same timing as bit_out/bit_valid --
   // plain wires, not new registers, since `disc` is already stable by
   // the strobe_d3 cycle bit_out reads it on.
   output [32:0] disc_out,
   output        disc_valid
);
   assign disc_out   = disc;
   assign disc_valid = bit_valid;

   // Defaults to enabled at reset -- see the comment on the fp_gpio_atr
   // instantiation in radio_legacy.v for why (no sanctioned host-software
   // path reaches this register to turn it on at runtime).
   wire enable;
   setting_reg #(.my_addr(BASE), .awidth(8), .width(1), .at_reset(1)) sr_enable
     (.clk(clk), .rst(reset), .strobe(set_stb), .addr(set_addr), .in(set_data),
      .out(enable), .changed());

   wire signed [15:0] i_in = sample[31:16];
   wire signed [15:0] q_in = sample[15:0];

   // Stage A: latch the current sample.
   reg signed [15:0] i_cur, q_cur;
   // Stage B: previous sample (one strobe period behind i_cur/q_cur).
   reg signed [15:0] i_prev, q_prev;
   reg signed [31:0] cross_a, cross_b;   // Q[n]*I[n-1], I[n]*Q[n-1]
   // Stage C: the discriminator value itself.
   reg signed [32:0] disc;               // Im{s[n]*conj(s[n-1])}

   reg strobe_d1, strobe_d2, strobe_d3;

   always @(posedge clk) begin
      if (reset) begin
         i_cur <= 16'sd0; q_cur <= 16'sd0;
         i_prev <= 16'sd0; q_prev <= 16'sd0;
         cross_a <= 32'sd0; cross_b <= 32'sd0;
         disc <= 33'sd0;
         bit_out <= 1'b0; bit_valid <= 1'b0;
         strobe_d1 <= 1'b0; strobe_d2 <= 1'b0; strobe_d3 <= 1'b0;
      end else begin
         strobe_d1 <= strobe;
         strobe_d2 <= strobe_d1;
         strobe_d3 <= strobe_d2;

         // Capture the new sample.
         if (strobe) begin
            i_cur <= i_in;
            q_cur <= q_in;
         end

         // Cross-multiply current against previous, then shift previous
         // forward -- ordered so the multiply below always sees this
         // strobe's i_cur/q_cur against the *prior* strobe's i_prev/q_prev.
         if (strobe_d1) begin
            cross_a <= q_cur * i_prev;
            cross_b <= i_cur * q_prev;
            i_prev  <= i_cur;
            q_prev  <= q_cur;
         end

         // Subtract (sign-extended to avoid overflow on the difference).
         if (strobe_d2)
            disc <= {cross_a[31], cross_a} - {cross_b[31], cross_b};

         // Slice one cycle later so `disc` has settled.
         bit_valid <= strobe_d3;
         if (strobe_d3)
            bit_out <= enable & ~disc[32];   // disc >= 0 -> bit=1 (mark)
      end
   end

endmodule
