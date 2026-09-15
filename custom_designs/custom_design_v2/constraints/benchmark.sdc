# Units: ns, fF (Nangate45 typical Liberty). External ports are synchronous to
# their corresponding domain. No asynchronous-clock-wide false paths: those
# would suppress bundled-data max-delay checks as well.
source build/generated_clock_pins.tcl
set periods {1.000 0.833333333 1.250 1.538461538 2.000}
for {set i 0} {$i < 5} {incr i} {
    set cp [get_ports [format {clk[%d]} $i]]
    set p [lindex $periods $i]
    create_clock -name m$i -period $p $cp
    create_generated_clock -name g${i}_2 -source $cp -divide_by 2 \
        [get_pins $generated_clock_pins($i,2)]
    # Counter bit 1 rises on the second master edge: preserve /2-to-/4 phase.
    create_generated_clock -name g${i}_4 -source $cp -edges {3 7 11} \
        [get_pins $generated_clock_pins($i,4)]
    foreach ratio {2 4} {
        set_output_delay -clock g${i}_${ratio} -max 0 [get_ports [format {gen%d[%d]} $ratio $i]]
        set_output_delay -clock g${i}_${ratio} -min 0 [get_ports [format {gen%d[%d]} $ratio $i]]
    }
    set_clock_uncertainty 0.04 [get_clocks [list m$i g${i}_2 g${i}_4]]
    set_clock_transition 0.03 [get_clocks [list m$i g${i}_2 g${i}_4]]
    for {set b 0} {$b < 256} {incr b} {
        set idx [expr {$i*256+$b}]
        set_input_delay -clock m$i -max [expr {0.15*$p}] [get_ports [format {samples[%d]} $idx]]
        set_input_delay -clock m$i -min 0.02 [get_ports [format {samples[%d]} $idx]]
        set_output_delay -clock m$i -max [expr {0.15*$p}] [get_ports [format {sums[%d]} $idx]]
        set_output_delay -clock m$i -min 0.02 [get_ports [format {sums[%d]} $idx]]
    }
    foreach name {valid received_valid} {
        set_output_delay -clock m$i -max [expr {0.15*$p}] [get_ports [format {%s[%d]} $name $i]]
        set_output_delay -clock m$i -min 0.02 [get_ports [format {%s[%d]} $name $i]]
    }
    for {set b 0} {$b < 32} {incr b} {
        set idx [expr {$i*32+$b}]
        foreach {name clockname} [list received m$i slow_scores g${i}_4] {
            set_output_delay -clock $clockname -max [expr {0.15*$p}] [get_ports [format {%s[%d]} $name $idx]]
            set_output_delay -clock $clockname -min 0.02 [get_ports [format {%s[%d]} $name $idx]]
        }
    }
}
set_input_transition 0.03 [get_ports {samples*}]
set_load 5.0 [all_outputs]
# Reset assertion is asynchronous; deassertion is synchronized in every domain.
# Bookkeeping reference only; the exception below excludes reset timing.
set_input_delay -clock m0 -min 0 [get_ports arst_n]
set_input_delay -clock m0 -max 0 [get_ports arst_n]
set_false_path -from [get_ports arst_n]
# Cell D/Q pin lists are generated from mapped JSON: no fragile synthesized IDs.
source build/cdc_constraints.tcl

