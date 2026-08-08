# Copyright (c) 2026 The Regents of the University of California
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
Star topology: extends unified_sst.py by replacing the flat memHierarchy.Bus
fan-in with a single merlin.hr_router switch. Every gem5 node's
DirectoryController bridge (see network_common.create_node_directory_bridge)
terminates on its own port of the router; the shared MemController
terminates on the last port. Router radix = N (gem5 nodes) + 1 (memory).
See network_common.build_star() for the graph-construction details.

Usage (matches unified_run.py --network-topology=router):
    mpirun -np <N+1> -- bin/sst -v --add-lib-path=./ \\
        sst/unified_sst_router.py -- \\
        --output-directory=<dir> --jobs-path=<jobs.json> \\
        --clock=3GHz --systemd=false --memory-link-latency=50ns \\
        --uplink-latency=100ns
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import network_common as net

parser = net.build_arg_parser(
    "unified_sst + single Merlin router (star) between gem5 nodes and "
    "memory")
parser.add_argument(
    "--memory-link-latency",
    type=str,
    default="1ps",
    help="Latency of the link between the memory controller and the "
        "router (default: 1ps)",
)
parser.add_argument(
    "--uplink-latency",
    type=str,
    default="1ps",
    help="Latency of the gem5 <-> DirectoryController and "
        "DirectoryController <-> router links (default: 1ps). Required "
        "in practice -- see network_common._reject_default_link_latency().",
)
args = parser.parse_args()

if args.max_ticks is None:
    args.max_ticks = ""

jobs = net.load_jobs(args.jobs_path)

net.build_star(jobs, args.output_directory, args.clock, args.max_ticks,
                args.systemd, memory_link_latency=args.memory_link_latency,
                uplink_latency=args.uplink_latency)

net.enable_statistics(args.output_directory)
