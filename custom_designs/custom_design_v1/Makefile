SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
STA ?= $(if $(wildcard build/tools/sta),build/tools/sta,sta)
LIB ?= ../../third_party/nangate45/NangateOpenCellLibrary_typical.lib
.PHONY: lint test synth sta
lint:
	verilator --lint-only -Wall --Wno-DECLFILENAME --top-module benchmark rtl/benchmark.sv
test:
	mkdir -p build reports
	verilator --binary --timing --assert -Wno-TIMESCALEMOD --top-module tb --Mdir build/obj rtl/benchmark.sv tests/tb.sv > reports/verilator_build.log 2>&1
	build/obj/Vtb | tee reports/simulation.log
synth:
	python3 scripts/synth.py --lib "$(LIB)"
sta:
	python3 scripts/run_sta.py --sta "$(STA)" --lib "$(LIB)"
