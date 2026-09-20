#=============================================================================
#  create_project.tcl  --  build the radar simulation project from scratch
#
#  Usage (from inside this directory):
#      vivado -mode batch -source create_project.tcl        # build and exit
#      vivado -mode gui   -source create_project.tcl        # build and open the GUI
#
#  Re-running deletes the old project and rebuilds it, so it's always safe
#  to start over.
#  RTL source files are not copied into the project -- it references rtl/
#  and tb/ directly, so edits saved in your editor are picked up immediately
#  by Vivado.
#=============================================================================

set proj_name  radar_sim
set proj_dir   [file normalize [file dirname [info script]]]
set part       xc7z020clg400-2

puts "Project directory: $proj_dir"

#-----------------------------------------------------------------------------
# Remove any existing project
#-----------------------------------------------------------------------------
if {[file exists $proj_dir/$proj_name]} {
    puts "Deleting existing project..."
    file delete -force $proj_dir/$proj_name
}

create_project $proj_name $proj_dir/$proj_name -part $part
set_property target_language Verilog [current_project]

#-----------------------------------------------------------------------------
# RTL
#-----------------------------------------------------------------------------
add_files -fileset sources_1 $proj_dir/rtl/radar_dsp.sv
set_property file_type SystemVerilog [get_files $proj_dir/rtl/radar_dsp.sv]

#-----------------------------------------------------------------------------
# Sine lookup table
#
# Added to sources_1 so Vivado copies it into the simulation run directory,
# where $readmemh can find it.
#-----------------------------------------------------------------------------
if {![file exists $proj_dir/data/sin_lut.mem]} {
    puts "Error: data/sin_lut.mem is missing, run python py/gen_sin_lut.py first"
    exit 1
}
add_files -fileset sources_1 $proj_dir/data/sin_lut.mem

#-----------------------------------------------------------------------------
# Testbench
#-----------------------------------------------------------------------------
add_files -fileset sim_1 $proj_dir/tb/tb_radar_dsp.sv
set_property file_type SystemVerilog [get_files $proj_dir/tb/tb_radar_dsp.sv]
set_property top tb_radar_dsp [get_filesets sim_1]

#-----------------------------------------------------------------------------
# Simulation settings
#
# runtime all = run until $finish, no need to click Run All manually
#-----------------------------------------------------------------------------
set_property -name {xsim.simulate.runtime} -value {all} -objects [get_filesets sim_1]

update_compile_order -fileset sources_1
update_compile_order -fileset sim_1

puts ""
puts "=========================================="
puts " Project ready"
puts ""
puts " Run the simulation:  launch_simulation"
puts " Output:              $proj_name/$proj_name.sim/sim_1/behav/xsim/radar_out.txt"
puts "=========================================="
