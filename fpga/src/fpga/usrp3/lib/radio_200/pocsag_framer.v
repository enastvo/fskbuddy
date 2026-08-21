//
// pocsag_framer: batch/frame synchronizer for POCSAG. Consumes the
// recovered one-bit-per-symbol stream from pocsag_bitsync and finds the
// 32-bit frame sync code (0x7CD215D8), which also resolves the FSK
// polarity ambiguity (since 2-FSK on its own can't tell you which tone is
// "1" -- only the sync word's known bit pattern can). Once locked, shifts
// out each subsequent 32-bit codeword raw (still BCH-encoded -- decode and
// error correction happen in host software, this is PHY/framing only).
// After 16 codewords (8 frames x 2 words = one full batch), drops back to
// searching for the next sync word rather than assuming continued
// alignment, so any bit slip self-heals at the next batch boundary.
//
module pocsag_framer
  (
   input        clk,
   input        reset,
   input        en,
   input        sym_bit,
   input        sym_valid,
   output reg [31:0] codeword,
   output reg        codeword_valid,  // pulses one clk when codeword is a freshly captured word
   output reg [7:0]  codeword_count,  // free-running, increments per codeword_valid -- host's
                                       // "new data" indicator (compare against last-seen value)
   output reg        locked
   );

   localparam SYNC_WORD  = 32'h7CD215D8;
   localparam HAMMING_TOL = 2; // allow up to this many bit errors when matching the sync word

   localparam ST_SEARCH = 1'b0;
   localparam ST_LOCKED = 1'b1;

   reg        state;
   reg [31:0] sr;
   reg [5:0]  bit_cnt;   // 0..31 within the current codeword
   reg [4:0]  cw_cnt;    // 0..15 codewords within the current batch
   reg        polarity_invert;

   wire [31:0] shifted_sr = {sr[30:0], sym_bit};
   wire [31:0] xor_a = shifted_sr ^ SYNC_WORD;
   wire [31:0] xor_b = shifted_sr ^ ~SYNC_WORD;

   function [5:0] popcount32;
      input [31:0] v;
      integer      i;
      begin
         popcount32 = 6'd0;
         for (i = 0; i < 32; i = i + 1)
            popcount32 = popcount32 + {5'd0, v[i]};
      end
   endfunction

   wire match_a = (popcount32(xor_a) <= HAMMING_TOL[5:0]);
   wire match_b = (popcount32(xor_b) <= HAMMING_TOL[5:0]);

   always @(posedge clk) begin
      if (reset || !en) begin
         state           <= ST_SEARCH;
         sr              <= 32'd0;
         bit_cnt         <= 6'd0;
         cw_cnt          <= 5'd0;
         polarity_invert <= 1'b0;
         codeword        <= 32'd0;
         codeword_valid  <= 1'b0;
         codeword_count  <= 8'd0;
         locked          <= 1'b0;
      end else begin
         codeword_valid <= 1'b0;

         if (sym_valid) begin
            sr <= shifted_sr;

            case (state)
              ST_SEARCH: begin
                 locked <= 1'b0;
                 if (match_a || match_b) begin
                    state           <= ST_LOCKED;
                    polarity_invert <= match_b; // matched the inverted sync word -> flip payload bits
                    bit_cnt         <= 6'd0;
                    cw_cnt          <= 5'd0;
                    locked          <= 1'b1;
                 end
              end

              ST_LOCKED: begin
                 if (bit_cnt == 6'd31) begin
                    codeword       <= polarity_invert ? ~shifted_sr : shifted_sr;
                    codeword_valid <= 1'b1;
                    codeword_count <= codeword_count + 8'd1;
                    bit_cnt        <= 6'd0;
                    if (cw_cnt == 5'd15) begin
                       // End of batch -- re-verify sync rather than assume alignment holds.
                       state  <= ST_SEARCH;
                       cw_cnt <= 5'd0;
                    end else begin
                       cw_cnt <= cw_cnt + 5'd1;
                    end
                 end else begin
                    bit_cnt <= bit_cnt + 6'd1;
                 end
              end
            endcase
         end
      end
   end
endmodule
