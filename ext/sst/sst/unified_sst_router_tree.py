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
Tree topology: gem5 nodes are split into leaf groups of at most --leaf-size
nodes (default 2, i.e. N gem5 nodes -> N/2 leaf routers for the case in
this file's own design notes below). Each leaf group's gem5 nodes connect
(via their DirectoryController bridges, see
network_common.create_node_directory_bridge) to their own merlin.hr_router
leaf switch; every leaf router -- the gem5 leaves AND a dedicated
single-host memory leaf -- uplinks to one root router. So gem5 traffic
crosses two router hops (leaf -> root) to reach memory, vs. one hop in
unified_sst_router.py's star.

Why memory sits behind its own dedicated leaf router instead of hanging
directly off the root (as a first pass at "N/2 leaf routers, all uplinked
to one root router, root router carries memory" might assume): merlin's
fattree topology (ext/sst/sst-elements/.../merlin/topology/fattree.cc,
topo_fattree::getPortState()) hardcodes every port of a level>0 router
(the root, here) to be R2R (router-to-router) -- there is no way to attach
a bare host endpoint directly to the root. Giving memory its own tiny
leaf keeps every endpoint at level 0, where fattree's addressing is
consistent. So the actual shape built here is:

    gem5_0, gem5_1 -> dirctrl -> leaf_router_0 --\\
    gem5_2, gem5_3 -> dirctrl -> leaf_router_1 ----+--> root_router
    ...                                           /         |
    gem5_{N-1} (etc, more leaves for larger N)  --/          |
                                                              v
                                          memory_leaf_router -> memory

Latency: every hop except the memory leaf <-> MemController one (that's
--memory-link-latency) uses --uplink-latency -- gem5 <-> dirctrl,
dirctrl <-> its leaf router, AND leaf <-> root. This deliberately does
NOT read the jobs JSON's per-node "remote-memory.latency" field: that
field is a single-hop value from unified_sst.py's bus topology (gem5
directly to a memHierarchy.Bus), and reusing it for the two hops here
that replace that one hop would silently double the latency the JSON
author actually asked for. See network_common.remote_memory_latency()'s
docstring for the same note from the other side.

Rank layout -- this is the part that differs from
network_common.build_tree() and is the reason this topology gets its own
copy of the graph-construction code instead of calling that function:

  network_common.build_tree() assumes N + 3 ranks are available
  (dirctrl_rank = N+1, memctrl_rank = N+2, on top of gem5 nodes on 1..N
  and routers on rank 0) -- IDs that historically didn't correspond to
  any MPI process unified_run.py actually started (it used to launch only
  `--count + 1`, the bus/star process count, for every topology). In
  practice this showed up as two gem5.gem5Component instances (each of
  which embeds its own process-global Python interpreter / gem5::Root()
  singleton -- see create_gem5_node()'s comment in network_common.py)
  landing in the same MPI rank/process, aborting with "Attempt to
  allocate multiple instances of Root."

  unified_run.py now launches `N + num_leaf_routers + 2` MPI ranks
  specifically for --network-topology=router-tree (see its
  `sst_processes` computation), and the rank layout here is built to use
  every one of them -- one rank per gem5-leaf domain, instead of folding
  everything onto a single shared rank:

    rank 0                      : memory domain (the dedicated memory
                                   leaf router + the shared MemController)
    rank 1..N                   : one gem5 node each (create_gem5_node(),
                                   unchanged -- this is why gem5 nodes
                                   can never collide: node+1 is unique
                                   and nothing else is ever assigned into
                                   this range)
    rank N+1..N+num_leaf_routers: one gem5-leaf domain each -- a leaf
                                   router *and* the DirectoryController
                                   bridges of the gem5 nodes under it
                                   share this rank (they only ever talk
                                   to each other and to their own gem5
                                   node/the root router, so keeping them
                                   in one process is natural and cheap)
    rank N+num_leaf_routers+1   : the root router

  This is contiguous (0..N+num_leaf_routers+1, no gaps), so none of the
  MPI ranks unified_run.py launches for router-tree sit idle.

Usage (matches unified_run.py --network-topology=router-tree):
    mpirun -np <N + ceil(N/leaf_size) + 2> -- bin/sst -v --add-lib-path=./ \\
        sst/unified_sst_router_tree.py -- \\
        --output-directory=<dir> --jobs-path=<jobs.json> \\
        --clock=3GHz --systemd=false --leaf-size=2 \\
        --memory-link-latency=50ns --uplink-latency=100ns
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import network_common as net


def build_tree(jobs, leaf_size, output_directory, cpu_clock_rate, max_ticks,
                systemd, link_bw="25.6GB/s", uplink_latency="1ps",
                memory_link_latency="1ps"):
    """Builds the 2-level Merlin router tree described in this file's
    module docstring, giving every gem5-leaf domain (a leaf router + its
    gem5 nodes' DirectoryController bridges), the root router, and the
    memory domain (the dedicated memory leaf router + the MemController)
    their own MPI rank -- see the "Rank layout" section above for the
    exact scheme and why it has to match unified_run.py's process count.
    """
    num_gem5_nodes = len(jobs)
    if num_gem5_nodes == 0:
        print("fatal: jobs file has no gem5 nodes!")
        exit(-1)

    net.require_non_shared_memory(jobs)
    net.validate_disjoint_address_ranges(jobs)

    num_gem5_leaves = (num_gem5_nodes + leaf_size - 1) // leaf_size
    total_leaves = num_gem5_leaves + 1  # +1 dedicated memory leaf
    memory_leaf_id = num_gem5_leaves    # last leaf slot

    # See the "Rank layout" section in the module docstring.
    memory_rank = 0
    root_rank = num_gem5_nodes + num_gem5_leaves + 1

    def gem5_leaf_rank(leaf):
        return num_gem5_nodes + 1 + leaf

    print(f"info: building a 2-level Merlin router tree for "
          f"{num_gem5_nodes} gem5 node(s): {num_gem5_leaves} gem5 leaf "
          f"router(s) (leaf-size={leaf_size}) + 1 memory leaf, all "
          f"uplinked to 1 root router. Using "
          f"{num_gem5_nodes + num_gem5_leaves + 2} MPI ranks (matching "
          f"unified_run.py's mpirun -np): memory domain=rank{memory_rank}, "
          f"gem5 nodes=rank1..{num_gem5_nodes}, gem5-leaf domains=rank"
          f"{num_gem5_nodes + 1}..{num_gem5_nodes + num_gem5_leaves}, "
          f"root router=rank{root_rank}.")

    # merlin.fattree shape: "<down,up>:<down,up>:..." from leaves to root.
    #   level 0 (leaves): leaf_size down-ports (hosts), 1 up-port (root)
    #   level 1 (root):   total_leaves down-ports (leaves), 0 up-ports
    shape = f"{leaf_size},1:{total_leaves},0"
    topo_params = {"shape": shape, "routing_alg": "deterministic"}

    total_sst_memory = net.compute_total_sst_memory(jobs)

    leaf_routers = []
    for leaf in range(total_leaves):
        leaf_rank = memory_rank if leaf == memory_leaf_id \
            else gem5_leaf_rank(leaf)
        leaf_routers.append(net.create_router(
            f"leaf_router_{leaf}", leaf_size + 1, leaf,
            topology="merlin.fattree", topology_params=dict(topo_params),
            link_bw=link_bw, rank=leaf_rank))

    root_router = net.create_router(
        "root_router", total_leaves, total_leaves,
        topology="merlin.fattree", topology_params=dict(topo_params),
        link_bw=link_bw, rank=root_rank)

    for leaf in range(total_leaves):
        net.link(f"leaf_{leaf}_to_root",
                 leaf_routers[leaf], f"port{leaf_size}",
                 root_router, f"port{leaf}",
                 latency=uplink_latency)

    for node in range(num_gem5_nodes):
        job = str(node)
        leaf = node // leaf_size
        port_on_leaf = node % leaf_size

        _gem5_node, memory_port = net.create_gem5_node(
            node, jobs, output_directory, cpu_clock_rate, max_ticks,
            systemd)

        dirctrl, dirctrl_net = net.create_node_directory_bridge(
            f"dirctrl_{node}",
            int(jobs[job]["remote-memory"]["start"], 16),
            int(jobs[job]["remote-memory"]["end"], 16) - 1,
            group=1, rank=gem5_leaf_rank(leaf))

        # Both hops use --uplink-latency, not the jobs JSON's per-node
        # "remote-memory.latency" -- see the "Latency" paragraph in the
        # module docstring.
        net.link(f"gem5_{node}_to_dirctrl", memory_port, "port",
                 dirctrl, "highlink", latency=uplink_latency)
        net.link(f"dirctrl_{node}_to_leaf{leaf}", dirctrl_net, "port",
                 leaf_routers[leaf], f"port{port_on_leaf}",
                 latency=uplink_latency)

    memctrl, memctrl_net = net.create_memory_bridge(
        "memory", total_sst_memory, group=2, rank=memory_rank)
    net.link("memory_to_memory_leaf", memctrl_net, "port",
             leaf_routers[memory_leaf_id], "port0",
             latency=memory_link_latency)


parser = net.build_arg_parser(
    "unified_sst + 2-level Merlin router tree (<= --leaf-size gem5 nodes "
    "per leaf) between gem5 nodes and memory")
parser.add_argument(
    "--leaf-size",
    type=int,
    default=2,
    help="Maximum number of gem5 nodes served by each leaf router " +
        "(default: 2)",
)
parser.add_argument(
    "--uplink-latency",
    type=str,
    default="1ps",
    help="Latency of every non-memory link: gem5 <-> DirectoryController, "
        "DirectoryController <-> its leaf router, and leaf-router <-> "
        "root-router. Does NOT come from the jobs JSON's per-node "
        "remote-memory.latency -- see the module docstring's Latency "
        "paragraph. Required in practice -- see "
        "network_common._reject_default_link_latency() (default: 1ps).",
)
parser.add_argument(
    "--memory-link-latency",
    type=str,
    default="1ps",
    help="Latency of the link between the memory controller and its "
        "dedicated memory leaf router (default: 1ps)",
)
args = parser.parse_args()

if args.max_ticks is None:
    args.max_ticks = ""

if args.leaf_size < 1:
    print("fatal: --leaf-size must be >= 1")
    exit(-1)

jobs = net.load_jobs(args.jobs_path)

build_tree(jobs, args.leaf_size, args.output_directory, args.clock,
           args.max_ticks, args.systemd,
           uplink_latency=args.uplink_latency,
           memory_link_latency=args.memory_link_latency)

net.enable_statistics(args.output_directory)
