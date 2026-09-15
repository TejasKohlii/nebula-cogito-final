`timescale 1ps/1ps
module tb;
  localparam LANES=8, DEPTH=40;
  reg [4:0] clk=0;
  reg arst_n=0;
  reg [1279:0] samples=0;
  wire [1279:0] sums;
  wire [4:0] valid, rv, gen2, gen4;
  wire [159:0] slow_scores, received;
  benchmark dut(clk, arst_n, samples, sums, valid, slow_scores, received, rv, gen2, gen4);
  // Independent phases and mutually different periods, including 1.0/1.2 GHz.
  always #500 clk[0]=~clk[0];
  initial begin #137; forever #417 clk[1]=~clk[1]; end
  initial begin #211; forever #625 clk[2]=~clk[2]; end
  initial begin #307; forever #769 clk[3]=~clk[3]; end
  initial begin #419; forever #1000 clk[4]=~clk[4]; end
  integer transfers[5], checks[5], div2_edges[5], div4_edges[5];
  genvar g;
  generate for(g=0;g<5;g=g+1) begin: model
    reg [31:0] hist[LANES][DEPTH];
    reg [31:0] expected[LANES];
    reg [31:0] rng = 32'h12345678 + g;
    integer n=0;
    reg [31:0] launched[$];
    reg [31:0] want;
    // Snapshot exactly when the mailbox accepts a source sample.
    always @(posedge gen4[g]) begin
      if (dut.slow4_rst_n[g] &&
          dut.domain[g].cdc.req == dut.domain[g].cdc.ack_sync)
        launched.push_back(slow_scores[g*32+:32]);
      div4_edges[g]++;
    end
    always @(posedge gen2[g]) div2_edges[g]++;
    always @(negedge arst_n) launched.delete();
    always @(posedge clk[(g+1)%5]) begin
      #2;
      if (arst_n && rv[(g+1)%5]) begin
        assert(launched.size()>0) else $fatal(1,"Unexpected CDC transfer %0d",g);
        want=launched.pop_front();
        assert(received[((g+1)%5)*32+:32]===want)
          else $fatal(1,"CDC corruption/order error %0d",g);
        transfers[g]++;
      end
    end
    always @(negedge clk[g]) begin
      for(integer l=0;l<LANES;l++) begin
        rng=rng^(rng<<13); rng=rng^(rng>>17); rng=rng^(rng<<5);
        samples[(g*LANES+l)*32+:32]=rng;
      end
    end
    always @(posedge clk[g]) begin
      if (!dut.master_rst_n[g]) begin
        n=0;
        for(integer l=0;l<LANES;l++) begin
          expected[l]=0;
          for(integer d=0;d<DEPTH;d++) hist[l][d]=0;
        end
      end else begin
        for(integer l=0;l<LANES;l++) begin
          expected[l]=expected[l]+samples[(g*LANES+l)*32+:32]-hist[l][DEPTH-1];
          for(integer d=DEPTH-1;d>0;d--) hist[l][d]=hist[l][d-1];
          hist[l][0]=samples[(g*LANES+l)*32+:32];
        end
        n++;
        #1;
        assert(valid[g] == (n>=DEPTH)) else $fatal(1,"valid mismatch %0d",g);
        for(integer l=0;l<LANES;l++)
          assert(sums[(g*LANES+l)*32+:32] === (n>=DEPTH ? expected[l] : 32'b0))
            else $fatal(1,"sum mismatch domain=%0d lane=%0d cycle=%0d",g,l,n);
        checks[g]++;
      end
    end
  end endgenerate
  initial begin
    #12345; arst_n=1;
    #700000; arst_n=0;
    #13279; arst_n=1;
    #900000;
    for(integer i=0;i<5;i++) begin
      assert(checks[i]>600 && transfers[i]>30) else $fatal(1,"insufficient activity");
      assert(div2_edges[i]>2*div4_edges[i]-4 && div2_edges[i]<2*div4_edges[i]+4)
        else $fatal(1,"divider ratio mismatch");
      $display("domain %0d: %0d window checks, %0d CDC transfers",i,checks[i],transfers[i]);
    end
    $display("PASS: full-size rolling windows, generated clocks, CDC ordering, reset recovery");
    $finish;
  end
  initial begin #2000000; $fatal(1,"timeout"); end
endmodule
