// Five independent streaming window processors. Arithmetic is modulo 2**32.
module reset_sync(input wire clk, arst_n, output wire rst_n);
  (* async_reg = "true" *) reg [1:0] sync_q;
  always @(posedge clk or negedge arst_n)
    if (!arst_n) sync_q <= 0; else sync_q <= {sync_q[0], 1'b1};
  assign rst_n = sync_q[1];
endmodule

module clock_divider(input wire clk, rst_n, output wire div2, div4);
  reg [1:0] count;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) count <= 0; else count <= count + 1'b1;
  assign div2 = count[0];
  assign div4 = count[1];
endmodule

// One-entry bundled-data mailbox. Source holds payload until acknowledgement.
// Both endpoints must share the global reset epoch. No independent hot reset.
module mailbox(input wire src_clk, src_rst_n, input wire [31:0] src_data,
               input wire dst_clk, dst_rst_n,
               output reg [31:0] dst_data, output reg dst_valid);
  reg [31:0] payload;
  reg req, ack;
  (* async_reg = "true" *) reg ack_meta, ack_sync, req_meta, req_sync;
  always @(posedge src_clk or negedge src_rst_n)
    if (!src_rst_n) begin
      payload <= 0; req <= 0; ack_meta <= 0; ack_sync <= 0;
    end else begin
      ack_meta <= ack; ack_sync <= ack_meta;
      if (req == ack_sync) begin payload <= src_data; req <= ~req; end
    end
  always @(posedge dst_clk or negedge dst_rst_n)
    if (!dst_rst_n) begin
      req_meta <= 0; req_sync <= 0; ack <= 0; dst_data <= 0; dst_valid <= 0;
    end else begin
      req_meta <= req; req_sync <= req_meta; dst_valid <= 0;
      if (req_sync != ack) begin
        dst_data <= payload; dst_valid <= 1; ack <= req_sync;
      end
    end
endmodule

module window_engine #(parameter LANES=8, DEPTH=40)(
  input wire clk, rst_n, input wire [LANES*32-1:0] samples,
  output wire [LANES*32-1:0] sums, output wire valid,
  output reg [31:0] score);
  localparam CW = $clog2(DEPTH+1);
  reg [CW-1:0] filled;
  wire full = filled == CW'(DEPTH);
  assign valid = full;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) filled <= 0;
    else if (!full) filled <= filled + 1'b1;
  genvar l;
  generate for (l=0; l<LANES; l=l+1) begin: lane
    // Every history bit affects a later rolling sum. No keep/dont_touch padding.
    // Deliberately register-based sliding windows; no SRAM or FPGA shift macros.
    reg [31:0] history [0:DEPTH-1];
    reg [31:0] sum;
    integer d;
    always @(posedge clk) begin
      history[0] <= samples[l*32+:32];
      for (d=1; d<DEPTH; d=d+1) history[d] <= history[d-1];
    end
    always @(posedge clk or negedge rst_n)
      if (!rst_n) sum <= 0;
      else sum <= sum + samples[l*32+:32] - (full ? history[DEPTH-1] : 32'b0);
    assign sums[l*32+:32] = full ? sum : 32'b0;
  end endgenerate
  // A same-cycle reduction offers an RTL restructuring optimization target.
  reg [31:0] reduction;
  integer k;
  always @* begin
    reduction = 0;
    for (k=0; k<LANES; k=k+1) reduction = reduction + sums[k*32+:32];
  end
  always @(posedge clk or negedge rst_n)
    if (!rst_n) score <= 0; else score <= reduction;
endmodule

module benchmark #(parameter LANES=8, DEPTH=40)(
  input wire [4:0] clk, input wire arst_n,
  input wire [5*LANES*32-1:0] samples,
  output wire [5*LANES*32-1:0] sums,
  output wire [4:0] valid,
  output wire [159:0] slow_scores, received,
  output wire [4:0] received_valid,
  output wire [4:0] gen2, gen4);
  wire [4:0] master_rst_n, slow2_rst_n, slow4_rst_n;
  wire [159:0] scores;
  genvar i;
  generate for (i=0; i<5; i=i+1) begin: domain
    reset_sync master_reset(clk[i], arst_n, master_rst_n[i]);
    clock_divider divider(clk[i], master_rst_n[i], gen2[i], gen4[i]);
    reset_sync slow2_reset(gen2[i], arst_n, slow2_rst_n[i]);
    reset_sync slow4_reset(gen4[i], arst_n, slow4_rst_n[i]);
    window_engine #(.LANES(LANES), .DEPTH(DEPTH)) engine(
      clk[i], master_rst_n[i], samples[i*LANES*32+:LANES*32],
      sums[i*LANES*32+:LANES*32], valid[i], scores[i*32+:32]);
    reg [31:0] half_score, quarter_score;
    always @(posedge gen2[i] or negedge slow2_rst_n[i])
      if (!slow2_rst_n[i]) half_score <= 0; else half_score <= scores[i*32+:32];
    always @(posedge gen4[i] or negedge slow4_rst_n[i])
      if (!slow4_rst_n[i]) quarter_score <= 0; else quarter_score <= half_score;
    assign slow_scores[i*32+:32] = quarter_score;
    mailbox cdc(
      gen4[i], slow4_rst_n[i], quarter_score,
      clk[(i+1)%5], master_rst_n[(i+1)%5],
      received[((i+1)%5)*32+:32], received_valid[(i+1)%5]);
  end endgenerate
endmodule
