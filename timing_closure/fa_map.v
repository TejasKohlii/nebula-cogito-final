// Exact Nangate45 full-adder binding for Yosys arithmetic compression.
// Library CO=(A&B)|(CI&(A|B)); S=CI^(A^B). No state or latency change.
(* techmap_celltype = "$fa" *)
module _80_nangate_fa(A, B, C, X, Y);
  parameter WIDTH = 1;
  parameter [WIDTH-1:0] _TECHMAP_CONSTMSK_A_ = 0;
  parameter [WIDTH-1:0] _TECHMAP_CONSTMSK_B_ = 0;
  parameter [WIDTH-1:0] _TECHMAP_CONSTMSK_C_ = 0;
  input [WIDTH-1:0] A, B, C;
  output [WIDTH-1:0] X, Y;
  genvar i;
  generate for (i=0; i<WIDTH; i=i+1) begin: bits
    if (_TECHMAP_CONSTMSK_A_[i] || _TECHMAP_CONSTMSK_B_[i] || _TECHMAP_CONSTMSK_C_[i]) begin
      assign Y[i]=A[i]^B[i]^C[i];
      assign X[i]=(A[i]&B[i])|(C[i]&(A[i]|B[i]));
    end else begin
      FA_X1 cell_fa(.A(A[i]), .B(B[i]), .CI(C[i]), .CO(X[i]), .S(Y[i]));
    end
  end endgenerate
endmodule
