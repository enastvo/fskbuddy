//
// gsc_framer: batch/block synchronizer for GSC (Golay Sequential Code),
// consuming the recovered one-symbol-per-clock stream from a
// pocsag_bitsync instance (reused as-is at GSC's own sps -- see
// radio_legacy.v's gsc_bitsync instantiation; that module was already
// protocol-agnostic, nothing GSC-specific needed there).
//
// Framing facts (comma/half-bit-gap structure, LSB-first + doubled-bit
// transmission, Word2=Word1-complement for the control word) come from
// gsc.py's module docstring -- primary-source-confirmed (a Motorola
// patent) plus cross-checked against multimon-ng's real, independent GSC
// decoder (github.com/EliasOenal/multimon-ng, public domain). See there
// for the full writeup; this module just implements the RX side of the
// same wire format gsc.py's build_bitstream()/build_word_pair_bits()
// produce on TX.
//
// Unlike pocsag_framer.v (fixed 16-codewords-per-batch, resyncs once per
// batch), this project's own GSC convention puts a full comma before
// EVERY block (not just the first -- see gsc.py's build_bitstream
// docstring on why: GSC's real variable-length batches have no fixed
// resync point analogous to POCSAG's, so this resyncs on every block
// instead of tracking batch position). That trades a little airtime for
// a simpler framer: no batch-length bookkeeping, self-heals every 121
// symbols instead of only at batch boundaries.
//
// Sequence once locked: comma(28) -> Word1(46 raw symbols = 23 bits x2)
// -> half-bit gap(1) -> Word2(46 raw symbols). Initial acquisition
// correlates the very first Word1 against the known control-word pattern
// (CONTROL_W1_PATTERN below, computed by gsc.py from CONTROL_WORD_INFO=713
// -- see there); once found, subsequent blocks' Word1/Word2 are accepted
// as-is (their content is real address/data payload, not a fixed
// pattern) and shipped raw to host software for Golay decode -- same
// division of labor as pocsag_framer.v (PHY/framing in fabric, FEC decode
// in host software).
//
// Known, characterized edge case (confirmed on real hardware, digital
// loopback): if a transmission's trailing control word is IMMEDIATELY
// followed by another transmission's own fresh 200-bit preamble with zero
// gap (e.g. concatenating repeat bursts bit-for-bit, the way
// pocsag_framer.v's POCSAG side does safely), the comma-based per-block
// resync above can transiently mistake preamble content for a comma+word
// pair -- both are alternating patterns, and the comma check is
// deliberately tolerant (COMMA_TOL below) the same way the word
// correlator is. This is SAFE (the resulting garbage word1/word2 pair
// fails Golay's error threshold on the host side -- gsc.LiveParser's
// MAX_TRUSTED_ERRORS -- and is correctly discarded, never producing a
// corrupted page) and self-healing (full ST_SEARCH correlation re-finds
// the next real control word), but can cost throughput right at that
// boundary. transceiver.py's PocsagTransmitter.send() works around this
// on the TX side for GSC specifically (a real inter-repeat gap instead of
// zero-gap concatenation) rather than making this module's resync more
// elaborate to fully disambiguate every case.
//
module gsc_framer
  (
   input        clk,
   input        reset,
   input        en,
   input        sym_bit,
   input        sym_valid,
   output reg [22:0] word1,
   output reg [22:0] word2,
   output reg        pair_valid,   // pulses one clk when word1/word2 are freshly captured
   output reg [7:0]  pair_count,   // free-running, increments per pair_valid
   output reg        locked
   );

   // 46-symbol transmission pattern for the control word's Word1
   // (info=713 -- gsc.py's CONTROL_WORD_INFO -- Golay-encoded, then
   // doubled+LSB-first per gsc.py's _codeword_symbols(), computed once in
   // Python and pasted here as a fixed correlation target). Same
   // technique pocsag_framer.v uses for its own SYNC_WORD, just longer
   // (46 vs 32 bits) and against a value this project's own gsc.py
   // computes, not a POCSAG-spec constant. Word 2's expected pattern is
   // this value's bitwise complement -- verified in gsc.py's own test
   // suite that Word2=Word1-codeword-complement is always another valid
   // Golay codeword, and that relationship survives the doubling/
   // bit-reversal transmission encoding unchanged (also confirmed there).
   localparam [45:0] CONTROL_W1_PATTERN = 46'h30c3cc3c000c;
   localparam [5:0]  HAMMING_TOL = 6'd4;  // ~9% of 46 bits, similar margin to
                                          // pocsag_framer's 2/32 (~6%)

   localparam ST_SEARCH = 3'd0;  // sliding-window correlating for the control word
   localparam ST_GAP    = 3'd1;  // the 1-symbol half-bit gap after a word1
   localparam ST_WORD2  = 3'd2;  // reading word2 (46 symbols)
   localparam ST_COMMA  = 3'd3;  // reading/verifying the 28-symbol comma before the next word1
   localparam ST_WORD1  = 3'd4;  // reading word1 (46 symbols, any content -- already locked)

   reg [2:0]  state;
   reg [45:0] sr;          // rolling/accumulating shift register
   reg [6:0]  sym_cnt;     // position within the current comma/word1/word2 run
   reg        polarity_inverted;

   function [5:0] popcount46;
      input [45:0] v;
      integer      i;
      begin
         popcount46 = 6'd0;
         for (i = 0; i < 46; i = i + 1)
            popcount46 = popcount46 + {5'd0, v[i]};
      end
   endfunction

   wire [45:0] shifted_sr = {sr[44:0], sym_bit};
   wire [45:0] xor_a = shifted_sr ^ CONTROL_W1_PATTERN;
   wire [45:0] xor_b = shifted_sr ^ ~CONTROL_W1_PATTERN;
   wire        match_a = (popcount46(xor_a) <= HAMMING_TOL);
   wire        match_b = (popcount46(xor_b) <= HAMMING_TOL);

   // word[i] = raw46[45 - 2*i] -- de-doubles (keeps the first copy of
   // each repeated bit) and un-reverses the LSB-first transmission order
   // in one step (see gsc.py's module docstring on bit order: symbol i
   // and i+1 both carry codeword bit i//2, transmitted LSB-first, so the
   // first-arrived copy of codeword bit k sits 45-2k positions from the
   // shift register's newest end). Pure wiring, no logic.
   function [22:0] extract_word;
      input [45:0] raw46;
      integer      i;
      begin
         for (i = 0; i < 23; i = i + 1)
            extract_word[i] = raw46[45 - 2*i];
      end
   endfunction

   // Comma check: 28 symbols, alternating, tolerant of a few mismatches --
   // same Hamming-tolerant philosophy as the word correlator above, just
   // checking "did every adjacent pair toggle" instead of matching a
   // fixed pattern (the comma's own polarity varies with whatever word1
   // follows it -- see gsc.py's comma_bits()). Built from shifted_sr, not
   // the registered sr -- same reasoning as extract_word(shifted_sr) above
   // in ST_WORD1/ST_WORD2: on the cycle the 28th symbol arrives, sr itself
   // still only holds the first 27 (this cycle's update hasn't landed
   // yet), so checking against sr instead of shifted_sr would compare
   // against a stale, never-shifted-in bit 27 -- caught by a real-hardware
   // loopback test, not by inspection (round 1 of this module locked onto
   // the first control word fine via ST_SEARCH's own direct correlation,
   // then failed nearly every comma re-check afterward and kept dropping
   // back to search). shifted_sr[27:0] holds the last 28 symbols once a
   // comma run completes; XOR each bit against its neighbor -- a clean
   // alternating run makes every one of those XORs a 1, so counting the
   // 0s directly gives the mismatch count.
   wire [26:0] comma_xor = shifted_sr[27:1] ^ shifted_sr[26:0];
   function [4:0] comma_mismatches;
      input [26:0] x;
      integer      i;
      begin
         comma_mismatches = 5'd0;
         for (i = 0; i < 27; i = i + 1)
            comma_mismatches = comma_mismatches + {4'd0, ~x[i]};
      end
   endfunction
   localparam [4:0] COMMA_TOL = 5'd3;  // out of 27 adjacent-pair checks

   always @(posedge clk) begin
      if (reset || !en) begin
         state              <= ST_SEARCH;
         sr                 <= 46'd0;
         sym_cnt            <= 7'd0;
         polarity_inverted  <= 1'b0;
         word1              <= 23'd0;
         word2              <= 23'd0;
         pair_valid         <= 1'b0;
         pair_count         <= 8'd0;
         locked             <= 1'b0;
      end else begin
         pair_valid <= 1'b0;

         if (sym_valid) begin
            case (state)

              // Slide a 46-bit window over the incoming stream, looking
              // for the control word's Word1 (either polarity -- 2-FSK's
              // inherent mark/space ambiguity, same reasoning
              // pocsag_framer.v's dual xor_a/xor_b match already uses).
              ST_SEARCH: begin
                 locked <= 1'b0;
                 sr     <= shifted_sr;
                 if (match_a || match_b) begin
                    polarity_inverted <= match_b;
                    word1   <= match_b ? ~extract_word(shifted_sr) : extract_word(shifted_sr);
                    sym_cnt <= 7'd0;
                    state   <= ST_GAP;
                 end
              end

              // Exactly one symbol, ignored -- its own value carries no
              // information beyond timing (gsc.py's half_bit_gap_polarity
              // is derived from word2's first bit, not independently
              // meaningful on receive).
              ST_GAP: begin
                 sr      <= 46'd0;
                 sym_cnt <= 7'd0;
                 state   <= ST_WORD2;
              end

              ST_WORD2: begin
                 sr <= shifted_sr;
                 if (sym_cnt == 7'd45) begin
                    word2      <= polarity_inverted ? ~extract_word(shifted_sr) : extract_word(shifted_sr);
                    pair_valid <= 1'b1;
                    pair_count <= pair_count + 8'd1;
                    locked     <= 1'b1;
                    sr         <= 46'd0;
                    sym_cnt    <= 7'd0;
                    state      <= ST_COMMA;
                 end else begin
                    sym_cnt <= sym_cnt + 7'd1;
                 end
              end

              // Ongoing (already-locked) block tracking: verify the next
              // comma still looks like a comma -- if it doesn't, that's
              // this design's only "are we still in sync" signal (no
              // batch-length bookkeeping to fall back on -- see header),
              // so drop back to searching rather than trust a
              // possibly-slipped bit position.
              ST_COMMA: begin
                 sr <= shifted_sr;
                 if (sym_cnt == 7'd27) begin
                    if (comma_mismatches(comma_xor) <= COMMA_TOL) begin
                       sr      <= 46'd0;
                       sym_cnt <= 7'd0;
                       state   <= ST_WORD1;
                    end else begin
                       locked  <= 1'b0;
                       sr      <= 46'd0;
                       sym_cnt <= 7'd0;
                       state   <= ST_SEARCH;
                    end
                 end else begin
                    sym_cnt <= sym_cnt + 7'd1;
                 end
              end

              ST_WORD1: begin
                 sr <= shifted_sr;
                 if (sym_cnt == 7'd45) begin
                    word1   <= polarity_inverted ? ~extract_word(shifted_sr) : extract_word(shifted_sr);
                    sr      <= 46'd0;
                    sym_cnt <= 7'd0;
                    state   <= ST_GAP;
                 end else begin
                    sym_cnt <= sym_cnt + 7'd1;
                 end
              end

              // state is 3 bits but only 5 of 8 encodings are used above;
              // a clocked case with missing arms just holds previous state
              // (normal flip-flop behavior, not a latch-inference risk --
              // unlike a combinational always @*), but an unreachable
              // encoding reached via e.g. an SEU would otherwise wedge the
              // FSM permanently since nothing would ever match it again.
              // Recover to ST_SEARCH instead.
              default: begin
                 locked  <= 1'b0;
                 sr      <= 46'd0;
                 sym_cnt <= 7'd0;
                 state   <= ST_SEARCH;
              end

            endcase
         end
      end
   end
endmodule
