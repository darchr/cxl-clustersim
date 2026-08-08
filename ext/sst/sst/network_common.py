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
Shared helpers for the Merlin-router-based extensions of unified_sst.py:

    unified_sst_router.py       -- star topology: every gem5 node's
                                    DirectoryController bridge terminates
                                    directly on one merlin.hr_router, which
                                    also terminates the shared MemController.
    unified_sst_router_tree.py  -- two-level tree: gem5 nodes are split
                                    into leaf groups of at most
                                    --leaf-size nodes, each leaf's router
                                    uplinks to a single root router, which
                                    also carries a dedicated memory leaf.

Design notes (why DirectoryController + MemNIC, and why fattree, not
singlerouter, for the tree):

  gem5's SST bridge (gem5.gem5Bridge, the "remote_memory_port"
  SubComponent) speaks a *plain* point-to-point memHierarchy.MemEventBase
  protocol -- there's no way to plug it directly into a merlin.hr_router
  port, which expects "MemRtrEvent" packets. memHierarchy.MemNIC is the
  standard translator between the two: it is loaded as a highlink/lowlink
  SubComponent slot on a Cache, DirectoryController, or MemController, and
  its "port" connects straight to a merlin.hr_router port.
  DirectoryController is used here purely as a lightweight per-node bridge
  (plain link up to one gem5 node, MemNIC down to the network) -- there is
  no real cache-coherence sharing happening since every gem5 node owns a
  disjoint physical address range.

  merlin.hr_router's default topology, "merlin.singlerouter", implements
  routing as `next_port = destination_id`: it assumes it IS the entire
  network, so chaining two singlerouter routers together would silently
  misroute anything not destined for one of their own directly attached
  endpoints. A genuine router-to-router hierarchy (the tree script) needs
  a topology that understands multiple hops, so unified_sst_router_tree.py
  uses "merlin.fattree" instead.
"""

import os

import sst
from sst import UnitAlgebra

CACHE_LINK_LATENCY = "1ps"

# Set SST_DIRCTRL_DEBUG=1 in the environment before running to enable
# memHierarchy's own verbose event tracing on every DirectoryController,
# its MemNIC, and the shared MemController (output goes to stdout, so it's
# captured wherever the run's normal output already goes). Useful for
# diagnosing routing/coherence-handshake issues between gem5's bridge and
# DirectoryController's "incoherentSrc" non-coherent-client handling.
# debug_level requires sst-core to have been built with --enable-debug to
# have any effect; harmless to set otherwise.
_DIRCTRL_DEBUG = os.environ.get("SST_DIRCTRL_DEBUG", "") not in (
    "", "0", "false", "False")


def build_arg_parser(description=""):
    """Same CLI contract as unified_sst.py so this drops in as a
    replacement --gem5-config-style script for the unified_run.py launcher.
    """
    import argparse

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--output-directory",
        type=str,
        required=True,
        help="Output directory where gem5 has stored all the stats and " +
            "checkpoints",
    )
    parser.add_argument(
        "--jobs-path",
        type=str,
        required=True,
        help="The JSON of gem5 jobs will be sent over to SST for simplicity.",
    )
    parser.add_argument(
        "--clock",
        type=str,
        required=True,
        help="Specify the core frequency for all the processes",
    )
    parser.add_argument(
        "--systemd",
        type=str,
        required=True,
        choices=["true", "false"],
        help="SST needs to know if systemd is enabled to call the gem5-side"
            " script",
    )
    parser.add_argument(
        "--max-ticks",
        type=str,
        required=False,
        help="Tell SST how long to run. SST passes this to gem5.",
    )
    return parser


def load_jobs(jobs_path):
    import json

    with open(jobs_path) as f:
        return json.loads(f.read())


def util_int_to_size(size_in_bytes: int) -> str:
    return str(size_in_bytes / 1024 / 1024 / 1024) + "GiB"


def remote_memory_latency(jobs, job: str, default: str = CACHE_LINK_LATENCY) -> str:
    """Per-node link latency, read from jobs[job]["remote-memory"]["latency"]
    (the same field unified_sst.py's bus topology uses for the gem5 <-> SST-
    memory hop). Falls back to `default` if a job (e.g. one produced by
    unified_run.py's --count auto-joblist path, rather than a hand-written
    --joblist) doesn't set it.

    NOT used by the Merlin router topologies (build_star() /
    unified_sst_router_tree.py's build_tree()) any more: those replace
    unified_sst.py's single gem5 <-> memory hop with two hops (gem5 <->
    dirctrl, dirctrl <-> router), and applying this same per-node JSON
    value to both would double-count it. They use --uplink-latency for
    both of those hops instead, so this stays purely a bus-topology
    helper (kept here since it's still a live default for `remote-memory
    ["latency"] not set`).
    """
    return jobs[job]["remote-memory"].get("latency", default)


def require_non_shared_memory(jobs):
    """Every DirectoryController bridge here owns a disjoint physical
    address range (its addr_range_start/end must be unique). A "shared"
    remote-memory region (multiple nodes mapped to the *same* range, as
    used by unified_sst_bi.py for CXL.mem-style shared memory) would
    create multiple DirectoryControllers claiming the same range, which
    isn't supported here. Fail fast instead of silently routing wrong.
    """
    for job in jobs:
        if jobs[job]["remote-memory"]["shared"].lower() == "true":
            print("fatal: the Merlin router topologies do not support "
                  "shared remote-memory regions (remote-memory.shared="
                  "true). Use --network-topology=bus (unified_sst_bi.py) "
                  "for shared/CXL.mem workloads, or mark every job's "
                  "remote-memory.shared as \"false\".")
            exit(-1)


def validate_disjoint_address_ranges(jobs):
    """Sanity-checks that every job's remote-memory range is disjoint from
    every other job's. Each DirectoryController bridge (see
    create_node_directory_bridge) is configured to exclusively own exactly
    one node's range -- if a joblist ever assigned two nodes overlapping
    ranges, both DirectoryControllers would claim some of the same
    addresses, and responses could plausibly get delivered to the wrong
    node. Fail fast with the actual offending ranges here instead of
    debugging a routing/coherence error at runtime.
    """
    ranges = []
    for job in jobs:
        start = int(jobs[job]["remote-memory"]["start"], 16)
        end = int(jobs[job]["remote-memory"]["end"], 16)
        if start >= end:
            print(f"fatal: job {job} has an empty or inverted "
                  f"remote-memory range: start=0x{start:x} end=0x{end:x}")
            exit(-1)
        ranges.append((start, end, job))

    ranges.sort()
    for i in range(1, len(ranges)):
        prev_start, prev_end, prev_job = ranges[i - 1]
        start, end, job = ranges[i]
        if start < prev_end:
            print(f"fatal: remote-memory ranges for jobs {prev_job} "
                  f"(0x{prev_start:x}-0x{prev_end:x}) and {job} "
                  f"(0x{start:x}-0x{end:x}) overlap. Every "
                  "DirectoryController bridge must own a disjoint range.")
            exit(-1)


def compute_total_sst_memory(jobs) -> int:
    """The size (in bytes, from address 0) that the shared MemController
    must be able to address to cover every node's remote-memory window.
    Mirrors unified_sst.py's total memory computation exactly, since the
    checkpoints gem5 restores from were taken against that same address
    layout (including the "blank" I/O-hole space below the first node's
    remote-memory start).
    """
    blank_memory_space = jobs["0"]["remote-memory"]["start"]
    total_sst_memory = int(blank_memory_space, 16)
    for job in jobs:
        total_sst_memory = total_sst_memory + \
            int(jobs[job]["remote-memory"]["end"], 16) - \
            int(jobs[job]["remote-memory"]["start"], 16)
        if jobs[job]["remote-memory"]["shared"] == "true":
            break
    return total_sst_memory


def create_gem5_node(node, jobs, output_directory, cpu_clock_rate,
                      max_ticks, systemd,
                      response_receiver_name=
                          "board.remote_memory.outgoing_request_bridge"):
    """Builds one gem5.gem5Component + its gem5.gem5Bridge SubComponent.
    Same per-node construction as unified_sst.py, so checkpoint restore
    (--checkpoints=False, gem5's -re-equivalent path via m5.instantiate())
    behaves identically regardless of which network topology script is
    driving it.

    @returns (gem5_component, memory_port_subcomponent)
    """
    job = str(node)

    p_config = os.path.join(os.getcwd(), "../../")
    if not os.path.exists(os.path.join(p_config, "disaggregated_memory")):
        print("fatal: Please run this script from the top gem5 directory.")
        exit(-1)

    isa = jobs[job]["cpu"]["isa"].lower()
    if isa == "arm":
        p_config = os.path.join(
            os.getcwd(), "../../disaggregated_memory/configs/arm_unified.py")
    elif isa == "riscv":
        p_config = os.path.join(
            os.getcwd(),
            "../../disaggregated_memory/configs/riscv_unified.py")
    elif isa == "x86":
        p_config = os.path.join(
            os.getcwd(), "../../disaggregated_memory/configs/x86_unified.py")
    else:
        print("fatal: Unsupported ISA!")
        exit(-1)

    cmd = [
        "--outdir=" + os.path.join(
            output_directory, jobs[job]["metadata"]["experiment"]) +
            "_" + str(node),
        p_config,
    ]

    max_ticks_val = 0
    max_insts_val = 0
    try:
        if jobs[job]["metadata"]["maxtics"] != "":
            max_ticks_val = int(jobs[job]["metadata"]["maxtics"])
    except (KeyError, ValueError):
        max_ticks_val = 0
    try:
        if jobs[job]["metadata"]["maxinsts"] != "":
            max_insts_val = int(jobs[job]["metadata"]["maxinsts"])
    except (KeyError, ValueError):
        max_insts_val = 0
    if max_insts_val == max_ticks_val and max_insts_val != 0:
        print("fatal: cannot simulate with both max insts and max tics!")
        exit(-1)

    rest_of_cmd = [
        "--instance=" + job,
        "--ff-core-type=" + jobs[job]["cpu"]["ff-core"],
        "--roi-core-type=" + jobs[job]["cpu"]["roi-core"],
        "--core-count=" + jobs[job]["cpu"]["count"],
        "--core-frequency=" + cpu_clock_rate,
        "--cache-type=" + jobs[job]["cache"]["type"],
        "--l1i-size=" + jobs[job]["cache"]["l1i-size"],
        "--l1d-size=" + jobs[job]["cache"]["l1d-size"],
        "--l2-size=" + jobs[job]["cache"]["l2-size"],
        "--l3-size=" + jobs[job]["cache"]["l3-size"],
        "--l3-assoc=" + jobs[job]["cache"]["l3-assoc"],
        "--local-memory-type=" + jobs[job]["local-memory"]["type"],
        "--local-memory-size=" + jobs[job]["local-memory"]["size"],
        "--remote-memory-shared=" +
            jobs[job]["remote-memory"]["shared"].lower(),
        "--remote-memory-start=" + str(jobs[job]["remote-memory"]["start"]),
        "--remote-memory-end=" + str(jobs[job]["remote-memory"]["end"]),
        "--is-composable=true",
        "--cmd=\"\"",
        "--disk-path=" + jobs[job]["workitem"]["disk"],
        "--kernel-path=" + jobs[job]["workitem"]["kernel"],
        "--bootloader-path=" + jobs[job]["workitem"]["bootloader"],
        "--systemd=" + systemd,
    ]
    cmd = cmd + rest_of_cmd

    port_list = ["remote_memory_port"]
    cpu_params = {
        "frequency": cpu_clock_rate,
        "cmd": " ".join(cmd),
        "debug_flags": "",
        "ports": " ".join(port_list),
        "max_ticks": max_ticks,
    }

    gem5_node = sst.Component("gem5_node_{}".format(node), "gem5.gem5Component")
    gem5_node.addParams(cpu_params)
    # Each gem5 node gets its own rank (ranks 1..N here). gem5's Root and
    # its embedded Python interpreter/event queue are process-global --
    # two gem5Components sharing a rank means two Root() allocations in
    # one process, which aborts ("Attempt to allocate multiple instances
    # of Root."). See build_star()/build_tree() for how ranks N+1..N+3 are
    # reserved for the router(s)/dirctrl(s)/memctrl, each on their own
    # rank and never sharing with a gem5 node's rank either.
    gem5_node.setRank(node + 1, 0)

    memory_port = gem5_node.setSubComponent(
        "remote_memory_port", "gem5.gem5Bridge", 0)
    memory_port.addParams({"response_receiver_name": response_receiver_name})

    return gem5_node, memory_port


def create_router(name, num_ports, rtr_id, topology="merlin.singlerouter",
                   topology_params=None, link_bw="25.6GB/s",
                   xbar_bw="51.2GB/s", input_buf_size="2KB",
                   output_buf_size="2KB", flit_size="64B", rank=0):
    """Creates one merlin.hr_router with the given topology SubComponent."""
    router = sst.Component(name, "merlin.hr_router")
    router.addParams({
        "num_ports": num_ports,
        "id": rtr_id,
        "link_bw": link_bw,
        "xbar_bw": xbar_bw,
        "input_buf_size": input_buf_size,
        "output_buf_size": output_buf_size,
        "flit_size": flit_size,
    })
    router.setRank(rank, 0)
    topo = router.setSubComponent("topology", topology)
    if topology_params:
        topo.addParams(topology_params)
    return router


def create_node_directory_bridge(name, addr_range_start, addr_range_end,
                                  network_bw="25GB/s", group=1,
                                  entry_cache_size=1024,
                                  mshr_num_entries=64, rank=0):
    """A lightweight, non-coherent DirectoryController used purely as a
    protocol bridge for one gem5 node's remote-memory address range:
    "highlink" (up, towards the gem5 node) stays a plain link so the
    gem5.gem5Bridge SubComponent can connect directly; "lowlink" (down,
    towards memory) is a MemNIC so this bridge can attach to any
    merlin.hr_router port.
    """
    dirctrl = sst.Component(name, "memHierarchy.DirectoryController")
    dirctrl_params = {
        "clock": "2GHz",
        "coherence_protocol": "MESI",
        "entry_cache_size": entry_cache_size,
        "mshr_num_entries": mshr_num_entries,
        "addr_range_start": addr_range_start,
        "addr_range_end": addr_range_end,
    }
    if _DIRCTRL_DEBUG:
        dirctrl_params["debug"] = "1"
        dirctrl_params["debug_level"] = 10
        dirctrl_params["verbose"] = 2
    dirctrl.addParams(dirctrl_params)
    dirctrl.setRank(rank, 0)
    lowlink_nic = dirctrl.setSubComponent("lowlink", "memHierarchy.MemNIC")
    nic_params = {"group": group, "network_bw": network_bw}
    if _DIRCTRL_DEBUG:
        nic_params["debug"] = "1"
        nic_params["debug_level"] = 10
    lowlink_nic.addParams(nic_params)
    return dirctrl, lowlink_nic


def create_memory_bridge(name, total_mem_bytes, network_bw="25GB/s",
                          group=2, mem_clock="1.2GHz", rank=0):
    """The single shared MemController for the whole disaggregated memory
    pool. Its "highlink" is a MemNIC so it can be reached from any router
    in the fabric, no matter how many hops away the requester is.
    """
    memctrl = sst.Component(name, "memHierarchy.MemController")
    memctrl_params = {
        "debug": "1" if _DIRCTRL_DEBUG else "0",
        "clock": mem_clock,
        "request_width": "64",
        "addr_range_start": 0x0,
        "addr_range_end": UnitAlgebra(
            util_int_to_size(total_mem_bytes)).getRoundedValue(),
    }
    if _DIRCTRL_DEBUG:
        memctrl_params["debug_level"] = 10
    memctrl.addParams(memctrl_params)
    memctrl.setRank(rank, 0)

    highlink_nic = memctrl.setSubComponent("highlink", "memHierarchy.MemNIC")
    nic_params = {"group": group, "network_bw": network_bw}
    if _DIRCTRL_DEBUG:
        nic_params["debug"] = "1"
        nic_params["debug_level"] = 10
    highlink_nic.addParams(nic_params)

    backend = memctrl.setSubComponent("backend", "memHierarchy.timingDRAM")
    backend.addParams({
        "id": 0,
        "addrMapper": "memHierarchy.simpleAddrMapper",
        "addrMapper.interleave_size": "64B",
        "addrMapper.row_size": "1KiB",
        "clock": mem_clock,
        "mem_size": util_int_to_size(total_mem_bytes),
        "channels": 4,
        "channel.numRanks": 2,
        "channel.rank.numBanks": 16,
        "channel.rank.bank.TRP": 14,
        "printconfig": 1,
    })
    return memctrl, highlink_nic


def _reject_default_link_latency(link_name, latency):
    """CACHE_LINK_LATENCY ("1ps") only exists as a fallback default value
    for latency arguments -- it must never actually be used to build a
    link. SST's cross-rank synchronization interval (min_part, in
    sst-core's main.cc) is set by the *smallest* latency of any link that
    crosses an MPI rank boundary, and every topology built here puts gem5
    nodes on their own rank, so a single un-overridden 1ps default
    anywhere in the graph silently forces the entire simulation to
    synchronize every 1ps of simulated time -- effectively serializing
    what should be a parallel run, with no error or warning to explain
    why. Fail loudly here instead: if this fires, it means a required
    --*-latency argument was left at its default instead of being
    explicitly set.
    """
    # NOTE: don't use UnitAlgebra.getRoundedValue() here -- it rounds to
    # the nearest whole *second* (confirmed empirically: getRoundedValue()
    # returns 0 for 1ps, 50ns, 100ns, 170ns alike, since all of them are
    # << 1 second), so it can't distinguish any of the latencies actually
    # used in this codebase and made this check fire unconditionally.
    # UnitAlgebra's `==` is unit-aware and compares the actual quantity
    # (e.g. 1ps == 1000fs is True, 1ps == 100ns is False) -- use that.
    if UnitAlgebra(latency) == UnitAlgebra(CACHE_LINK_LATENCY):
        print(f"fatal: link '{link_name}' would be built with the "
              f"default {CACHE_LINK_LATENCY} latency. This almost "
              "certainly means a required latency argument (e.g. "
              "--uplink-latency, --memory-link-latency) was left "
              "unset. Pass an explicit, non-default latency instead.")
        exit(-1)


def link(link_name, ep_a, port_a, ep_b, port_b, latency=CACHE_LINK_LATENCY):
    _reject_default_link_latency(link_name, latency)
    l = sst.Link(link_name)
    l.connect((ep_a, port_a, latency), (ep_b, port_b, latency))
    return l


def enable_statistics(output_directory):
    sst.setStatisticLoadLevel(10)
    sst.setStatisticOutput(
        "sst.statOutputTXT",
        {"filepath": output_directory + "/sst-output.txt"})
    sst.enableAllStatisticsForAllComponents()


def build_star(jobs, output_directory, cpu_clock_rate, max_ticks, systemd,
                memory_link_latency="1ps", uplink_latency="1ps"):
    """One merlin.hr_router; every gem5 node's DirectoryController bridge
    and the shared MemController all terminate directly on it:

        gem5_0 --dirctrl_0--\\
        gem5_1 --dirctrl_1---+---> router ---> memory
        ...                 /
        gem5_{N-1} --dirctrl_{N-1}--/

    Router radix = N (gem5 nodes) + 1 (memory).

    Rank layout (total ranks needed = N + 3, matching unified_run.py's
    mpirun -np for a network topology): the router gets its own rank,
    every dirctrl shares a second rank, and memctrl gets a third -- none
    of the N + 3 ranks are shared between different SST element *types*,
    and none are shared with a gem5 node's own rank (1..N) either. See
    create_gem5_node()'s comment for why gem5 nodes can never share a
    rank with each other or with anything else.

    Both the gem5 <-> dirctrl and dirctrl <-> router links use
    `uplink_latency`, not the per-node "latency" field from the jobs
    JSON: that field is a single-hop value from unified_sst.py's bus
    topology, and this topology has two hops in its place, so reusing it
    for both would silently double-count it. `uplink_latency` is an
    explicit, required-in-practice argument instead (see
    _reject_default_link_latency()).
    """
    require_non_shared_memory(jobs)
    validate_disjoint_address_ranges(jobs)

    num_gem5_nodes = len(jobs)
    total_sst_memory = compute_total_sst_memory(jobs)

    router_rank = 0
    dirctrl_rank = num_gem5_nodes + 1
    memctrl_rank = num_gem5_nodes + 2

    print(f"info: building a single-router star for {num_gem5_nodes} gem5 "
          f"node(s) + 1 memory endpoint (router radix = "
          f"{num_gem5_nodes + 1}). Using {num_gem5_nodes + 3} MPI ranks: "
          f"router=rank{router_rank}, dirctrl(s)=rank{dirctrl_rank}, "
          f"memctrl=rank{memctrl_rank}, gem5 nodes=rank1..{num_gem5_nodes}.")

    router = create_router("router", num_gem5_nodes + 1, 0,
                            topology="merlin.singlerouter",
                            rank=router_rank)

    for node in range(num_gem5_nodes):
        job = str(node)

        _gem5_node, memory_port = create_gem5_node(
            node, jobs, output_directory, cpu_clock_rate, max_ticks,
            systemd)

        dirctrl, dirctrl_net = create_node_directory_bridge(
            f"dirctrl_{node}",
            int(jobs[job]["remote-memory"]["start"], 16),
            int(jobs[job]["remote-memory"]["end"], 16) - 1,
            group=1, rank=dirctrl_rank)

        link(f"gem5_{node}_to_dirctrl", memory_port, "port",
             dirctrl, "highlink", latency=uplink_latency)
        link(f"dirctrl_{node}_to_router", dirctrl_net, "port",
             router, f"port{node}", latency=uplink_latency)

    memctrl, memctrl_net = create_memory_bridge(
        "memory", total_sst_memory, group=2, rank=memctrl_rank)
    link("memory_to_router", memctrl_net, "port",
         router, f"port{num_gem5_nodes}",
         latency=memory_link_latency)


def build_tree(jobs, leaf_size, output_directory, cpu_clock_rate, max_ticks,
                systemd, link_bw="25.6GB/s", uplink_latency="1ps",
                memory_link_latency="1ps"):
    """Builds a two-level merlin.fattree network:

        gem5 nodes -> per-node DirectoryController bridge -> leaf router
        (<= leaf_size gem5 nodes per leaf) -> root router -> memory leaf
        (a dedicated, single-host leaf router that carries the MemController)

    Every leaf (gem5 leaves AND the memory leaf) uplinks to the same root
    router.

    Memory is *not* attached directly to the root: fattree.cc's routing
    only supports raw single-host endpoints at level 0 (getPortState()
    returns R2R for every port at level > 0, and attaching a bare host to a
    root down-port makes getEndpointID()'s self-assigned ID inconsistent
    with what route_deterministic() computes for it from other leaves).
    Giving memory its own tiny dedicated leaf keeps every endpoint at
    level 0, where the addressing is provably consistent.

    Latency knobs:
      - gem5 <-> DirectoryController, and DirectoryController <-> its leaf
        router: per-node jobs[job]["remote-memory"]["latency"], via
        remote_memory_latency().
      - leaf router <-> root router (the extra network hop the tree adds
        over the star): `uplink_latency`.
      - memory <-> its dedicated memory leaf: `memory_link_latency`.

    Rank layout (total ranks needed = N + 3, matching unified_run.py's
    mpirun -np for a network topology): every router (all leaf routers
    AND the root router) shares one rank, every dirctrl shares a second
    rank, and memctrl gets a third -- same scheme as build_star(), see
    its docstring and create_gem5_node()'s comment for the full
    rationale (gem5's process-global Root() means gem5 nodes can never
    share a rank with each other or with anything else).
    """
    num_gem5_nodes = len(jobs)
    if num_gem5_nodes == 0:
        print("fatal: jobs file has no gem5 nodes!")
        exit(-1)

    require_non_shared_memory(jobs)
    validate_disjoint_address_ranges(jobs)

    num_gem5_leaves = (num_gem5_nodes + leaf_size - 1) // leaf_size
    total_leaves = num_gem5_leaves + 1  # +1 dedicated memory leaf
    memory_leaf_id = num_gem5_leaves    # last leaf slot

    router_rank = 0
    dirctrl_rank = num_gem5_nodes + 1
    memctrl_rank = num_gem5_nodes + 2

    print(f"info: building a 2-level Merlin router tree for "
          f"{num_gem5_nodes} gem5 node(s): {num_gem5_leaves} gem5 leaf "
          f"router(s) (leaf-size={leaf_size}) + 1 memory leaf, all "
          f"uplinked to 1 root router. Using {num_gem5_nodes + 3} MPI "
          f"ranks: router(s)=rank{router_rank}, "
          f"dirctrl(s)=rank{dirctrl_rank}, memctrl=rank{memctrl_rank}, "
          f"gem5 nodes=rank1..{num_gem5_nodes}.")

    # merlin.fattree shape: "<down,up>:<down,up>:..." from leaves to root.
    #   level 0 (leaves): leaf_size down-ports (hosts), 1 up-port (root)
    #   level 1 (root):   total_leaves down-ports (leaves), 0 up-ports
    shape = f"{leaf_size},1:{total_leaves},0"
    topo_params = {"shape": shape, "routing_alg": "deterministic"}

    total_sst_memory = compute_total_sst_memory(jobs)

    leaf_routers = []
    for leaf in range(total_leaves):
        leaf_routers.append(create_router(
            f"leaf_router_{leaf}", leaf_size + 1, leaf,
            topology="merlin.fattree", topology_params=dict(topo_params),
            link_bw=link_bw, rank=router_rank))

    root_router = create_router(
        "root_router", total_leaves, total_leaves,
        topology="merlin.fattree", topology_params=dict(topo_params),
        link_bw=link_bw, rank=router_rank)

    for leaf in range(total_leaves):
        link(f"leaf_{leaf}_to_root",
             leaf_routers[leaf], f"port{leaf_size}",
             root_router, f"port{leaf}",
             latency=uplink_latency)

    for node in range(num_gem5_nodes):
        print("creating node: ", node)
        job = str(node)
        leaf = node // leaf_size
        port_on_leaf = node % leaf_size
        node_latency = remote_memory_latency(jobs, job)

        _gem5_node, memory_port = create_gem5_node(
            node, jobs, output_directory, cpu_clock_rate, max_ticks,
            systemd)

        dirctrl, dirctrl_net = create_node_directory_bridge(
            f"dirctrl_{node}",
            int(jobs[job]["remote-memory"]["start"], 16),
            int(jobs[job]["remote-memory"]["end"], 16) - 1,
            group=1, rank=dirctrl_rank)

        link(f"gem5_{node}_to_dirctrl", memory_port, "port",
             dirctrl, "highlink", latency=node_latency)
        link(f"dirctrl_{node}_to_leaf{leaf}", dirctrl_net, "port",
             leaf_routers[leaf], f"port{port_on_leaf}",
             latency=node_latency)

    memctrl, memctrl_net = create_memory_bridge(
        "memory", total_sst_memory, group=2, rank=memctrl_rank)
    link("memory_to_memory_leaf", memctrl_net, "port",
         leaf_routers[memory_leaf_id], "port0",
         latency=memory_link_latency)
