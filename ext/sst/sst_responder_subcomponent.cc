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

#include "sst_responder_subcomponent.hh"
// #include <sst/elements/memHierarchy/membackend/backing.h>

#include <cassert>
#include <sstream>
#include <iomanip>

#ifdef fatal  // gem5 sets this
#undef fatal
#endif

static const int NUM_RESTORE_PHASES = 6;
SSTResponderSubComponent::SSTResponderSubComponent(SST::ComponentId_t id,
                                                   SST::Params& params)
    : SubComponent(id)
{
    sstResponder = new SSTResponder(this);
    gem5SimObjectName = params.find<std::string>("response_receiver_name", "");
    memSize = params.find<std::string>("mem_size", "8GiB");
    if (gem5SimObjectName == "")
        assert(false && "The response_receiver_name must be specified");

    // Default (64) matches network_common.create_node_directory_bridge's
    // mshr_num_entries default, so the bridge never injects more requests
    // than the downstream DirectoryController's MSHR can actually hold.
    maxInflightRequests = \
        (uint32_t)params.find<uint32_t>("max_inflight_requests", 64);
    inflightRequests = 0;

    maxPostedWrites = \
        (uint32_t)params.find<uint32_t>("max_posted_writes", 32);
    postedWritesOutstanding = 0;
}

SSTResponderSubComponent::~SSTResponderSubComponent()
{
    // Anything still sitting in the throttle queue was never handed to
    // memoryInterface, so it's ours to free.
    while (!throttledRequestQueue.empty()) {
        delete throttledRequestQueue.front();
        throttledRequestQueue.pop();
    }
    while (!postedWriteQueue.empty()) {
        delete postedWriteQueue.front();
        postedWriteQueue.pop();
    }
    delete sstResponder;
}

void
SSTResponderSubComponent::setTimeConverter(SST::TimeConverter* tc)
{
    timeConverter = tc;

    // Get the memory interface
    SST::Params interface_params;
    // This is how you tell the interface the name of the port it should use
    interface_params.insert("port", "port");
    interface_params.insert("mem_size", memSize.c_str());
    // Loads a “memHierarchy.memInterface” into index 0 of the “memory” slot
    // SHARE_PORTS means the interface can use our port as if it were its own
    // INSERT_STATS means the interface will inherit our statistic
    //   configuration (e.g., if ours are enabled, the interface’s will be too)
    memoryInterface = loadAnonymousSubComponent<SST::Interfaces::StandardMem>(
        "memHierarchy.standardInterface", "memory", 0,
        SST::ComponentInfo::SHARE_PORTS | SST::ComponentInfo::INSERT_STATS,
        interface_params, timeConverter,
        new SST::Interfaces::StandardMem::Handler<SSTResponderSubComponent>(
            this, &SSTResponderSubComponent::portEventHandler)
    );
    assert(memoryInterface != NULL);
}

void
SSTResponderSubComponent::setOutputStream(SST::Output* output_)
{
    output = output_;
}

void
SSTResponderSubComponent::setResponseReceiver(
    gem5::ExternalMemory* gem5_bridge)
{
    // The response receiver in this branch is ExternalMemory. This is defined
    // in the header.
    responseReceiver = gem5_bridge;
    responseReceiver->setResponder(sstResponder);
}

bool
SSTResponderSubComponent::handleTimingReq(
    SST::Interfaces::StandardMem::Request* request)
{
    // Only requests registered in sstRequestIdToPacketMap can ever
    // trigger releaseInflightSlot() -- portEventHandler() only releases
    // a slot when an incoming response's ID matches an entry there.
    // Anything else has no possible way to ever release a slot if we
    // counted it here, so it must bypass the throttle entirely (sent
    // immediately, never queued, never counted) rather than leaking a
    // slot forever. This is NOT the same as checking
    // request->needsResponse(): Translator::gem5RequestToSSTRequest()
    // always constructs Write requests with posted=false (see
    // translator.hh / stdMem.h's Write constructor default), so a
    // translated Write's own needsResponse() is always true regardless
    // of whether the *original* gem5 packet (e.g. a cache writeback)
    // needed one -- sstRequestIdToPacketMap membership reflects that
    // original gem5-side needsResponse() correctly, since that's
    // exactly what gates insertion into it.
    bool tracksCredit = sstRequestIdToPacketMap.find(request->getID()) !=
        sstRequestIdToPacketMap.end();

    if (!tracksCredit) {
        // Untracked request: nothing will ever call releaseInflightSlot()
        // for it (see above), so it can't go through the
        // maxInflightRequests/throttledRequestQueue admission control at
        // all. If it's specifically a posted write (the common case --
        // e.g. a gem5 cache writeback), it still gets its own, separately
        // budgeted admission control instead of being sent completely
        // unthrottled -- see maxPostedWrites'/releasePostedWriteSlot()'s
        // comment in the header for why a normal response-keyed release
        // doesn't work here and what does instead. Any other untracked
        // type (there isn't currently one Translator::
        // gem5RequestToSSTRequest() produces, but being conservative)
        // is sent immediately, matching the historical unthrottled
        // behavior, since we have no known release signal for it.
        Translator::IsWriteDetector writeCheck(output);
        request->handle(&writeCheck);
        if (writeCheck.isWrite) {
            if (maxPostedWrites > 0 &&
                    postedWritesOutstanding >= maxPostedWrites) {
                postedWriteQueue.push(request);
                return true;
            }
            ++postedWritesOutstanding;
        }
        memoryInterface->send(request);
        return true;
    }

    // Throttle: if we're already at the outstanding-request cap, hold this
    // request client-side instead of forwarding it to SST. Note this can't
    // signal backpressure back to gem5 (ExternalMemory::handleTiming
    // ignores our return value and always reports success unless
    // ExternalMemory's own enable_backpressure is set) -- when it's not,
    // gem5 keeps issuing at its own rate and we just delay when each
    // request actually reaches SST's memory subsystem, which is enough to
    // bound how many requests pile up in the DirectoryController/MemNIC
    // pipeline at once.
    if (maxInflightRequests > 0 && inflightRequests >= maxInflightRequests) {
        throttledRequestQueue.push(request);
        return true;
    }
    ++inflightRequests;
    memoryInterface->send(request);
    return true;
}

void
SSTResponderSubComponent::releasePostedWriteSlot()
{
    if (maxPostedWrites == 0)
        return;

    if (postedWritesOutstanding > 0)
        --postedWritesOutstanding;

    while (!postedWriteQueue.empty() &&
           postedWritesOutstanding < maxPostedWrites) {
        SST::Interfaces::StandardMem::Request* next = \
            postedWriteQueue.front();
        postedWriteQueue.pop();
        ++postedWritesOutstanding;
        memoryInterface->send(next);
    }
}

void
SSTResponderSubComponent::releaseInflightSlot()
{
    if (maxInflightRequests == 0)
        return;

    if (inflightRequests > 0)
        --inflightRequests;

    while (!throttledRequestQueue.empty() &&
           inflightRequests < maxInflightRequests) {
        SST::Interfaces::StandardMem::Request* next = \
            throttledRequestQueue.front();
        throttledRequestQueue.pop();
        ++inflightRequests;
        memoryInterface->send(next);
    }
}
/*
void
SSTResponderSubComponent::init(unsigned phase)
{
    std::cout << phase << " " << phases_needed << std::endl;
    if (phase == 0) {
        // Added support for MPI send and recv. We have to split and send
        // gem5's data in phases to SST.
        // get the size of this memory.
        // We are using a MemBackdoor to get the data to restore from gem5.
        gem5::MemBackdoorPtr data;
        responseReceiver->getBackdoor(data);
        assert(data->readable());

        uint64_t memory_size = data->range().end() - data->range().start();

        // phases needed must be an integer. creating a temporary variable.
        uint64_t unsigned_phases_needed = memory_size/(1 << 30);
        phases_needed = (int)unsigned_phases_needed;
        
        // we read the mem in 1 MB blocks
        count_limit = 1024;
        processed_addr = 0x0;
    }
    for (int i = 0 ; i < phases_needed ; i++) {
        // TODO: This needs to be distinguished whether we are simulating a
        // full memory in SST or we are restoring SST's memory
        // odd phases send data from gem5 to SST
        if (phase == i * 2 + 1) {
            // We are using a MemBackdoor to get the data to restore from gem5.
            gem5::MemBackdoorPtr data;
            responseReceiver->getBackdoor(data);
            assert(data->readable());

            // We are loading a lot of data in one instance for faster
            // initializtion.
            const uint64_t chunk_size = 1 << 20;
            
            // So here is the thing about membackdoor. It has the size of the
            // memroy preserved however, the data pointer always stats at 0x0.
            // When we are loading this data (this case), the data has to be
            // correctly offset to read and restore.
            // (start of backdoor) 0x0 -> 0x100000000 (start of remote memory)
            //                        0x4 -> 0x100000004
            //                        ..
            //                 0x80000000 -> 0x180000000
            for (gem5::Addr addr = processed_addr;
                    addr < ((uint64_t)((phase/2) + 1) * \
                            (uint64_t)count_limit * chunk_size); 
                    addr += chunk_size) {
                std::vector<uint8_t> chunk(data->ptr() + addr,
                                           data->ptr() + addr + chunk_size);
                SST::Interfaces::StandardMem::Request* request = \
                    new SST::Interfaces::StandardMem::Write(
                        data->range().start() + addr, chunk_size, chunk);
                memoryInterface->sendUntimedData(request);
	    		delete request;
            }
            processed_addr += (1 << 30);

            // clear the data to free the memory at the final phase 
            if (i == phases_needed)
                responseReceiver->clearInitData();
        }
        memoryInterface->init(phase);    
    }
    if (phase >= phases_needed)
        memoryInterface->init(phase);
}
*/
void
SSTResponderSubComponent::init(unsigned phase)
{
    std::cout << "myname: " << this->getName() << std::endl;
    if (phase == 0) {
        // Added support for MPI send and recv. We have to split and send
        // gem5's data in phases to SST.
        // get the size of this memory.
        // We are using a MemBackdoor to get the data to restore from gem5.
        gem5::MemBackdoorPtr data;
        responseReceiver->getBackdoor(data);
        assert(data->readable());

        uint64_t memory_size = data->range().end() - data->range().start();

        // See NUM_RESTORE_PHASES above for why this is a fixed count
        // rather than memory_size / 1GiB. memory_size == 0 means no
        // remote memory to restore at all.
        phases_needed = (memory_size == 0) ? 0 : NUM_RESTORE_PHASES;
        bytes_per_phase = (phases_needed == 0) ? 0 :
            (memory_size + phases_needed - 1) / phases_needed;

        processed_addr = 0x0;
    }

    // Odd phases (1, 3, 5, ...) each send one slice of the checkpoint
    // image; i is that slice's index. Even phases don't send data here.
    int i = (phase >= 1 && (phase % 2) == 1) ? (int)((phase - 1) / 2) : -1;
    if (i >= 0 && i < phases_needed) {
        // We are using a MemBackdoor to get the data to restore from gem5.
        gem5::MemBackdoorPtr data;
        responseReceiver->getBackdoor(data);
        assert(data->readable());

        uint64_t memory_size = data->range().end() - data->range().start();

        // We read the mem in 1 MB blocks.
        const uint64_t chunk_size = 1 << 20;

        // So here is the thing about membackdoor. It has the size of the
        // memroy preserved however, the data pointer always stats at 0x0.
        // When we are loading this data (this case), the data has to be
        // correctly offset to read and restore.
        // (start of backdoor) 0x0 -> 0x100000000 (start of remote memory)
        //                        0x4 -> 0x100000004
        //                        ..
        //                 0x80000000 -> 0x180000000
        uint64_t phase_end = std::min(
            (uint64_t)(i + 1) * bytes_per_phase, memory_size);
        for (gem5::Addr addr = processed_addr; addr < phase_end;
                addr += chunk_size) {
            uint64_t this_chunk_size = std::min(chunk_size, phase_end - addr);
            std::vector<uint8_t> chunk(data->ptr() + addr,
                                       data->ptr() + addr + this_chunk_size);
            SST::Interfaces::StandardMem::Request* request = \
                new SST::Interfaces::StandardMem::Write(
                    data->range().start() + addr, this_chunk_size, chunk);
            memoryInterface->sendUntimedData(request);
            delete request;
        }
        processed_addr = phase_end;

        // Clear the data to free the memory once the final restore phase
        // has been sent.
        if (i == phases_needed - 1)
            responseReceiver->clearInitData();
    }
    memoryInterface->init(phase);
}


void
SSTResponderSubComponent::setup()
{
}

bool
SSTResponderSubComponent::findCorrespondingSimObject(gem5::Root* gem5_root)
{
    /*
    gem5::OutgoingRequestBridge* receiver = \
        dynamic_cast<gem5::OutgoingRequestBridge*>(
            gem5_root->find(gem5SimObjectName.c_str()));
    }
    */
    gem5::ExternalMemory* receiver = \
        dynamic_cast<gem5::ExternalMemory*>(
            gem5_root->find(gem5SimObjectName.c_str()));
    setResponseReceiver(receiver);
    return receiver != NULL;
}

/* A SwapReq's underlying SST response is always a ReadResp -- that's a
 * protocol invariant of how SwapReq is issued, not something to silently
 * tolerate a mismatch on, so this deliberately does NOT derive from
 * Translator::NoOpRequestHandler: any other type reaching handle() here
 * falls through to RequestHandler's base "not implemented" fatal(),
 * which is exactly the loud failure a genuine protocol violation should
 * get. */
class SSTResponderSubComponent::SwapReqResponseHandler :
    public SST::Interfaces::StandardMem::RequestHandler
{
  public:
    SwapReqResponseHandler(SSTResponderSubComponent* owner,
                            gem5::PacketPtr pkt, SST::Output* output) :
        SST::Interfaces::StandardMem::RequestHandler(output),
        owner(owner), pkt(pkt)
    {}

    void handle(SST::Interfaces::StandardMem::ReadResp* request) override
    {
        // get the data, then,
        //     1. send a response to gem5 with the original data
        //     2. send a write to memory with atomic op applied
        std::vector<uint8_t> data = request->data;

        // step 1
        pkt->setData(request->data.data());
        pkt->makeAtomicResponse();
        pkt->headerDelay = pkt->payloadDelay = 0;
        if (owner->blocked() || !owner->responseReceiver->sendTimingResp(pkt))
            owner->responseQueue.push(pkt);

        // step 2
        (*(pkt->getAtomicOp()))(data.data()); // apply the atomic op
        // This is a Write. Need to use the Write visitor class. But the
        // original request is a read response. Therefore, we need to find
        // the address and the data size and then call Write.
        SST::Interfaces::StandardMem::Addr addr = request->pAddr;
        auto data_size = data.size();
        // Create the Write request here.
        SST::Interfaces::StandardMem::Request* write_request = \
            new SST::Interfaces::StandardMem::Write(addr, data_size, data);
        // F_LOCKED flag in SimpleMem was changed to ReadLock and
        // WriteUnlock visitor classes. This has to be addressed in the
        // future. The boot test works without using ReadLock and
        // WriteUnlock classes.
        owner->memoryInterface->send(write_request);
    }

  private:
    SSTResponderSubComponent* owner;
    gem5::PacketPtr pkt;
};

void
SSTResponderSubComponent::handleSwapReqResponse(
    SST::Interfaces::StandardMem::Request* request)
{
    TPacketMap::iterator it = \
        sstRequestIdToPacketMap.find(request->getID());
    assert(it != sstRequestIdToPacketMap.end());
    gem5::PacketPtr pkt = it->second;

    SwapReqResponseHandler handler(this, pkt, output);
    request->handle(&handler);

    delete request;
}

/* Handles a request that arrived from SST without a matching outstanding
 * gem5 request (the sstRequestIdToPacketMap miss case in
 * portEventHandler() below). Read/ReadResp/WriteResp need no action
 * there (inherited no-op from NoOpRequestHandler); only FlushAddr turns
 * into a snoop sent up to gem5. */
class SSTResponderSubComponent::UnsolicitedRequestHandler :
    public Translator::NoOpRequestHandler
{
  public:
    UnsolicitedRequestHandler(SSTResponderSubComponent* owner,
                               SST::Output* output) :
        Translator::NoOpRequestHandler(output), owner(owner)
    {}

    // An unsolicited WriteResp (no sstRequestIdToPacketMap match) can
    // only ever be the completion of one of our own posted writes --
    // nothing else in this pipeline generates one -- so its arrival is
    // exactly the release signal maxPostedWrites' admission control
    // needs (see releasePostedWriteSlot()'s comment in the header). gem5
    // still isn't told about it (it never asked), this is purely
    // internal bookkeeping.
    void handle(SST::Interfaces::StandardMem::WriteResp*) override
    {
        owner->releasePostedWriteSlot();
    }

    void handle(SST::Interfaces::StandardMem::FlushAddr* request) override
    {
        // for Snoop/no response needed
        // presently no consideration for masterId, packet type, flags...
        gem5::RequestPtr req = std::make_shared<gem5::Request>(
            request->pAddr, request->size, 0, 0);

        gem5::PacketPtr pkt = new gem5::Packet(
            req, gem5::MemCmd::InvalidateReq);

        // Clear out bus delay notifications
        pkt->headerDelay = pkt->payloadDelay = 0;

        owner->responseReceiver->sendTimingSnoopReq(pkt);
    }

  private:
    SSTResponderSubComponent* owner;
};

void
SSTResponderSubComponent::portEventHandler(
    SST::Interfaces::StandardMem::Request* request)
{
    // Expect to handle an SST response
    SST::Interfaces::StandardMem::Request::id_t request_id = request->getID();

    TPacketMap::iterator it = sstRequestIdToPacketMap.find(request_id);

    // replying to a prior request
    if (it != sstRequestIdToPacketMap.end()) {
        // This response frees up the inflight slot that request occupied;
        // do this before any early-return branches below (e.g. SwapReq) so
        // a queued request always gets released promptly.
        releaseInflightSlot();

        gem5::PacketPtr pkt = it->second; // the packet that needs response

        // Responding to a SwapReq requires a special handler
        //     1. send a response to gem5 with the original data
        //     2. send a write to memory with atomic op applied
        if ((gem5::MemCmd::Command)pkt->cmd.toInt() == gem5::MemCmd::SwapReq) {
            handleSwapReqResponse(request);
            return;
        }

        sstRequestIdToPacketMap.erase(it);

        Translator::inplaceSSTRequestToGem5PacketPtr(pkt, request, output);

        if (blocked() || !(responseReceiver->sendTimingResp(pkt))) {
            responseQueue.push(pkt);
        }
    } else {
        // we can handle a few types of requests.
        UnsolicitedRequestHandler handler(this, output);
        request->handle(&handler);
    }

    delete request;
}

void
SSTResponderSubComponent::handleRecvRespRetry()
{
    while (blocked() &&
           responseReceiver->sendTimingResp(responseQueue.front()))
        responseQueue.pop();
}

// void
// SSTResponderSubComponent::handleRecvFunctional(gem5::PacketPtr pkt)
// {
// }

void
SSTResponderSubComponent::handleRecvFunctional(gem5::PacketPtr pkt)
{
    // SST does not understand what is a functional access in gem5 since SST
    // only allows functional accesses at init time. Since it
    // has all the stored in it's memory, any functional access made to SST has
    // to be correctly handled. The idea here is to convert this functional
    // access into a timing access and keep the SST memory consistent.
    
    gem5::Addr addr = pkt->getAddr();
    uint8_t* ptr = pkt->getPtr<uint8_t>();
    uint64_t size = pkt->getSize();

    // Create a new request to handle this request immediately.
    SST::Interfaces::StandardMem::Request* request = nullptr;

    // we need a minimal translator here which does reads and writes. Any other
    // command type is unexpected and the program should crash immediately.
    switch((gem5::MemCmd::Command)pkt->cmd.toInt()) {
        case gem5::MemCmd::WriteReq: {
            std::vector<uint8_t> data(ptr, ptr+size);
            request = new SST::Interfaces::StandardMem::Write(
                addr, data.size(), data);
            break;
        }
        case gem5::MemCmd::ReadReq: {
            request = new SST::Interfaces::StandardMem::Read(addr, size);
            break;
        }
        // case gem5::MemCmd::WriteResp:
        // case gem5::MemCmd::ReadResp: {
        //     // std::vector<uint8_t> data(ptr, ptr+size);
        //     // request = new SST::Interfaces::StandardMem::ReadResp(
        //     //     0, addr, data.size(), data);
        //     return;
        // }
        default:
            panic(
                "handleRecvFunctional: Unable to convert gem5 packet: %s\n",
                pkt->cmd.toString()
            );
    }
    if(pkt->req->isUncacheable()) {
        request->setFlag(
            SST::Interfaces::StandardMem::Request::Flag::F_NONCACHEABLE);
    }
    memoryInterface->send(request);
}
bool
SSTResponderSubComponent::blocked()
{
    return !(responseQueue.empty());
}
