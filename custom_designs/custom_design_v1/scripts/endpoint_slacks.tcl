# Enumerate worst max-delay path per endpoint, with a generous per-group cap.
set paths [find_timing_paths -path_delay max -slack_max 0 -group_path_count 100000 -endpoint_path_count 1]
set endpoint_report reports/violating_endpoints.tsv
if {[info exists ::env(ENDPOINT_REPORT)]} {set endpoint_report $::env(ENDPOINT_REPORT)}
set fp [open $endpoint_report w]
puts $fp "endpoint\tslack_ns"
foreach path $paths {
    puts $fp "[get_full_name [get_property $path endpoint]]\t[get_property $path slack]"
}
close $fp
