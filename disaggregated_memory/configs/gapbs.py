# Copyright (c) 2021 The Regents of the University of California.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
Script to run GAPBS benchmarks with gem5. The script expects the
benchmark program and the simulation size to run. The input is in the format
<benchmark_prog> <size> <synthetic>
The system is fixed with 2 CPU cores, MESI Two Level system cache and 3 GiB
DDR4 memory. It uses the x86 board.

This script will count the total number of instructions executed
in the ROI. It also tracks how much wallclock and simulated time.

Usage:
------

```
scons build/X86/gem5.opt
./build/X86/gem5.opt \
    configs/example/gem5_library/x86-gabps-benchmarks.py \
    --benchmark <benchmark_name> \
    --synthetic <synthetic> \
    --size <simulation_size/graph_name>
```
"""
import os
import argparse
import sys
import time

# all the source files are one directory above.
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
)

import m5
from m5.objects import Root

from gem5.coherence_protocol import CoherenceProtocol
# from gem5.components.boards.x86_board import X86Board
from gem5.components.memory import SingleChannelDDR4_2400
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import *
from gem5.resources.workload import *
from gem5.resources.resource import obtain_resource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

from boards.x86_main_board import X86AlternateComposableMemoryBoard

from cachehierarchies.dm_caches import ClassicPrivateL1PrivateL2SharedL3DMCache

requires(
    isa_required=ISA.X86,
    kvm_required=True,
)

def QuadChannelDDR4_2400(size = None):
    """
    A dual channel memory system using DDR4_2400_8x8 based DIMM.
    """
    return ChanneledMemory(DDR4_2400_8x8, 4, 64, size=size)


parser = argparse.ArgumentParser(
    description="An example configuration script to run the gapbs benchmarks."
)

# The only positional argument accepted is the benchmark name in this script.

parser.add_argument(
    "--benchmark",
    type=str,
    required=True,
    help="Input the benchmark program to execute.",
    choices=["bc", "bfs", "cc", "cc_sv", "pr", "tc"],
)

args = parser.parse_args()
cache_hierarchy = ClassicPrivateL1PrivateL2SharedL3DMCache(
                        l1i_size="32KiB",
                        l1d_size="32KiB",
                        l2_size="512KiB",
                        l3_size="8MiB",
                        l3_assoc=16)

# Memory: Dual Channel DDR4 2400 DRAM device.
# The X86 board only supports 3 GiB of main memory.

local_memory = SingleChannelDDR4_2400(size="16GiB")
remote_memory = QuadChannelDDR4_2400(size="16GiB")

# Here we setup the processor. This is a special switchable processor in which
# a starting core type and a switch core type must be specified. Once a
# configuration is instantiated a user may call `processor.switch()` to switch
# from the starting core types to the switch core types. In this simulation
# we start with KVM cores to simulate the OS boot, then switch to the Timing
# cores for the command we wish to run after boot.

processor = SimpleSwitchableProcessor(
    starting_core_type=CPUTypes.KVM,
    switch_core_type=CPUTypes.O3,
    isa=ISA.X86,
    num_cores=8,
)

# Here we setup the board. The X86Board allows for Full-System X86 simulations

board = X86AlternateComposableMemoryBoard(
    clk_freq="4GHz",
    processor=processor,
    local_memory=local_memory,
    remote_memory=remote_memory,
    cache_hierarchy=cache_hierarchy,
)

# Here we set the FS workload, i.e., gapbs benchmark program
# After simulation has ended you may inspect
# `m5out/system.pc.com_1.device` to the stdout, if any.

# After the system boots, we execute the benchmark program and wait till the
# ROI `workbegin` annotation is reached. We start collecting the number of
# committed instructions till ROI ends (marked by `workend`). We then finish
# executing the rest of the benchmark.
cmd = ["echo '12345' | sudo -S ndctl create-namespace -f -enamespace0.0 -m devdax;",
       "echo \"exp: baseline :: " + args.benchmark + " !;\"\n",
       "echo '12345' | sudo -S /home/gem5/shared-gapbs/allocator -x 0 -S 16 -g 25;",
       "echo '12345' | sudo -S /home/gem5/shared-gapbs/" + args.benchmark + " -x 1 -S 16 -g 25;",
       "m5 exit;"
]

workload = CustomWorkload(
    function="set_kernel_disk_workload",
    parameters={
        "kernel": CustomResource("/home/kaustavg/kernel/x86/linux-6.9.9/vmlinux"),
        "disk_image": DiskImageResource("/home/kaustavg/projects/kg-resources-2/src/shared-gapbs/x86-disk-image-24-04/x86-ubuntu", root_partition="2"),
        "readfile_contents": " ".join(cmd),
    },
)
board.set_workload(workload)

board._pre_instantiate()
root = Root(full_system=True, board=board)
root.sim_quantum = int(1e9)
m5.instantiate()

def handle_workbegin():
    print("Done booting Linux")
    print("Resetting stats at the start of ROI!")
    m5.stats.reset()
    global start_tick
    start_tick = m5.curTick()
    print("config: switching cpus")
    processor.switch()
    yield False  # E.g., continue the simulation.


def handle_workend():
    print("Dump stats at the end of the ROI!")
    m5.stats.dump()
    print("config: finished simulation!")
    yield True  # Stop the simulation. We're done.


def on_exit():
    yield False

# We maintain the wall clock time.

globalStart = time.time()

print("Running the simulation")
print("Using KVM cpu")

# There are a few thihngs to note regarding the gapbs benchamrks. The first is
# that there are several ROI annotations in the code present in the disk image.
# These ROI begin and end calls are inside a loop. Therefore, we only simulate
# the first ROI annotation in details. The X86Board currently does not support
#  `work items started count reached`.

m5.simulate()
m5.simulate()
m5.simulate()
processor.switch()
print("switched cpu")
m5.stats.reset()
# Let's put everything to the test! 1B ticks to compare
m5.simulate(500_000_000_000)

# simulator.run()
# simulator.run()
end_tick = m5.curTick()
# Since we simulated the ROI in details, therefore, simulation is over at this
# point.

# Simulation is over at this point. We acknowledge that all the simulation
# events were successful.
print("All simulation events were successful.")

# We print the final simulation statistics.
print("Done with the simulation")
print()
print("Performance statistics:")

print(
    f"Simulated time in ROI: {(end_tick - start_tick) / 1000000000000.0:.2f}s"
)
print(
    "Ran a total of", simulator.get_current_tick() / 1e12, "simulated seconds"
)
print(
    "Total wallclock time: %.2fs, %.2f min"
    % (time.time() - globalStart, (time.time() - globalStart) / 60)
)


