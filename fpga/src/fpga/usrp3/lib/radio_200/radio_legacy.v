//
// Copyright 2013 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: LGPL-3.0-or-later
//


// radio top level module for b200
//  Contains all clock-rate DSP components, all radio and hardware controls and settings

module radio_legacy
  #(
    parameter RADIO_FIFO_SIZE = 13,
    parameter SAMPLE_FIFO_SIZE = 11,
    parameter FP_GPIO = 0,
    parameter NEW_HB_INTERP = 0,
    parameter NEW_HB_DECIM = 0,
    parameter SOURCE_FLOW_CONTROL = 0,
    parameter USER_SETTINGS = 0,
    parameter DEVICE = "SPARTAN6"
  )
  (input radio_clk, input radio_rst,
   input [31:0] rx, output reg [31:0] tx,
   input [31:0] fe_gpio_in, output [31:0] fe_gpio_out, output [31:0] fe_gpio_ddr,
   input [9:0] fp_gpio_in, output [9:0] fp_gpio_out, output [9:0] fp_gpio_ddr,
   input pps, input time_sync,
   input bus_clk, input bus_rst,
   input [63:0]  tx_tdata, input tx_tlast, input tx_tvalid, output tx_tready,
   output [63:0] rx_tdata, output rx_tlast, output rx_tvalid, input rx_tready,
   input [63:0]  ctrl_tdata, input ctrl_tlast, input ctrl_tvalid, output ctrl_tready,
   output [63:0] resp_tdata, output resp_tlast, output resp_tvalid, input resp_tready,

   output reg [63:0] vita_time_b,

   output [63:0] debug
   );


   // ///////////////////////////////////////////////////////////////////////////////
   // FIFO Interfacing to the bus clk domain
   // in_tdata splits to tx_tdata and ctrl_tdata
   // rx_tdata and resp_tdata get muxed to out_tdata
   // Everything except rx flow control must cross in to radio_clk domain before further use
   // _b signifies bus_clk domain, _r signifies radio_clk domain

   wire [63:0] 	 ctrl_tdata_r;
   wire 	 ctrl_tready_r, ctrl_tvalid_r;
   wire 	 ctrl_tlast_r;

   wire [63:0] 	 resp_tdata_r;
   wire 	 resp_tready_r, resp_tvalid_r;
   wire 	 resp_tlast_r;

   wire [63:0] 	 rx_tdata_r;
   wire 	 rx_tready_r, rx_tvalid_r;
   wire 	 rx_tlast_r;

   wire [63:0] 	 rx_err_tdata_r;
   wire 	 rx_err_tready_r, rx_err_tvalid_r;
   wire 	 rx_err_tlast_r;

   wire [63:0]     rx_prefc_tdata_r;
   wire   rx_prefc_tready_r, rx_prefc_tvalid_r;
   wire   rx_prefc_tlast_r;

   wire [63:0]     rx_postfc_tdata_r;
   wire   rx_postfc_tready_r, rx_postfc_tvalid_r;
   wire   rx_postfc_tlast_r;

   wire [63:0] 	 tx_tdata_r;
   wire 	 tx_tready_r, tx_tvalid_r;
   wire 	 tx_tlast_r;

   wire [63:0] 	 txresp_tdata, txresp_tdata_r;
   wire 	 txresp_tready, txresp_tready_r, txresp_tvalid, txresp_tvalid_r;
   wire 	 txresp_tlast, txresp_tlast_r;

   wire [63:0] 	 rmux_tdata_r;
   wire 	 rmux_tlast_r, rmux_tvalid_r, rmux_tready_r;

   wire [31:0] 	 tx_idle;
   wire [3:0] 	 ibs_state;
   wire [63:0] 	 rx_tdata_int;
   wire 	 rx_tready_int, rx_tvalid_int;
   wire 	 rx_tlast_int;

   // Declared up here (rather than next to its driving fsk_demod instance,
   // down by ddc_chain) because XST's generate-block elaboration didn't
   // reliably pick up a forward reference to it from inside the
   // `add_fp_gpio` generate block below -- it silently decided the driving
   // assignment was dead ("Assignment to fsk_bit_out ignored, since the
   // identifier is never used") and optimized the whole fsk_demod instance
   // away. Declaring it before that generate block avoids the issue.
   wire fsk_bit_out, fsk_bit_valid;

   // Same reasoning: declared here, ahead of the USER_SETTINGS generate
   // block that drives it (down near the SR_LOOPBACK register) and the
   // rx_fe mux that reads it (down by ddc_chain).
   wire user_loopback;

   // Same reasoning again, both directions: pocsag_sps/pocsag_en are driven
   // inside the USER_SETTINGS generate block but consumed by pocsag_bitsync
   // (instantiated later, by ddc_chain); pocsag_codeword/codeword_valid/
   // codeword_count/locked are driven by pocsag_framer (also down there)
   // but read from inside the (earlier-appearing) USER_SETTINGS generate
   // block's readback case statement.
   wire [15:0] pocsag_sps;
   wire        pocsag_en;
   wire [31:0] pocsag_codeword;
   wire        pocsag_codeword_valid;
   wire [7:0]  pocsag_codeword_count;
   wire        pocsag_locked;

   // Same forward-declaration reasoning as pocsag_codeword etc. above:
   // clip_detect is instantiated down by ddc_chain, but read from inside
   // the (earlier-appearing) USER_SETTINGS generate block's readback case
   // statement (RB_PHY_STATUS).
   wire [7:0]  clip_count;

   // Same forward-declaration reasoning again: fsk_demod's new disc
   // outputs are consumed by channel_width_detect (instantiated further
   // down, by fsk_demod); channel_width_detect's own outputs are read from
   // inside the USER_SETTINGS readback case statement above.
   wire [32:0] fsk_disc_out;
   wire        fsk_disc_valid;
   wire        channel_width_narrow, channel_width_locked;

   // Same forward-declaration reasoning again: gsc_sps/gsc_en are driven
   // inside the USER_SETTINGS generate block but consumed by gsc_bitsync/
   // gsc_framer (instantiated later, alongside their POCSAG counterparts);
   // the framer's word1/word2/pair_valid/pair_count/locked are driven
   // there but read from inside the (earlier-appearing) USER_SETTINGS
   // readback case statement (RB_GSC_STATUS). gsc_bitsync is just a second
   // instance of the existing pocsag_bitsync module -- it was already
   // protocol-agnostic (sps/en/raw_bit in, sym_bit/sym_valid out, nothing
   // POCSAG-specific about its logic), so GSC reuses it as-is at its own
   // 600-baud sps rather than needing a variant.
   wire [15:0] gsc_sps;
   wire        gsc_en;
   wire [22:0] gsc_word1, gsc_word2;
   wire        gsc_pair_valid;
   wire [7:0]  gsc_pair_count;
   wire        gsc_locked;


   axi_fifo_2clk #(.WIDTH(65), .SIZE(0/*minimal*/)) ctrl_fifo
     (.reset(bus_rst),
      .i_aclk(bus_clk), .i_tvalid(ctrl_tvalid), .i_tready(ctrl_tready), .i_tdata({ctrl_tlast, ctrl_tdata}),
      .o_aclk(radio_clk), .o_tvalid(ctrl_tvalid_r), .o_tready(ctrl_tready_r), .o_tdata({ctrl_tlast_r, ctrl_tdata_r}));

   axi_fifo_2clk #(.WIDTH(65), .SIZE(RADIO_FIFO_SIZE)) tx_fifo
     (.reset(bus_rst),
      .i_aclk(bus_clk), .i_tvalid(tx_tvalid), .i_tready(tx_tready), .i_tdata({tx_tlast, tx_tdata}),
      .o_aclk(radio_clk), .o_tvalid(tx_tvalid_r), .o_tready(tx_tready_r), .o_tdata({tx_tlast_r, tx_tdata_r}));

   axi_fifo_2clk #(.WIDTH(65), .SIZE(0/*minimal*/)) resp_fifo
     (.reset(radio_rst),
      .i_aclk(radio_clk), .i_tvalid(rmux_tvalid_r), .i_tready(rmux_tready_r), .i_tdata({rmux_tlast_r, rmux_tdata_r}),
      .o_aclk(bus_clk), .o_tvalid(resp_tvalid), .o_tready(resp_tready), .o_tdata({resp_tlast, resp_tdata}));

   axi_fifo_2clk #(.WIDTH(65), .SIZE(RADIO_FIFO_SIZE)) rx_fifo
     (.reset(radio_rst),
      .i_aclk(radio_clk), .i_tvalid(rx_tvalid_r), .i_tready(rx_tready_r), .i_tdata({rx_tlast_r, rx_tdata_r}),
      .o_aclk(bus_clk), .o_tvalid(rx_tvalid_int), .o_tready(rx_tready_int), .o_tdata({rx_tlast_int, rx_tdata_int}));

   axi_packet_gate #(.WIDTH(64), .SIZE(SAMPLE_FIFO_SIZE), .USE_AS_BUFF(0)) buffer_whole_pkt
     (
      .clk(bus_clk), .reset(bus_rst), .clear(1'b0),
      .i_tdata(rx_tdata_int), .i_tlast(rx_tlast_int), .i_terror(1'b0), .i_tvalid(rx_tvalid_int), .i_tready(rx_tready_int),
      .o_tdata(rx_tdata), .o_tlast(rx_tlast), .o_tvalid(rx_tvalid), .o_tready(rx_tready));

   ///////////////////////////////////////////////////////////////////////////////////////
   // Setting bus and controls

   wire [63:0]    ctrl_tdata_proc;
   wire           ctrl_tready_proc, ctrl_tvalid_proc;
   wire           ctrl_tlast_proc;

   localparam SR_LOOPBACK     = 8'd6;
   localparam SR_SPI          = 8'd8;
   localparam SR_ATR          = 8'd12; // thorugh 8'd18
   localparam SR_TEST         = 8'd21;
   localparam SR_CODEC_IDLE   = 8'd22;
   localparam SR_FSK_DEMOD    = 8'd24;
   localparam SR_READBACK     = 8'd32;
   localparam SR_TX_CTRL      = 8'd64;
   localparam SR_RX_CTRL      = 8'd96;
   localparam SR_TIME         = 8'd128;
   localparam SR_RX_FMT       = 8'd136;
   localparam SR_TX_FMT       = 8'd138;
   localparam SR_RX_DSP       = 8'd144;
   localparam SR_TX_DSP       = 8'd184;
   localparam SR_FP_GPIO      = 8'd200; // thorugh 8'd206
   localparam SR_USER_SR_BASE = 8'd253;
   localparam SR_USER_RB_ADDR = 8'd255;

   wire           set_stb;
   wire [7:0]     set_addr;
   wire [31:0]    set_data;
   wire [31:0]    test_readback;
   wire [9:0] 	  fp_gpio_readback;
   wire           run_rx, run_tx;
   wire           rx_flow_ctrl_busy;

   reg [63:0]     rb_data;
   wire [2:0]     rb_addr;

   wire [63:0] vita_time, vita_time_lastpps;
   timekeeper_legacy #(.SR_TIME_HI(SR_TIME), .SR_TIME_LO(SR_TIME+1), .SR_TIME_CTRL(SR_TIME+2)) timekeeper
     (.clk(radio_clk), .reset(radio_rst), .pps(pps), .sync_in(time_sync), .strobe(1'b1),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .vita_time(vita_time), .vita_time_lastpps(vita_time_lastpps),
      .sync_out());

   wire [31:0] debug_radio_ctrl_proc;
   radio_ctrl_proc radio_ctrl_proc
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .ctrl_tdata(ctrl_tdata_proc), .ctrl_tlast(ctrl_tlast_proc), .ctrl_tvalid(ctrl_tvalid_proc), .ctrl_tready(ctrl_tready_proc),
      .resp_tdata(resp_tdata_r), .resp_tlast(resp_tlast_r), .resp_tvalid(resp_tvalid_r), .resp_tready(resp_tready_r),
      .vita_time(vita_time),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .ready(1'b1), .readback(rb_data),
      .debug(debug_radio_ctrl_proc));

   reg [63:0]     rb_data_user;
generate
   if (USER_SETTINGS == 1) begin
      wire           set_stb_user;
      wire [7:0]     set_addr_user;
      wire [31:0]    set_data_user;
      wire [7:0]     rb_addr_user;

      user_settings #(.BASE(SR_USER_SR_BASE)) user_settings
        (.clk(radio_clk), .rst(radio_rst),
         .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
         .set_stb_user(set_stb_user), .set_addr_user(set_addr_user), .set_data_user(set_data_user));

      setting_reg #(.my_addr(SR_USER_RB_ADDR), .awidth(8), .width(8)) user_rb_addr
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb), .addr(set_addr), .in(set_data),
         .out(rb_addr_user), .changed());

      // ----------------------------------
      // Enter user settings registers here
      // ----------------------------------

      // Example code for 32-bit settings registers and 64-bit readback registers
      //
      // To test this, modify the *_core.v file for your specific USRP and set
      // USER_SETTINGS=1 for the parameters for the radio_legacy instantiation.
      //
      // You can then use the get_user_settings_iface() like this:
      //
      // auto usrp = multi_usrp::make("type=b200,enable_user_regs");
      // auto regs = usrp->get_user_settings_iface(0);
      // regs->poke32(0, 0xCAFE);
      // regs->poke32(4, 0xBEEF);
      // std::cout << boost::format("0x%016X") % regs->peek64(0) << std::endl;
      wire [31:0] user_reg_0_value, user_reg_1_value;

      setting_reg #(.my_addr(8'd0), .awidth(8), .width(32)) user_reg_0
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb_user), .addr(set_addr_user), .in(set_data_user),
         .out(user_reg_0_value), .changed());

      setting_reg #(.my_addr(8'd1), .awidth(8), .width(32)) user_reg_1
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb_user), .addr(set_addr_user), .in(set_data_user),
         .out(user_reg_1_value), .changed());

      // Host-reachable twin of the pre-existing (but unreachable -- nothing
      // ever writes SR_LOOPBACK) `sr_loopback` register below. ORed into the
      // same mux select so either path can assert it. Byte address from
      // get_user_settings_iface(): poke32(2*4, 1) to enable, poke32(8, 0) to
      // disable.
      wire [31:0] user_loopback_value;

      setting_reg #(.my_addr(8'd2), .awidth(8), .width(32)) user_reg_loopback
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb_user), .addr(set_addr_user), .in(set_data_user),
         .out(user_loopback_value), .changed());

      assign user_loopback = user_loopback_value[0];

      // POCSAG PHY control: bits[15:0] = samples-per-bit (RX rate / POCSAG
      // bit rate, host computes this from whatever rate it configured);
      // bit[16] = enable (also asynchronously resets the bitsync/framer
      // state machines while low). poke32(3*4, value) to set.
      wire [31:0] pocsag_ctrl_value;

      setting_reg #(.my_addr(8'd3), .awidth(8), .width(32)) user_reg_pocsag_ctrl
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb_user), .addr(set_addr_user), .in(set_data_user),
         .out(pocsag_ctrl_value), .changed());

      assign pocsag_sps = pocsag_ctrl_value[15:0];
      assign pocsag_en  = pocsag_ctrl_value[16];

      // GSC PHY control: same {bit16:enable, bits15:0:sps} layout as
      // pocsag_ctrl above, just a second independent register -- both
      // bitsync/framer chains run concurrently in fabric regardless of
      // which one host software is actually polling (cheap: no DSP48A1
      // cost, see gsc_bitsync/gsc_framer below). poke32(4*4, value) to
      // set.
      wire [31:0] gsc_ctrl_value;

      setting_reg #(.my_addr(8'd4), .awidth(8), .width(32)) user_reg_gsc_ctrl
        (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb_user), .addr(set_addr_user), .in(set_data_user),
         .out(gsc_ctrl_value), .changed());

      assign gsc_sps = gsc_ctrl_value[15:0];
      assign gsc_en  = gsc_ctrl_value[16];

      always @* begin
         case(rb_addr_user)
            8'd0 : rb_data_user <= {user_reg_1_value, user_reg_0_value};
            8'd1 : rb_data_user <= {63'd0, user_loopback_value[0]};
            // peek64(2*8): {23'b0, locked, codeword_count[7:0], codeword[31:0]}
            8'd2 : rb_data_user <= {23'd0, pocsag_locked, pocsag_codeword_count, pocsag_codeword};
            // peek64(3*8) -- RB_GSC_STATUS: {9'b0, locked, pair_count[7:0],
            // word1[22:0], word2[22:0]}. word1/word2 are gsc_framer's most
            // recently captured Golay(23,12,7) codewords, raw -- FEC
            // decode (golay.py) and message assembly (gsc.py's LiveParser)
            // are host-side, same PHY/framing-only split pocsag_framer
            // uses. pair_count is a free-running counter, diffed the same
            // way as codeword_count above.
            8'd3 : rb_data_user <= {9'd0, gsc_locked, gsc_pair_count, gsc_word1, gsc_word2};
            // peek64(4*8) -- RB_PHY_STATUS: protocol-agnostic PHY status,
            // shared by whatever framer is active. clip_count is
            // clip_detect.v's free-running counter (see there for the
            // diff-against-last-seen-value host-side convention, same as
            // codeword_count above). width_narrow/width_locked are
            // channel_width_detect.v's classification (see there for the
            // calibration behind THRESHOLD_SHIFT_WIDE/NARROW).
            8'd4 : rb_data_user <= {32'd0, channel_width_narrow, channel_width_locked,
                                     clip_count, 22'd0};
            default : rb_data_user <= 64'd0;
         endcase
      end

   end else begin    //for USER_SETTINGS == 1
      always @* rb_data_user <= 64'd0;
      assign user_loopback = 1'b0;
      assign pocsag_sps = 16'd0;
      assign pocsag_en  = 1'b0;
      assign gsc_sps = 16'd0;
      assign gsc_en  = 1'b0;
   end
endgenerate

   always @*
     case(rb_addr)
       3'd0 : rb_data <= { 32'b0, test_readback };
       3'd1 : rb_data <= vita_time;
       3'd2 : rb_data <= vita_time_lastpps;
       3'd3 : rb_data <= {tx, rx};
       3'd4 : rb_data <= {54'h0,fp_gpio_readback};
       3'd5 : rb_data <= {59'h0,rx_flow_ctrl_busy,ibs_state[3:0]}; // Monitor state of RX state machine.
//     3'd6 : rb_data <= <unused>;
       3'd7 : rb_data <= rb_data_user;
       default : rb_data <= 64'd0;
     endcase // case (rb_addr)

   //
   // Sample VITA_TIME into the bus_clk domain for use by instrumentation.
   //
   wire [63:0] vita_time_b_int;
   wire        vita_time_b_valid;

    axi_fifo_2clk #(.WIDTH(64), .SIZE(0)) vita_time_fifo
     (.reset(radio_rst),
      .i_aclk(radio_clk), .i_tvalid(1'b1), .i_tready(), .i_tdata(vita_time),
      .o_aclk(bus_clk), .o_tvalid(vita_time_b_valid), .o_tready(1'b1), .o_tdata(vita_time_b_int));

   always @(posedge bus_clk)
     if (vita_time_b_valid)
       vita_time_b <= vita_time_b_int;

   // Set this register to loop TX data directly to RX data.
   setting_reg #(.my_addr(SR_LOOPBACK), .awidth(8), .width(1)) sr_loopback
     (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb), .addr(set_addr), .in(set_data),
      .out(loopback), .changed());

   setting_reg #(.my_addr(SR_TEST), .awidth(8), .width(32)) sr_test
     (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb), .addr(set_addr), .in(set_data),
      .out(test_readback), .changed());

   setting_reg #(.my_addr(SR_CODEC_IDLE), .awidth(8), .width(32)) sr_codec_idle
     (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb), .addr(set_addr), .in(set_data),
      .out(tx_idle), .changed());

   setting_reg #(.my_addr(SR_READBACK), .awidth(8), .width(3)) sr_rdback
     (.clk(radio_clk), .rst(radio_rst), .strobe(set_stb), .addr(set_addr), .in(set_data),
      .out(rb_addr), .changed());

   //The fe_atr pins driven by this module are always configured as outputs so default
   //the DDR (data direction register) to be all ones (outputs) so that the drive direction
   //these lines does not change during/after resets.
   gpio_atr #(.BASE(SR_ATR), .WIDTH(32), .FAB_CTRL_EN(0), .DEFAULT_DDR(32'hFFFFFFFF), .DEFAULT_IDLE(32'h00000000)) fe_gpio_atr
     (.clk(radio_clk),.reset(radio_rst),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
      .rx(run_rx), .tx(run_tx),
      .gpio_in(fe_gpio_in), .gpio_out(fe_gpio_out), .gpio_ddr(fe_gpio_ddr),
      .gpio_out_fab(32'h00000000 /* no fabric control */), .gpio_sw_rb() );

   generate
      if (FP_GPIO != 0) begin: add_fp_gpio
         // DEFAULT_DDR/DEFAULT_FAB_CTRL bit 0 = 1: fp_gpio[0] comes up as an
         // output driven by fsk_demod at reset, with no host-side register
         // write needed -- there's no sanctioned public API path to poke
         // SR_FSK_DEMOD or this block's fabric_ctrl mask (they're outside
         // the USER_SETTINGS mechanism), so defaulting both at synthesis
         // time is what makes this observable via UHD's standard GPIO
         // readback at all.
         gpio_atr #(.BASE(SR_FP_GPIO), .WIDTH(10), .FAB_CTRL_EN(1),
                    .DEFAULT_DDR(10'b00_0000_0001), .DEFAULT_FAB_CTRL(10'b00_0000_0001)) fp_gpio_atr
            (.clk(radio_clk),.reset(radio_rst),
            .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
            .rx(run_rx), .tx(run_tx),
            .gpio_in(fp_gpio_in), .gpio_out(fp_gpio_out), .gpio_ddr(fp_gpio_ddr),
            .gpio_out_fab({9'b0, fsk_bit_out}), .gpio_sw_rb(fp_gpio_readback));
      end
   endgenerate



   ///////////////////////////////////////////////////////////////////////////////////////
   // Source flow control

generate
   if (SOURCE_FLOW_CONTROL == 1) begin

      localparam SID_PREFIX_CTRL = 2'd0;
      localparam SID_PREFIX_FC   = 2'd1;

      wire [63:0]    ctrl_tdata_fc;
      wire           ctrl_tready_fc, ctrl_tvalid_fc;
      wire           ctrl_tlast_fc;

      wire [63:0]    ctrl_hdr;
      wire [1:0]     ctrl_dest;

      assign ctrl_dest = (ctrl_hdr[1:0] == SID_PREFIX_FC) ? 2'd1 : 2'd0;

      axi_demux4 #(.ACTIVE_CHAN(4'b0011), .WIDTH(64), .BUFFER(1)) demux_proc_fc
        (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
         .header(ctrl_hdr), .dest(ctrl_dest),
         .i_tdata(ctrl_tdata_r), .i_tlast(ctrl_tlast_r), .i_tvalid(ctrl_tvalid_r), .i_tready(ctrl_tready_r),                  //Input
         .o0_tdata(ctrl_tdata_proc), .o0_tlast(ctrl_tlast_proc), .o0_tvalid(ctrl_tvalid_proc), .o0_tready(ctrl_tready_proc),  //Settings/Readback
         .o1_tdata(ctrl_tdata_fc), .o1_tlast(ctrl_tlast_fc), .o1_tvalid(ctrl_tvalid_fc), .o1_tready(ctrl_tready_fc),          //Flow control
         .o2_tdata(), .o2_tlast(), .o2_tvalid(), .o2_tready(1'b0),                                                            //Unused
         .o3_tdata(), .o3_tlast(), .o3_tvalid(), .o3_tready(1'b0));                                                           //Unused

      source_flow_control_legacy #(.BASE(SR_RX_CTRL+6)) rx_sfc
        (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
         .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
         .fc_tdata(ctrl_tdata_fc), .fc_tlast(ctrl_tlast_fc), .fc_tvalid(ctrl_tvalid_fc), .fc_tready(ctrl_tready_fc),                      //Flow control In
         .in_tdata(rx_prefc_tdata_r), .in_tlast(rx_prefc_tlast_r), .in_tvalid(rx_prefc_tvalid_r), .in_tready(rx_prefc_tready_r),          //RX Input
         .out_tdata(rx_postfc_tdata_r), .out_tlast(rx_postfc_tlast_r), .out_tvalid(rx_postfc_tvalid_r), .out_tready(rx_postfc_tready_r),  //RX Output
         .busy(rx_flow_ctrl_busy));

   end else begin    //for SOURCE_FLOW_CONTROL == 1

      assign ctrl_tdata_proc  = ctrl_tdata_r;
      assign ctrl_tlast_proc  = ctrl_tlast_r;
      assign ctrl_tvalid_proc = ctrl_tvalid_r;
      assign ctrl_tready_r    = ctrl_tready_proc;

      assign rx_postfc_tdata_r   = rx_prefc_tdata_r;
      assign rx_postfc_tlast_r   = rx_prefc_tlast_r;
      assign rx_postfc_tvalid_r  = rx_prefc_tvalid_r;
      assign rx_prefc_tready_r   = rx_postfc_tready_r;

      assign rx_flow_ctrl_busy   = 1'b0;

   end

endgenerate

   // /////////////////////////////////////////////////////////////////////////////////
   //  TX Chain

   wire [175:0] txsample_tdata;
   wire 	txsample_tvalid, txsample_tready;
   wire [31:0] 	sample_tx;
   wire 	ack_or_error, packet_consumed;
   wire [11:0] 	seqnum;
   wire [63:0] 	error_code;
   wire [31:0] 	sid;
   wire [23:0] tx_fe_i, tx_fe_q;

   wire [31:0] debug_tx_control;

   always @(posedge radio_clk) begin
      tx[31:16] <= (run_tx) ? tx_fe_i[23:8] : tx_idle[31:16];
      tx[15:0]  <= (run_tx) ? tx_fe_q[23:8] : tx_idle[15:0];
   end

   wire [63:0] tx_tdata_i; wire tx_tlast_i, tx_tvalid_i, tx_tready_i;

   new_tx_deframer tx_deframer
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .i_tdata(tx_tdata_i), .i_tlast(tx_tlast_i), .i_tvalid(tx_tvalid_i), .i_tready(tx_tready_i),
      .sample_tdata(txsample_tdata), .sample_tvalid(txsample_tvalid), .sample_tready(txsample_tready),
      .debug());

   new_tx_control #(.BASE(SR_TX_CTRL)) tx_control
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .vita_time(vita_time),
      .ack_or_error(ack_or_error), .packet_consumed(packet_consumed),
      .seqnum(seqnum), .error_code(error_code), .sid(sid),
      .sample_tdata(txsample_tdata), .sample_tvalid(txsample_tvalid), .sample_tready(txsample_tready),
      .sample(sample_tx), .run(run_tx), .strobe(strobe_tx),
      .debug(debug_tx_control));

   tx_responder #(.BASE(SR_TX_CTRL+2)) tx_responder
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .ack_or_error(ack_or_error), .packet_consumed(packet_consumed),
      .seqnum(seqnum), .error_code(error_code), .sid(sid),
      .vita_time(vita_time),
      .o_tdata(txresp_tdata_r), .o_tlast(txresp_tlast_r), .o_tvalid(txresp_tvalid_r), .o_tready(txresp_tready_r));

   wire [31:0]       debug_duc_chain;
   duc_chain #(.BASE(SR_TX_DSP), .DSPNO(0), .WIDTH(24), .NEW_HB_INTERP(NEW_HB_INTERP),.DEVICE(DEVICE)) duc_chain
     (.clk(radio_clk), .rst(radio_rst), .clr(1'b0),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
      .tx_fe_i(tx_fe_i),.tx_fe_q(tx_fe_q),
      .sample(sample_tx), .run(run_tx), .strobe(strobe_tx),
      .debug(debug_duc_chain) );

`ifdef DELETE_FORMAT_CONVERSION
   assign 	     tx_tdata_i = tx_tdata_r;
   assign 	     tx_tlast_i = tx_tlast_r;
   assign 	     tx_tvalid_i = tx_tvalid_r;
   assign 	     tx_tready_r = tx_tready_i;
`else
    chdr_xxxx_to_16sc_chain #(.BASE(SR_TX_FMT)) convert_xxxx_to_16sc
     (.clk(radio_clk), .reset(radio_rst),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
      .i_tdata(tx_tdata_r), .i_tlast(tx_tlast_r), .i_tvalid(tx_tvalid_r), .i_tready(tx_tready_r),
      .o_tdata(tx_tdata_i), .o_tlast(tx_tlast_i), .o_tvalid(tx_tvalid_i), .o_tready(tx_tready_i),
      .debug());
`endif // !`ifdef DELETE_FORMAT_CONVERSION

   // /////////////////////////////////////////////////////////////////////////////////
   //  RX Chain

   wire 	full, eob_rx;
   wire 	strobe_rx;
   wire [31:0] 	sample_rx;
   wire [31:0] 	  rx_sid;
   wire [11:0] 	  rx_seqnum;
   wire [63:0] rx_tdata_i; wire rx_tlast_i, rx_tvalid_i, rx_tready_i;
   reg  [1:0]       rx_err_delay_cnt = 2'd0;
   reg              rx_err_gate = 0;

   wire [31:0] debug_rx_framer;
   new_rx_framer #(.BASE(SR_RX_CTRL+4),.SAMPLE_FIFO_SIZE(SAMPLE_FIFO_SIZE)) new_rx_framer
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .vita_time(vita_time),
      .strobe(strobe_rx), .sample(sample_rx), .run(run_rx), .eob(eob_rx), .full(full),
      .sid(rx_sid), .seqnum(rx_seqnum),
      .o_tdata(rx_tdata_i), .o_tlast(rx_tlast_i), .o_tvalid(rx_tvalid_i), .o_tready(rx_tready_i),
      .debug(debug_rx_framer));

   wire [31:0]       debug_rx_control;
   new_rx_control #(.BASE(SR_RX_CTRL)) new_rx_control
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .vita_time(vita_time),
      .strobe(strobe_rx), .run(run_rx), .eob(eob_rx), .full(full),
      .sid(rx_sid), .seqnum(rx_seqnum),
      .err_tdata(rx_err_tdata_r), .err_tlast(rx_err_tlast_r), .err_tvalid(rx_err_tvalid_r), .err_tready(rx_err_gate & rx_err_tready_r),
      .ibs_state(ibs_state),
      .debug(debug_rx_control));

   wire [31:0] 	     debug_ddc_chain;

   // Digital Loopback TX -> RX (Pipeline immediately inside rx_frontend).
   wire [31:0] 	     rx_fe = (loopback | user_loopback) ? tx : rx;

   // ddc_chain's own run gate, DELIBERATELY separate from run_rx (which
   // new_rx_control/new_rx_framer/gpio_atr still use unchanged, below and
   // above). run_rx drops to 0 the instant the host-facing framing FIFO
   // backs up (new_rx_control.v: `assign run = (ibs_state ==
   // IBS_RUNNING)`, leaves that state on `strobe && full`) -- which
   // previously stalled ddc_chain too, since it shared that same signal,
   // meaning the whole custom PHY chain (channel filter, fsk_demod, both
   // bitsync/framer pairs, clip_detect, channel_width_detect -- all
   // tapped off ddc_chain's own sample_rx/strobe_rx) went idle unless the
   // host kept continuously draining rx_streamer.recv() at full rate,
   // even though none of that PHY logic needs the USB-facing packet path
   // at all (it's read back entirely through USER_SETTINGS registers).
   // Confirmed empirically on real hardware: with stream_cmd(start_cont)
   // issued once but zero subsequent recv() calls, register-visible
   // decode progress stopped within a handful of seconds -- this fixes
   // that. ddc_chain's `run` is just a level-sensitive enable throughout
   // (uhd/fpga/usrp3/lib/dsp/ddc_chain.v: CORDIC/CIC-strober/both
   // halfband decimators all take it as `.enable()`/`.run()` directly);
   // the only other effect of holding it low is resetting the NCO phase
   // accumulator, harmless to avoid by holding this at 1 whenever
   // either protocol's decode is enabled. new_rx_control/new_rx_framer's
   // own run_rx (and both gpio_atr instances' ATR wiring) are completely
   // untouched -- the actual USB packet path still gates exactly as
   // before; this only lets the decimator (and everything downstream of
   // it in fabric) keep running independent of whether the host asks
   // for/drains a packetized RX stream at all.
   wire               run_rx_fabric = pocsag_en | gsc_en;

   ddc_chain #(.BASE(SR_RX_DSP), .DSPNO(0), .WIDTH(24), .NEW_HB_DECIM(NEW_HB_DECIM), .DEVICE(DEVICE)) ddc_chain
     (.clk(radio_clk), .rst(radio_rst), .clr(1'b0),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
      .rx_fe_i({rx_fe[31:16],8'd0}),.rx_fe_q({rx_fe[15:0],8'd0}),
      .sample(sample_rx), .run(run_rx_fabric), .strobe(strobe_rx),
      .debug(debug_ddc_chain) );

   // /////////////////////////////////////////////////////////////////////////////////
   //  RX front-end clipping detection (see clip_detect.v) -- a parallel tap
   //  off sample_rx/strobe_rx, same as the channel filter below and for the
   //  same reason (doesn't touch the primary RX data path). Read via
   //  USER_SETTINGS (peek64(4*8), RB_PHY_STATUS) -- see the readback case
   //  statement above.

   clip_detect clip_detect
     (.clk(radio_clk), .reset(radio_rst),
      .strobe_rx(strobe_rx), .sample_rx(sample_rx),
      .clip_count(clip_count));

   // /////////////////////////////////////////////////////////////////////////////////
   //  Channel filter ahead of the FSK discriminator (see pocsag_channel_filter.v):
   //  a narrowband FIR lowpass, ~20kHz cutoff, sized to POCSAG's real occupied
   //  bandwidth rather than the DDC's full output bandwidth. Fixes a measured
   //  FM-discriminator "threshold effect" -- a delay-and-multiply discriminator's
   //  SNR scales with deviation^2 for a *fixed* input noise bandwidth, so without
   //  this, standard +/-4500Hz POCSAG deviation collapsed completely on real RF
   //  while a much larger deviation worked. This is a parallel tap off
   //  sample_rx/strobe_rx -- it doesn't touch the primary RX data path anything
   //  else (new_rx_framer, etc.) consumes.

   wire        pocsag_filt_strobe;
   wire [31:0] pocsag_filt_sample;

   pocsag_channel_filter pocsag_channel_filter
     (.clk(radio_clk), .reset(radio_rst),
      .strobe_in(strobe_rx), .sample_in(sample_rx),
      .strobe_out(pocsag_filt_strobe), .sample_out(pocsag_filt_sample));

   // /////////////////////////////////////////////////////////////////////////////////
   //  FSK demodulator (delay-and-multiply discriminator, bit decision only --
   //  see fsk_demod.v). Tapped off the channel filter above (not sample_rx/
   //  strobe_rx directly -- see its comment); drives fp_gpio[0] directly from
   //  fabric, bypassing the normal ATR mux, so the bit stream shows up on the
   //  pin in real time with no software in the per-symbol path. Host must set
   //  bit 0 of the SR_FP_GPIO+6 mask (the gpio_atr FAB_CTRL_EN register) to
   //  hand that pin over to fabric control; it defaults to software/ATR-driven
   //  like every other fp_gpio bit.

   fsk_demod #(.BASE(SR_FSK_DEMOD)) fsk_demod
     (.clk(radio_clk), .reset(radio_rst),
      .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
      .strobe(pocsag_filt_strobe), .sample(pocsag_filt_sample),
      .bit_out(fsk_bit_out), .bit_valid(fsk_bit_valid),
      .disc_out(fsk_disc_out), .disc_valid(fsk_disc_valid));

   // /////////////////////////////////////////////////////////////////////////////////
   //  Channel width (12.5kHz vs 25kHz) auto-detection -- see
   //  channel_width_detect.v for the full writeup (estimates FSK deviation
   //  from the discriminator's statistical swing rather than running a
   //  second, DSP48A1-expensive bandpass filter pair). Parallel tap off
   //  the same filtered signal fsk_demod itself consumes, plus fsk_demod's
   //  own new disc_out/disc_valid outputs above.

   channel_width_detect channel_width_detect
     (.clk(radio_clk), .reset(radio_rst),
      .strobe(pocsag_filt_strobe), .sample(pocsag_filt_sample),
      .disc(fsk_disc_out), .disc_valid(fsk_disc_valid),
      .width_narrow(channel_width_narrow), .width_locked(channel_width_locked));

   // /////////////////////////////////////////////////////////////////////////////////
   //  POCSAG PHY: bit-timing recovery + batch/frame sync, straight off the
   //  fsk_demod discriminator. Reachable entirely through USER_SETTINGS
   //  (poke32(3*4,...) to configure/enable, peek64(2*8) to read back the
   //  latest codeword) -- see the pocsag_ctrl register above. BCH decode
   //  and message assembly are left to host software; this is PHY/framing
   //  only, same division of labor as fsk_demod.

   wire pocsag_sym_bit, pocsag_sym_valid;

   pocsag_bitsync pocsag_bitsync
     (.clk(radio_clk), .reset(radio_rst), .en(pocsag_en), .sps(pocsag_sps),
      .raw_bit(fsk_bit_out), .raw_bit_valid(fsk_bit_valid), .framer_locked(pocsag_locked),
      .sym_bit(pocsag_sym_bit), .sym_valid(pocsag_sym_valid));

   pocsag_framer pocsag_framer
     (.clk(radio_clk), .reset(radio_rst), .en(pocsag_en),
      .sym_bit(pocsag_sym_bit), .sym_valid(pocsag_sym_valid),
      .codeword(pocsag_codeword), .codeword_valid(pocsag_codeword_valid),
      .codeword_count(pocsag_codeword_count), .locked(pocsag_locked));

   // /////////////////////////////////////////////////////////////////////////////////
   //  GSC PHY: same fsk_demod bit stream as POCSAG above, a second
   //  bit-timing recovery + block-sync chain running concurrently in
   //  fabric (see gsc_framer.v for the framing writeup and why this
   //  project's own GSC convention resyncs on a comma before every block
   //  rather than tracking POCSAG-style fixed-length batches). Reuses
   //  pocsag_bitsync as-is -- it was already protocol-agnostic, nothing
   //  GSC-specific needed there, just its own sps/en (poke32(4*4,...),
   //  peek64(3*8) to read back -- see the gsc_ctrl register above).

   wire gsc_sym_bit, gsc_sym_valid;

   pocsag_bitsync gsc_bitsync
     (.clk(radio_clk), .reset(radio_rst), .en(gsc_en), .sps(gsc_sps),
      .raw_bit(fsk_bit_out), .raw_bit_valid(fsk_bit_valid), .framer_locked(gsc_locked),
      .sym_bit(gsc_sym_bit), .sym_valid(gsc_sym_valid));

   gsc_framer gsc_framer
     (.clk(radio_clk), .reset(radio_rst), .en(gsc_en),
      .sym_bit(gsc_sym_bit), .sym_valid(gsc_sym_valid),
      .word1(gsc_word1), .word2(gsc_word2),
      .pair_valid(gsc_pair_valid), .pair_count(gsc_pair_count), .locked(gsc_locked));

`ifdef DELETE_FORMAT_CONVERSION
   assign 	     rx_prefc_tdata_r = rx_tdata_i;
   assign 	     rx_prefc_tlast_r = rx_tlast_i;
   assign 	     rx_prefc_tvalid_r = rx_tvalid_i;
   assign 	     rx_tready_i = rx_prefc_tready_r;
`else
   chdr_16sc_to_xxxx_chain #(.BASE(SR_RX_FMT)) convert_16sc_to_xxxx
     (.clk(radio_clk), .reset(radio_rst),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
      .i_tdata(rx_tdata_i), .i_tlast(rx_tlast_i), .i_tvalid(rx_tvalid_i), .i_tready(rx_tready_i),
      .o_tdata(rx_prefc_tdata_r), .o_tlast(rx_prefc_tlast_r), .o_tvalid(rx_prefc_tvalid_r), .o_tready(rx_prefc_tready_r),
      .debug());
`endif
   // /////////////////////////////////////////////////////////////////////////////////
   //  RX Channel Muxing

   // The RX framer and converters can add up to 3 bubble cycles on the data path.
   // Delay any error packets so errors are back in band with the data packets.
   always @(posedge radio_clk) begin
      if (radio_rst) begin
         rx_err_delay_cnt <= 2'd0;
         rx_err_gate <= 0;
      end else begin
         if (rx_err_gate) begin
            if (rx_err_tvalid_r & rx_err_tready_r & rx_err_tlast_r) begin
               rx_err_delay_cnt <= 2'd0;
               rx_err_gate <= 0;
            end
         end else begin
            if (rx_postfc_tvalid_r) begin
               rx_err_delay_cnt <= 2'd0;
            end else if (rx_err_delay_cnt == 2'd3) begin
               rx_err_gate <= 1;
            end else if (rx_err_tvalid_r) begin
               rx_err_delay_cnt <= rx_err_delay_cnt + 2'd1;
            end
         end
      end
   end

   axi_mux4 #(.PRIO(1), .WIDTH(64), .BUFFER(1)) rx_mux
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .i0_tdata(rx_postfc_tdata_r), .i0_tlast(rx_postfc_tlast_r), .i0_tvalid(rx_postfc_tvalid_r), .i0_tready(rx_postfc_tready_r),
      .i1_tdata(rx_err_tdata_r), .i1_tlast(rx_err_gate & rx_err_tlast_r), .i1_tvalid(rx_err_gate & rx_err_tvalid_r), .i1_tready(rx_err_tready_r),
      .i2_tdata(64'h0), .i2_tlast(1'b0), .i2_tvalid(1'b0), .i2_tready(),
      .i3_tdata(64'h0), .i3_tlast(1'b0), .i3_tvalid(1'b0), .i3_tready(),
      .o_tdata(rx_tdata_r), .o_tlast(rx_tlast_r), .o_tvalid(rx_tvalid_r), .o_tready(rx_tready_r));

   // /////////////////////////////////////////////////////////////////////////////////
   //  Response Channel Muxing

   axi_mux4 #(.PRIO(0), .WIDTH(64)) response_mux
     (.clk(radio_clk), .reset(radio_rst), .clear(1'b0),
      .i0_tdata(txresp_tdata_r), .i0_tlast(txresp_tlast_r), .i0_tvalid(txresp_tvalid_r), .i0_tready(txresp_tready_r),
      .i1_tdata(resp_tdata_r), .i1_tlast(resp_tlast_r), .i1_tvalid(resp_tvalid_r), .i1_tready(resp_tready_r),
      .i2_tdata(64'h0), .i2_tlast(1'b0), .i2_tvalid(1'b0), .i2_tready(),
      .i3_tdata(64'h0), .i3_tlast(1'b0), .i3_tvalid(1'b0), .i3_tready(),
      .o_tdata(rmux_tdata_r), .o_tlast(rmux_tlast_r), .o_tvalid(rmux_tvalid_r), .o_tready(rmux_tready_r));




   /*******************************************************************
    * Debug only logic below here.
    ******************************************************************/
 assign debug = 0;

endmodule // radio_legacy
