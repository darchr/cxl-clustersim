// Copyright (c) 2021-2023 The Regents of the University of California
// All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are
// met: redistributions of source code must retain the above copyright
// notice, this list of conditions and the following disclaimer;
// redistributions in binary form must reproduce the above copyright
// notice, this list of conditions and the following disclaimer in the
// documentation and/or other materials provided with the distribution;
// neither the name of the copyright holders nor the names of its
// contributors may be used to endorse or promote products derived from
// this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
// "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
// LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
// A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
// OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
// SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
// LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
// DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
// THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
// OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#ifndef __SST_RESPONDER_SUBCOMPONENT_HH__
#define __SST_RESPONDER_SUBCOMPONENT_HH__

#define TRACING_ON 0

#include <string>
#include <vector>
#include <unordered_map>
#include <queue>

#include <sst/core/sst_config.h>
#include <sst/core/component.h>
#include <sst/core/interfaces/stringEvent.h>
#include <sst/core/interfaces/stdMem.h>

#include <sst/core/eli/elementinfo.h>
#include <sst/core/link.h>

// from gem5
#include <sim/sim_object.hh>
#include <sst/outgoing_request_bridge.hh>
#include <sst/external_memory.hh>
#include <sim/root.hh>
#include <sst/sst_responder_interface.hh>
#include <mem/backdoor.hh>

#include "translator.hh"
#include "sst_responder.hh"

class SSTResponderSubComponent: public SST::SubComponent
{
  private:
    // gem5::OutgoingRequestBridge* responseReceiver;
    // responseReceiver for this branch is hardcoded to ExternalMemory*.
    // TODO: We need to make a better design to handle multiple types of
    // outgoing request classes.
    gem5::ExternalMemory* responseReceiver;
    gem5::SSTResponderInterface* sstResponder;

    SST::Interfaces::StandardMem* memoryInterface;
    // SST::MemHierarchy::Backend::Backing* backingStore;
    SST::TimeConverter* timeConverter;
    SST::Output* output;
    std::queue<gem5::PacketPtr> responseQueue;

    std::vector<SST::Interfaces::StandardMem::Request*> initRequests;

    std::string gem5SimObjectName;
    std::string memSize;
    uint64_t processed_addr;
    uint64_t bytes_per_phase;
    int count_limit;
    int phases_needed;

    // Throttling of gem5-side request injection: caps how many requests
    // this bridge will have outstanding towards SST (sent but not yet
    // responded to) at once, analogous to a real CXL link/device's
    // outstanding-transaction credit limit. Requests received while at the
    // cap are buffered in throttledRequestQueue instead of being handed to
    // memoryInterface, and released FIFO as responses free up slots.
    // 0 disables throttling (unbounded, the historical behavior).
    uint32_t maxInflightRequests;
    uint32_t inflightRequests;
    std::queue<SST::Interfaces::StandardMem::Request*> throttledRequestQueue;

    // Called whenever a response is received for a request that occupied an
    // inflight slot: frees that slot and, if throttling is enabled, drains
    // as many queued requests as now fit under the cap.
    void releaseInflightSlot();

    // Separate admission control for *posted* writes (requests that
    // don't have an sstRequestIdToPacketMap entry -- see
    // handleTimingReq()'s comment, e.g. gem5 cache writebacks). These
    // can't share maxInflightRequests/releaseInflightSlot() above: that
    // mechanism releases a slot when a response matches
    // sstRequestIdToPacketMap, which posted writes are never in by
    // definition, so counting them there would just leak slots forever
    // (this is exactly the bug that used to make the bridge deadlock).
    // Instead: WriteResp responses to posted writes still exist (SST
    // still executes them and acks them -- see
    // Translator::gem5RequestToSSTRequest()'s posted=false), they just
    // arrive as *unsolicited* responses (no sstRequestIdToPacketMap
    // match) since gem5 never asked to be told about them. Any
    // unsolicited WriteResp can only ever correspond to one of our own
    // posted writes (nothing else in this pipeline generates one), so
    // UnsolicitedRequestHandler uses that arrival, instead of a
    // response match, as the release signal here.
    //
    // Note this bounds burst size, not overflow: maxPostedWrites is a
    // *separate* budget from maxInflightRequests, and both draw against
    // the same downstream DirectoryController MSHR
    // (network_common.create_node_directory_bridge's mshr_num_entries),
    // so their sum can still exceed it under sustained combined
    // read/write load. It substantially reduces how often that happens
    // relative to posted writes being completely unthrottled, but
    // doesn't guarantee it can't.
    uint32_t maxPostedWrites;
    uint32_t postedWritesOutstanding;
    std::queue<SST::Interfaces::StandardMem::Request*> postedWriteQueue;
    void releasePostedWriteSlot();

    // Dispatches on the concrete SST::Interfaces::StandardMem::Request
    // subtype without dynamic_cast, via the RequestHandler double-dispatch
    // API stdMem.h already provides (Request::handle(RequestHandler*)).
    // Forward-declared here (nested classes are full members and so get
    // access to SSTResponderSubComponent's private members, e.g.
    // responseReceiver/memoryInterface/blocked()/responseQueue); fully
    // defined in the .cc, next to their one use site each.
    class UnsolicitedRequestHandler;
    class SwapReqResponseHandler;

  public:
    SSTResponderSubComponent(SST::ComponentId_t id, SST::Params& params);
    ~SSTResponderSubComponent();

    void init(unsigned phase);
    void setTimeConverter(SST::TimeConverter* tc);
    void setOutputStream(SST::Output* output_);

    // void setResponseReceiver(gem5::OutgoingRequestBridge* gem5_bridge);
    void setResponseReceiver(gem5::ExternalMemory* gem5_bridge);
    void portEventHandler(SST::Interfaces::StandardMem::Request* request);

    bool blocked();
    void setup();

    // return true if the SimObject could be found
    bool findCorrespondingSimObject(gem5::Root* gem5_root);

    bool handleTimingReq(SST::Interfaces::StandardMem::Request* request);
    void handleRecvRespRetry();
    void handleRecvFunctional(gem5::PacketPtr pkt);
    void handleSwapReqResponse(SST::Interfaces::StandardMem::Request* request);

    TPacketMap sstRequestIdToPacketMap;

  public: // register the component to SST
    SST_ELI_REGISTER_SUBCOMPONENT_API(SSTResponderSubComponent);
    SST_ELI_REGISTER_SUBCOMPONENT(
        SSTResponderSubComponent,
        "gem5", // SST will look for libgem5.so or libgem5.dylib
        "gem5Bridge",
        SST_ELI_ELEMENT_VERSION(1, 0, 0),
        "Initialize gem5 and link SST's ports to gem5's ports",
        SSTResponderSubComponent
    )

    SST_ELI_DOCUMENT_SUBCOMPONENT_SLOTS(
        {"memory", "Interface to the memory subsystem", \
         "SST::Interfaces::StandardMem"}
    )

    SST_ELI_DOCUMENT_PORTS(
        {"port", "Handling mem events", {"memHierarchy.MemEvent", ""}}
    )

    SST_ELI_DOCUMENT_PARAMS(
        {"response_receiver_name", \
         "Name of the SimObject receiving the responses"},
        {"max_inflight_requests", \
         "Max number of requests this bridge will have outstanding " \
         "towards SST at once (like a CXL link's outstanding-transaction " \
         "credit limit). Extra requests are queued client-side until a " \
         "slot frees up. 0 disables throttling. Default: 64 (matches " \
         "the DirectoryController bridge's mshr_num_entries default in " \
         "network_common.create_node_directory_bridge, so the bridge " \
         "never injects more requests than the downstream MSHR can " \
         "hold)."},
        {"max_posted_writes", \
         "Max number of posted writes (e.g. gem5 cache writebacks -- " \
         "requests with no matching sstRequestIdToPacketMap entry, see " \
         "handleTimingReq()) this bridge will have outstanding towards " \
         "SST at once. Separate budget from max_inflight_requests, " \
         "since posted writes have no response to key a normal release " \
         "on. Extra posted writes are queued client-side until a slot " \
         "frees up. 0 disables throttling. Default: 32. Note this and " \
         "max_inflight_requests draw against the same downstream MSHR, " \
         "so their sum can still exceed its capacity under sustained " \
         "combined load -- this reduces, but doesn't eliminate, " \
         "burst-driven MSHR overflow from posted writes."}
    )

};

#endif // __SST_RESPONDER_SUBCOMPONENT_HH__
