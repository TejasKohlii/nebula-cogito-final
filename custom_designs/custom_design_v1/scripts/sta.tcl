read_liberty $::env(LIBERTY)
read_verilog build/benchmark_mapped.v
link_design benchmark
read_sdc constraints/benchmark.sdc
if {[llength [all_clocks]] != 15} { error "Expected 15 active clocks" }
if {![check_setup -no_clock -multiple_clock -loops -generated_clocks -verbose]} {
    error "Clock or topology checks failed"
}
check_setup -verbose
report_clock_properties [all_clocks]
report_checks -path_delay max -group_path_count 10 -fields {slew cap fanout} -digits 4
report_wns
report_tns
# Pre-layout ideal clocks and no extracted wire parasitics. Not signoff PPA.
source scripts/endpoint_slacks.tcl
